# -*- coding: utf-8 -*-
"""开放题离线自检：给一份问卷页 HTML，验证「识别 → 定位 → 填写」全链路。

严格离线：不联网、不提交、不写任何问卷。外部资源请求会被全部拦截。

用法：
    python verify_open_text.py --html page.html
    python verify_open_text.py --html page.html --text "建议增加午休时间"
    type page.html | python verify_open_text.py --html -

从哪拿 page.html：浏览器打开问卷填写页 → 右键「查看网页源代码」→ 全选另存为 .html。
（也可以直接用工作台里的「用源码识别」框里那份内容。）
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import wjx_autofill as core
import wjx_studio as studio
from validate_profile import validate_profile

SAMPLE_TEXT = "建议增加午休时间"


def analyze(page_html, sample_text=SAMPLE_TEXT):
    """纯函数部分：解析题目、挑出开放题、构造配置并体检。可单测。"""
    meta = studio.parse_html(page_html)
    text_questions = {
        q: m for q, m in meta.items()
        if m.get("kind") == "text" or not (m.get("opts") or [])
    }
    rules = {
        q: ({"mode": "text", "texts": [sample_text], "blank_rate": 0.0}
            if q in text_questions else core.default_question_cfg(m))
        for q, m in meta.items()
    }
    profile = {
        "url": "https://example.invalid/offline-check", "total": 1, "concurrency": 1,
        "bias": 0.0, "sharpness": 2.0,
        "questions_meta": meta, "questions": rules,
    }
    report = validate_profile(profile)
    answers = {"__text": {q: sample_text for q in text_questions}}
    payload = {
        "ans": answers,
        "ids": {q: (m.get("ids") or [f"{q}_{i+1}" for i in range(len(m.get("opts") or []))])
                for q, m in meta.items()},
        "textIds": {q: (m.get("text_ids") or []) for q, m in meta.items()},
    }
    return {"meta": meta, "text_questions": text_questions,
            "profile": profile, "report": report, "payload": payload}


async def verify_dom(page_html, payload, text_questions, sample_text):
    """真实浏览器 DOM 里跑 APPLY_JS 并回读输入框的值。"""
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = None
        for ch in ("msedge", "chrome", None):
            try:
                kw = {"channel": ch} if ch else {}
                browser = await p.chromium.launch(headless=True, **kw)
                break
            except Exception:
                pass
        if browser is None:
            raise RuntimeError("没有可用浏览器（装 Edge / Chrome，或 playwright install chromium）")
        ctx = await browser.new_context()

        async def abort(route):
            await route.abort()

        # 严格离线：任何外部请求都拦掉
        await ctx.route("http://**", abort)
        await ctx.route("https://**", abort)
        page = await ctx.new_page()
        await page.set_content(page_html, wait_until="domcontentloaded")
        applied = await page.evaluate(core.APPLY_JS, payload)

        readback = await page.evaluate(
            """(tids) => {
              const out = {};
              for (const [q, ids] of Object.entries(tids || {})) {
                let el = null;
                for (const id of ids) {
                  const e = document.getElementById(id);
                  if (e && !e.readOnly && !e.disabled) { el = e; break; }
                }
                if (!el) {   // 与 APPLY_JS 的兜底保持一致
                  const num = (q.match(/\\d+/) || [''])[0];
                  const box = document.querySelector("div.field[topic='" + num + "']");
                  if (box) el = box.querySelector('textarea, input[type=text]');
                }
                if (!el) { out[q] = '<找不到元素>'; continue; }
                out[q] = el.value;
              }
              return out;
            }""", payload.get("textIds") or {})
        await ctx.close()
        await browser.close()
        return applied, readback


def main():
    ap = argparse.ArgumentParser(description="开放题离线自检（不联网、不提交）")
    ap.add_argument("--html", required=True, help="问卷页源码 HTML 文件；传 - 表示从 stdin 读")
    ap.add_argument("--text", default=SAMPLE_TEXT, help="用来试填的文本")
    args = ap.parse_args()

    raw = sys.stdin.read() if args.html == "-" else Path(args.html).read_text(encoding="utf-8", errors="replace")
    if not raw.strip():
        print("HTML 为空，没东西可验")
        return 2

    r = analyze(raw, args.text)
    meta, tq, report = r["meta"], r["text_questions"], r["report"]
    print(f"识别到 {len(meta)} 个可填项，其中开放题 {len(tq)} 道")
    for q, m in tq.items():
        tids = m.get("text_ids") or []
        print(f"  {q}  {m.get('title','')[:40]}  text_ids={tids or '（没抓到，将按题号兜底）'}")
    print(f"配置体检：{report['summary']['errors']} error / {report['summary']['warnings']} warning")
    for e in report["errors"]:
        print(f"  ERROR {e['q'] or '全局'}: {e['msg']}")
    for w in report["warnings"]:
        print(f"  WARN  {w['q'] or '全局'}: {w['msg']}")

    if not tq:
        print("\n这份页面里没有识别到开放题，无需继续。")
        return 0

    print("\n浏览器填写验证（headless / 拦截所有外部请求 / 不提交）：")
    applied, readback = asyncio.run(verify_dom(raw, r["payload"], tq, args.text))
    bad = []
    for q in tq:
        status = applied.get(q, "?")
        value = readback.get(q, "<无>")
        ok = status == "text" and value == args.text
        flag = "✓" if ok else "✗"
        print(f"  {flag} {q}  APPLY_JS={status}  回读={value!r}")
        if not ok:
            bad.append(q)
    if bad:
        print(f"\n结论：{len(bad)} 道开放题没能正确填写（{', '.join(bad)}）。")
        print("多半是问卷结构与选择器不匹配 → 把这份 HTML 发给助手校准 parse_html / APPLY_JS。")
        return 1
    print("\n结论：这份问卷页面的开放题能被正确识别并填写。")
    print("注意：这只验证了本地 DOM 填写，服务端是否收到仍需在有权限的测试问卷上核对一次。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
