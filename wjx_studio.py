# -*- coding: utf-8 -*-
"""
问卷星【自有测试问卷】可视化工作台 —— 本地后端，零第三方依赖

启动：双击 启动.bat，或 python wjx_studio.py
然后浏览器会自动打开 http://127.0.0.1:8765

只监听 127.0.0.1，不对外暴露。题目识别、重心预览、批量填写都在这个界面里完成。
"""
from __future__ import annotations

import html as html_lib
import json
import os
import re
import subprocess
import sys
import threading
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import wjx_autofill as core
from validate_profile import (validate_profile, MAX_CONCURRENCY,
                              RECOMMENDED_CONCURRENCY, CONCURRENCY_NOTE)

BASE = Path(__file__).parent
PROFILE = BASE / "answer_profile.json"
HTML = BASE / "wjx_studio.html"
SCRIPT = BASE / "wjx_autofill.py"
LOG = BASE / "wjx_run.log"
PORT = 8765

_proc = None
_proc_lock = threading.Lock()
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# 问卷星页面尾部的全局输入框，不属于任何一道题，收集 text_ids 时必须排除，
# 否则开放题恰好在最后一题时会被塞进这些 id，顺序一变就填到别人的框里。
GLOBAL_INPUT_IDS = {"matrixinput", "clipvotecode", "clipvote", "captcha", "verifycode"}
GLOBAL_INPUT_WORDS = ("captcha", "verify", "votecode", "clipvote", "matrixinput")


# ------------------------- 问卷题目识别 -------------------------

def clean(s):
    s = re.sub(r"<[^>]+>", "", s)
    s = html_lib.unescape(s)
    return re.sub(r"\s+", " ", s).strip()[:48]


def label_for(block, oid, idx):
    if oid:
        pat = (r"<div\s+class=['\"]label['\"][^>]*for=['\"]" + re.escape(oid)
               + r"['\"][^>]*>(.*?)</div>")
        m = re.search(pat, block, re.S | re.I)
        if not m:
            m = re.search(r"<label[^>]*for=['\"]" + re.escape(oid) + r"['\"][^>]*>(.*?)</label>",
                          block, re.S | re.I)
        if m and clean(m.group(1)):
            return clean(m.group(1))
        m = re.search(r"for=['\"]" + re.escape(oid) + r"['\"][^>]*dit=['\"]([^'\"]+)['\"]",
                      block, re.I)
        if m:
            try:
                t = urllib.parse.unquote(m.group(1))
                if t:
                    return t[:48]
            except Exception:
                pass
    return f"选项{idx}"


def parse_html(page_html):
    """按题目块 + input 的 name 分组识别，兼容单选/多选/填空/矩阵量表多组选项。"""
    blocks = re.split(r"(?=<div\s+class=['\"]field\s+ui-field-contain)", page_html, flags=re.I)
    meta = {}
    for block in blocks[1:]:
        tm = re.search(r"\btopic=['\"](\d+)['\"]", block)
        num = tm.group(1) if tm else "?"
        tt = re.search(r"<div\s+class=['\"]topichtml['\"][^>]*>(.*?)</div>", block, re.S | re.I)
        title = clean(tt.group(1)) if tt else f"第{num}题"
        if not title:
            title = f"第{num}题"

        groups = {}
        for m in re.finditer(r"<input\b([^>]*)>", block, re.I):
            attrs = dict(re.findall(r"([\w-]+)\s*=\s*['\"]([^'\"]*)['\"]", m.group(1)))
            typ = (attrs.get("type") or "").lower()
            if typ not in ("radio", "checkbox"):
                continue
            name = attrs.get("name") or f"q{num}"
            g = groups.setdefault(name, {"kind": "checkbox" if typ == "checkbox" else "radio",
                                         "opts": [], "ids": []})
            oid = attrs.get("id") or ""
            g["opts"].append(label_for(block, oid, len(g["opts"]) + 1))
            g["ids"].append(oid)

        if groups:
            multi = len(groups) > 1
            for k, (name, g) in enumerate(groups.items()):
                meta[name] = {
                    "title": title + (f" · 第{k + 1}组" if multi else ""),
                    "kind": g["kind"], "opts": g["opts"], "ids": g["ids"], "topic": num,
                }
        else:
            # 没有 radio/checkbox：填空/简答/开放题。
            # 收集真实输入框 id 供 APPLY_JS 精确定位。注意 re.split 的最后一个题块会
            # 带上页面尾部的全局元素（matrixinput / clipVoteCode 等），必须过滤并按题号排序，
            # 否则开放题在末尾时会被塞进别人的 id，顺序一变就填错框。
            ranked = []
            for tm2 in re.finditer(r"<(textarea|input)\b([^>]*)>", block, re.I):
                tag = tm2.group(1).lower()
                raw = tm2.group(2)
                attrs = dict(re.findall(r"([\w-]+)\s*=\s*['\"]([^'\"]*)['\"]", raw))
                typ = (attrs.get("type") or ("textarea" if tag == "textarea" else "text")).lower()
                if typ not in ("text", "textarea", "search", "tel", "email", "number", ""):
                    continue
                if "readonly" in raw.lower() or "disabled" in raw.lower():
                    continue                      # clipVoteCode 之类只读系统字段
                oid = attrs.get("id") or attrs.get("name") or ""
                low = oid.lower()
                if not oid or low in GLOBAL_INPUT_IDS or any(w in low for w in GLOBAL_INPUT_WORDS):
                    continue
                # 与本题号匹配的排最前；明显属于别的题号（q\d+）的降权
                if low == f"q{num}" or low.startswith(f"q{num}_"):
                    rank = 0
                elif re.match(r"q\d+", low):
                    rank = 2
                else:
                    rank = 1
                ranked.append((rank, oid))
            ranked.sort(key=lambda x: x[0])       # 稳定排序，同分保持页面顺序
            text_ids = []
            for _, oid in ranked:
                if oid not in text_ids:
                    text_ids.append(oid)
            meta[f"q{num}"] = {"title": title, "kind": "text", "opts": [], "ids": [],
                               "text_ids": text_ids, "topic": num}
    return meta


def fetch_page(url):
    url = url.strip().split("#")[0]
    if not re.match(r"^https?://", url):
        raise ValueError("链接要以 http:// 或 https:// 开头")
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept-Encoding": "identity",
        "Accept": "text/html,application/xhtml+xml",
    })
    with urllib.request.urlopen(req, timeout=25) as r:
        raw = r.read()
    for enc in ("utf-8", "gbk", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def read_profile():
    if PROFILE.exists():
        try:
            return json.loads(PROFILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"url": "", "total": 20, "concurrency": 3, "headless": False,
            "bias": 0.0, "sharpness": 2.0, "questions_meta": {}, "questions": {}}


def save_profile(p):
    tmp = PROFILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(p, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, PROFILE)   # 原子替换，脚本热加载不会读到半截文件


def default_questions(meta):
    return {q: core.default_question_cfg(m) for q, m in meta.items()}


def is_running():
    return _proc is not None and _proc.poll() is None


def check_env():
    """开跑前先给一句人话提示，别让用户面对一串 traceback。"""
    try:
        import playwright  # noqa: F401
    except ImportError:
        return {"ok": False, "msg": "缺少 playwright：在本目录执行 pip install playwright"}
    # 本机装了 Edge / Chrome 就能直接跑，不必下载 Chromium
    pf = os.environ.get("ProgramFiles", "C:/Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")
    local_browsers = [
        Path(pf86) / "Microsoft/Edge/Application/msedge.exe",
        Path(pf) / "Microsoft/Edge/Application/msedge.exe",
        Path(pf) / "Google/Chrome/Application/chrome.exe",
        Path(pf86) / "Google/Chrome/Application/chrome.exe",
    ]
    if any(p.exists() for p in local_browsers):
        return {"ok": True, "msg": "环境就绪（使用本机 Edge / Chrome）"}

    cache = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if cache:
        root = Path(cache)
    elif sys.platform == "win32":
        root = Path.home() / "AppData" / "Local" / "ms-playwright"
    else:
        root = Path.home() / ".cache" / "ms-playwright"
    if not any(root.glob("chromium-*")) and not any(root.glob("chromium_headless_shell-*")):
        return {"ok": False, "msg": "浏览器还没下载：执行 playwright install chromium"}
    return {"ok": True, "msg": "环境就绪"}


# ------------------------------ HTTP ------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            data = HTML.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        elif path == "/api/profile":
            self._send(read_profile())
        elif path == "/api/env":
            self._send(check_env())
        elif path == "/api/limits":
            self._send({"max": MAX_CONCURRENCY, "recommended": RECOMMENDED_CONCURRENCY,
                        "note": CONCURRENCY_NOTE})
        elif path == "/api/log":
            text = ""
            if LOG.exists():
                text = LOG.read_text(encoding="utf-8", errors="replace")[-16000:]
            self._send({"running": is_running(), "text": text})
        elif path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
        else:
            self._send({"error": "not found"}, 404)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            body = self._body()
        except Exception as e:
            self._send({"error": f"请求格式错误: {e}"}, 400)
            return

        try:
            if path == "/api/parse":
                page_html = body.get("html") or fetch_page(body.get("url", ""))
                meta = parse_html(page_html)
                if not meta:
                    self._send({"error": "没识别到题目。可以点「抓不到？粘贴页面源码」，把问卷页另存的 HTML 贴进来。"})
                    return
                self._send({"meta": meta, "defaults": default_questions(meta),
                            "count": len(meta), "url": body.get("url", "")})

            elif path == "/api/preview":
                prof = body.get("profile") or read_profile()
                s = core.Sampler(profile_dict=prof)
                self._send(core.distribution(s, int(body.get("total", 300))))

            elif path == "/api/validate":
                self._send(validate_profile(body.get("profile") or read_profile()))

            elif path == "/api/save":
                save_profile(body.get("profile") or {})
                self._send({"ok": True})

            elif path == "/api/run":
                env = check_env()
                if not env["ok"]:
                    self._send({"error": env["msg"]})
                    return
                global _proc
                with _proc_lock:
                    if is_running():
                        self._send({"error": "已经在运行了，先点停止"})
                        return
                    save_profile(body.get("profile") or read_profile())
                    LOG.write_text("", encoding="utf-8")
                    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
                    lf = LOG.open("a", encoding="utf-8")
                    _proc = subprocess.Popen(
                        [sys.executable, str(SCRIPT), "--profile", str(PROFILE)],
                        cwd=str(BASE), stdout=lf, stderr=subprocess.STDOUT, env=env,
                    )
                self._send({"ok": True})

            elif path == "/api/stop":
                with _proc_lock:
                    if is_running():
                        _proc.terminate()
                self._send({"ok": True})

            else:
                self._send({"error": "not found"}, 404)
        except Exception as e:
            self._send({"error": f"{type(e).__name__}: {e}"})


def main():
    no_browser = "--no-browser" in sys.argv
    global PORT
    for i, a in enumerate(sys.argv):
        if a == "--port" and i + 1 < len(sys.argv):
            PORT = int(sys.argv[i + 1])

    url = f"http://127.0.0.1:{PORT}"
    print("=" * 52)
    print("  问卷填写工作台已启动")
    print(f"  打开：{url}")
    print("  停止：关掉这个窗口（或按 Ctrl+C）")
    print("=" * 52, flush=True)
    if not no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")


if __name__ == "__main__":
    main()
