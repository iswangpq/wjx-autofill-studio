# -*- coding: utf-8 -*-
"""
问卷星【自有测试问卷】批量填写 / 并发测试脚本 —— 题目与重心全部来自 answer_profile.json

一般不用改本文件：打开可视化工作台（双击 启动.bat 或 python wjx_studio.py），
粘问卷链接 -> 自动识别题目 -> 拖滑块调重心 -> 点「开始填写」。

命令行也保留：
    python wjx_autofill.py --preview --bias 0.6 --total 20   # 离线看分布，不提交
    python wjx_autofill.py --bias 0.8 --total 20              # 正式跑

用途边界：仅用于你自己创建、有管理权限的测试问卷，用来造测试数据、
验证题目跳转、必填校验和提交流程。请勿用于他人问卷、刷票、刷量或
影响任何真实调查统计结果。
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import random
import sys
import time
from datetime import datetime
from pathlib import Path

from throttle import AdaptiveLimiter

# playwright 延迟到真正开浏览器时才导入，这样 --preview 不装依赖也能跑

BASE_DIR = Path(__file__).parent
CFG = {
    "URL": "https://v.wjx.cn/vm/eudcN9d.aspx",
    "TOTAL": 20, "CONCURRENCY": 3, "HEADLESS": False, "DRY_RUN": False,
    "MIN_GAP": 1.0, "MAX_GAP": 4.0, "PROXIES": [], "OUT_CSV": "wjx_result.csv",
    "PROFILE": "answer_profile.json", "BIAS_OVERRIDE": None, "SHARP_OVERRIDE": None,
    "VERIFY_TIMEOUT": 45,    # 出现智能验证时，最多等多久（秒）
    "VERIFY_MAX_WAIT": 60,   # 人工过验证的额外宽限，别让一份卡住整个队列
    "VERIFY_GRACE": 30,      # 人工正在过验证时，每次检测到弹窗就顺延这么多秒
    "IDLE_TIMEOUT": 25,      # 页面一直没任何动静，就快速放弃这一份继续下一批
    "MAX_RESUBMIT": 3,       # 提交后没结果时，最多补点几次提交
    "KEEP_WINDOW_ON_FAIL": False,   # 失败时是否保留浏览器窗口（默认否，脚本自己收尾）
    "BROWSER_CHANNEL": "msedge",
    "ADAPTIVE": True,              # 检测到风控/验证时自动降并发、拉长间隔
    "ADAPTIVE_MIN_CONCURRENCY": 1,
    "THROTTLE_COOLDOWN": 6.0,      # 触发后新任务暂停的秒数
    "THROTTLE_GAP_FACTOR": 2.5,    # 间隔放大倍数
    "ADAPTIVE_RECOVER_AFTER": 5,   # 连续几份干净成功后恢复一档
}

# UA 必须和实际内核一致：用 Edge 就别声称 Chrome，反指纹会因此判定高风险
UA_CHROME = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
UA_EDGE = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0")
USER_AGENTS = [UA_CHROME, UA_EDGE]

# 隐藏自动化指纹：验证服务会看 navigator.webdriver / chrome.runtime 等
STEALTH_JS = r"""
(() => {
  try {
    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
  } catch (e) {}
  try {
    Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh']});
  } catch (e) {}
  try {
    Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
  } catch (e) {}
  try {
    if (!window.chrome) {
      window.chrome = {runtime: {}, loadTimes: function(){}, csi: function(){}, app: {}};
    }
  } catch (e) {}
  try {
    const orig = window.permissions && window.permissions.query;
    if (orig) {
      window.permissions.query = (p) => (
        p && p.name === 'notifications'
          ? Promise.resolve({state: Notification.permission})
          : orig.call(window.permissions, p)
      );
    }
  } catch (e) {}
})();
"""

# 仅作为「配置里没有 questions_meta」时的兜底；实际题目一律以 JSON 为准
FALLBACK_META = {
    "q1": {"title": "性别", "kind": "radio", "opts": ["男", "女"]},
    "q2": {"title": "晚上休息时间", "kind": "radio", "opts": ["10:00前", "10-11点", "11-12点", "12-1点", "1点后"]},
    "q3": {"title": "几点算熬夜", "kind": "radio", "opts": ["10点后", "11点后", "12点后", "1点后"]},
    "q4": {"title": "每天睡眠时长", "kind": "radio", "opts": ["<6小时", "6-7小时", "7-8小时", ">8小时"]},
    "q5": {"title": "一周熬夜天数", "kind": "radio", "opts": ["不熬夜", "1-3天", "4-6天", "每天"]},
    "q6": {"title": "熬夜原因(多选)", "kind": "checkbox", "opts": ["学习", "玩游戏", "聊天", "看电影", "失眠"]},
    "q7": {"title": "第二天状态", "kind": "radio", "opts": ["没影响", "昏昏欲睡"]},
}

NEUTRAL_HINT = ("性别", "年龄", "年级", "专业", "学院", "身份", "地区", "城市", "学校", "学历", "是否")
# 开放题默认文案：中性、与任何问卷主题都不冲突，别再冒出「经常熬夜」这种答非所问的内容
GENERIC_TEXTS = ("暂无", "无特别建议", "整体还可以。", "希望后续继续优化。", "测试填写")


def default_question_cfg(m):
    """新抓到的题目给个合理默认值：人口属性题不跟随重心。"""
    n = len(m.get("opts") or [])
    if m["kind"] == "checkbox":
        return {"mode": "multi", "prob": [0.5] * n, "min": 1, "max": min(3, n)}
    if m["kind"] == "text":
        return {"mode": "text", "texts": [], "blank_rate": 0.0}
    if any(k in m["title"] for k in NEUTRAL_HINT):
        return {"mode": "ratio", "ratio": [round(1 / n, 4)] * n}
    return {"mode": "trend", "dir": 1, "strength": 1.0}


class Sampler:
    """按配置生成答案；用缺口补偿抽样，让实际分布紧贴你设的重心 / ratio。"""

    def __init__(self, path=None, meta=None, profile_dict=None):
        self.path = Path(path) if path else None
        self._fixed = profile_dict
        self.profile = {"bias": 0.0, "sharpness": 2.0, "questions": {}, "questions_meta": {}}
        self._last_sig = None
        self._warned = False
        self.lock = asyncio.Lock()
        if self._fixed is not None:
            self.profile = json.loads(json.dumps(self._fixed))
        else:
            self._reload()
        self.meta = meta or self.profile.get("questions_meta") or FALLBACK_META
        self.counts = {q: [0] * len(m["opts"]) for q, m in self.meta.items() if m.get("opts")}
        self.done = 0
        self._last_sig = self._sig()

    def _sig(self):
        return json.dumps([self.profile.get("bias"), self.profile.get("sharpness"),
                           self.profile.get("questions")], sort_keys=True, ensure_ascii=False)

    def _reload(self):
        try:
            p = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(p, dict) and "questions" in p:
                self.profile = p
        except FileNotFoundError:
            if not self._warned:
                print(f"[提示] 没找到 {self.path.name}，用内置默认配置")
                self._warned = True
        except json.JSONDecodeError as e:
            if not self._warned:
                print(f"[提示] {self.path.name} JSON 有误({e})，本次沿用上一次配置")
                self._warned = True
        if CFG["BIAS_OVERRIDE"] is not None:
            self.profile["bias"] = CFG["BIAS_OVERRIDE"]
        if CFG["SHARP_OVERRIDE"] is not None:
            self.profile["sharpness"] = CFG["SHARP_OVERRIDE"]

        sig = self._sig()
        if self._last_sig is not None and sig != self._last_sig:
            # 配置变了就清零累计，让新重心从下一份立刻生效
            self.counts = {q: [0] * len(m["opts"]) for q, m in self.meta.items() if m.get("opts")}
            self.done = 0
            print(f"[重心已更新] bias={self.profile.get('bias')}  sharpness={self.profile.get('sharpness')}", flush=True)
        self._last_sig = sig

    def _weights(self, q, n):
        cfg = self.profile.get("questions", {}).get(q, {})
        mode = cfg.get("mode", "trend")
        if mode == "ratio":
            r = (list(cfg.get("ratio") or [1.0 / n] * n) + [0.0] * n)[:n]
            s = sum(r) or 1.0
            return [x / s for x in r]
        if mode in ("uniform", "off"):
            return [1.0 / n] * n
        bias = float(self.profile.get("bias", 0.0))
        sharp = float(self.profile.get("sharpness", 2.0))
        d = float(cfg.get("dir", 1))
        strength = float(cfg.get("strength", 1.0))
        k = sharp * bias * d * strength
        if n == 1 or abs(k) < 1e-9:
            return [1.0 / n] * n
        w = [math.exp(k * (2 * i / (n - 1) - 1)) for i in range(n)]
        s = sum(w)
        return [x / s for x in w]

    def _pick_one(self, q, n, w):
        base = self.done + 1
        scores = [w[i] * base - self.counts[q][i] + random.uniform(-0.12, 0.12) for i in range(n)]
        i = max(range(n), key=lambda j: scores[j])
        self.counts[q][i] += 1
        return i + 1

    def _pick_multi(self, q, n, cfg):
        prob = (list(cfg.get("prob") or [0.5] * n) + [0.5] * n)[:n]
        lo = max(1, int(cfg.get("min", 1)))
        hi = max(lo, min(n, int(cfg.get("max", min(3, n)))))
        picked = [i + 1 for i in range(n) if random.random() < prob[i]]
        if len(picked) < lo:
            rest = [i + 1 for i in range(n) if i + 1 not in picked]
            picked += random.sample(rest, min(lo - len(picked), len(rest)))
        if len(picked) > hi:
            picked = random.sample(picked, hi)
        for v in picked:
            self.counts[q][v - 1] += 1
        return sorted(picked)

    def _pick_text(self, cfg):
        # 优先级：自定义文本池 > 可选「跟随重心」> 中性通用池
        pool = cfg.get("texts")
        if not pool:
            b = float(self.profile.get("bias", 0.0))
            if cfg.get("follow_bias"):
                if b > 0.2:
                    pool = ["经常熬夜，白天没精神。", "睡得比较晚，状态一般。", "作息不太规律。"]
                elif b < -0.2:
                    pool = ["作息规律，睡眠充足。", "很少熬夜，状态不错。", "早睡早起，挺好。"]
                else:
                    pool = ["一般", "还行", "无", "测试"]
            else:
                pool = list(GENERIC_TEXTS)
        try:
            br = float(cfg.get("blank_rate", 0.0))
        except (TypeError, ValueError):
            br = 0.0
        if br > 0 and random.random() < br:
            return ""      # 允许有意识地留白（非必填开放题）
        return random.choice(pool)

    def _build(self):
        if self._fixed is None:
            self._reload()
        ans, texts = {}, {}
        for q, m in self.meta.items():
            cfg = self.profile.get("questions", {}).get(q, {})
            n = len(m.get("opts") or [])
            if m.get("kind") == "text" or n == 0:
                texts[q] = self._pick_text(cfg)
            elif m["kind"] == "checkbox" or cfg.get("mode") == "multi":
                ans[q] = self._pick_multi(q, n, cfg)
            else:
                ans[q] = self._pick_one(q, n, self._weights(q, n))
        if texts:
            ans["__text"] = texts
        self.done += 1
        return ans

    async def next_answers(self):
        async with self.lock:
            return self._build()


def fmt_answers(a):
    return " ".join(f"{q}={','.join(map(str, v)) if isinstance(v, list) else v}"
                    for q, v in a.items() if q != "__text")


def severity_index(samples, meta, profile):
    """0=最轻/最健康，1=最重/最严重，用来一眼看出整体重心。"""
    vals = []
    for a in samples:
        per = []
        for q, m in meta.items():
            cfg = profile.get("questions", {}).get(q, {})
            if m.get("kind") != "radio" or cfg.get("mode") in ("off", "uniform", "ratio"):
                continue   # 固定比例/不跟随的题（多为性别等人口属性）不算进整体倾向
            n = len(m.get("opts") or [])
            if n < 2 or q not in a:
                continue
            d = float(cfg.get("dir", 1))
            pos = (a[q] - 1) / (n - 1)
            per.append(pos if d > 0 else 1 - pos)
        if per:
            vals.append(sum(per) / len(per))
    return sum(vals) / len(vals) if vals else 0.0


def distribution(sampler, total=200):
    """离线模拟 N 份，返回每题占比 + 整体倾向值（界面和 --preview 共用）。"""
    samples = [sampler._build() for _ in range(total)]
    out = {}
    for q, m in sampler.meta.items():
        c = sampler.counts.get(q)
        if not c:
            out[q] = {"pct": [100.0], "labels": [m.get("title", q)]}
            continue
        s = sum(c) or 1
        out[q] = {"pct": [round(x / s * 100, 1) for x in c],
                  "labels": m.get("opts") or [m.get("title", q)]}
    return {"dist": out, "severity": round(severity_index(samples, sampler.meta, sampler.profile), 3)}


def preview(total, sampler):
    r = distribution(sampler, total)
    print(f"\n=== 离线预览 {total} 份  bias={sampler.profile.get('bias')}  sharpness={sampler.profile.get('sharpness')} ===")
    for q, m in sampler.meta.items():
        d = r["dist"][q]
        print(f"  {q} {m.get('title',''):<14} " +
              " | ".join(f"{d['labels'][i]} {d['pct'][i]:>5.1f}%" for i in range(len(d["pct"]))))
    print(f"  整体倾向值：{r['severity']:.2f}  (0=最轻, 1=最重)")
    print("  不满意就改 bias / sharpness / ratio 再预览，确认后再去掉 --preview 真跑\n")


APPLY_JS = r"""
(payload) => {
  const ans = payload.ans || {};
  const idsMap = payload.ids || {};   // 复杂问卷(矩阵/量表)用抓取到的真实 input id 精确定位
  const out = {};
  const clickOpt = id => {
    const inp = document.getElementById(id);
    if (!inp) return false;
    const lab = document.querySelector('div.label[for="' + CSS.escape(id) + '"]')
             || document.querySelector('label[for="' + CSS.escape(id) + '"]');
    const wrap = inp.parentElement;
    const a = wrap && wrap.querySelector('a.jqradio, a.jqcheckbox');
    if (lab) lab.click();
    else if (a) a.click();
    else inp.click();
    inp.checked = true;
    inp.dispatchEvent(new Event('change', {bubbles: true}));
    return true;
  };
  for (const [q, v] of Object.entries(ans)) {
    if (q === '__text') continue;
    const optId = x => (idsMap[q] && idsMap[q][x - 1]) ? idsMap[q][x - 1] : (q + '_' + x);
    const ids = Array.isArray(v) ? v.map(optId) : [optId(v)];
    out[q] = ids.filter(clickOpt).length + '/' + ids.length;
  }
  // 开放题 / 填空题：优先用抓取到的真实 id 精确定位，找不到再退回题号容器
  const handled = new Set();
  if (ans.__text) {
    const tids = payload.textIds || {};
    for (const [q, val] of Object.entries(ans.__text)) {
      const num = (q.match(/\d+/) || [''])[0];
      let el = null;
      for (const id of (tids[q] || [])) {
        const e = document.getElementById(id);
        if (e) { el = e; break; }
      }
      if (!el) {
        const box = document.querySelector("div.field[topic='" + num + "']");
        if (box) el = box.querySelector('textarea, input[type=text]');
      }
      if (!el) {
        for (const id of [q + '_1', 'q' + num + '_1', 'txt' + num]) {
          const e = document.getElementById(id);
          if (e && (e.tagName === 'TEXTAREA' || e.type === 'text')) { el = e; break; }
        }
      }
      if (!el) { out[q] = '0/1'; continue; }   // 填不中要报出来，不能静默
      handled.add(el);
      if (val === '') { out[q] = 'blank'; continue; }
      el.value = val;
      el.dispatchEvent(new Event('input', {bubbles: true}));
      el.dispatchEvent(new Event('change', {bubbles: true}));
      out[q] = 'text';
    }
  }
  // 兜底：页面上其它可见填空（已精确填过的元素不要被覆盖，尤其别破坏留白）
  document.querySelectorAll('input[type=text], textarea').forEach(el => {
    if (handled.has(el) || el.offsetParent === null || el.disabled || el.readOnly || el.id === 'matrixinput' || el.value) return;
    const hint = ((el.name||'') + ' ' + (el.id||'') + ' ' + (el.placeholder||'')).toLowerCase();
    if (/captcha|verify|验证码/.test(hint)) return;
    let v = '测试';
    if (/phone|mobile|手机|电话/.test(hint)) v = '13' + String(Math.floor(Math.random()*1e9)).padStart(9,'0');
    else if (/mail|邮箱/.test(hint)) v = 'test@example.com';
    else if (/age|年龄|num/.test(hint)) v = '22';
    el.value = v;
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
  });
  return out;
}
"""

# 问卷「智能验证 / 安全验证」相关：这类弹窗不能当成普通提示直接点掉
VERIFY_WORDS = ("安全验证", "智能验证", "请完成验证", "滑动验证", "拖动滑块", "点选验证")
# 成功页实际文案是「您的答卷已经提交，感谢您的参与！」，词表必须覆盖到，否则每份都被误判 timeout
SUCCESS_WORDS = ("提交成功", "答卷已经提交", "答卷已提交", "您的答卷", "问卷已结束",
                 "已完成该问卷", "提交完成", "感谢您的参与", "谢谢参与")
BLOCK_WORDS = ("提交频繁", "同一IP", "同一 IP", "不能重复", "已提交过", "次数过多",
               "超出限制", "提交失败", "限您")
# 提交成功后整页会跳到 /wjx/join/completemobile2.aspx，这个特征比中文文案可靠得多
SUCCESS_URL_MARKS = ("/wjx/join/complete", "completemobile", "complete.aspx", "/wjx/join/complete2")
# 被 IP/频率拦截时不跳 URL，仍靠正文关键词判定
# 阿里云智能验证的弹窗与内部元素
ALIYUN_POPUP = "#aliyunCaptcha-window-popup"
ALIYUN_CHECK = "#aliyunCaptcha-checkbox-icon"
ALIYUN_CLOSE = "#aliyunCaptcha-btn-close"
ALIYUN_REFRESH = "#aliyunCaptcha-btn-refresh"

STATUS_CN = {
    "ok": "提交成功", "need_verify": "需人工验证", "timeout": "等待超时",
    "ip_blocked": "被IP限制", "error": "异常", "dry_run": "仅填写未提交",
    "no_submit_button": "没找到提交按钮", "no_response": "页面无响应(已跳过)",
}


def is_verify_text(t):
    return any(w in t for w in VERIFY_WORDS)


# ---------------- 拟人化鼠标 ----------------
# 直线匀速移动是最明显的机器人特征，这里走随机弧线 + 逐点抖动 + 随机速度。

_MOUSE_POS = {}


def _rand_curve(x0, y0, x1, y1):
    """两点之间的三次贝塞尔控制点，中间带随机弧度，避免走直线。"""
    dx, dy = x1 - x0, y1 - y0
    dist = math.hypot(dx, dy) or 1.0
    nx, ny = -dy / dist, dx / dist          # 法线方向
    bow = random.uniform(0.06, 0.26) * dist * random.choice((-1.0, 1.0))
    c1 = (x0 + dx * 0.25 + nx * bow, y0 + dy * 0.25 + ny * bow)
    c2 = (x0 + dx * 0.72 + nx * bow * 0.45, y0 + dy * 0.72 + ny * bow * 0.45)
    return c1, c2


async def human_move(page, x0, y0, x1, y1, jitter=2.4, min_steps=8, max_steps=26):
    """沿随机弧线把鼠标移到目标点：轨迹弯、速度不匀、末端带微抖。"""
    c1, c2 = _rand_curve(x0, y0, x1, y1)
    steps = random.randint(min_steps, max_steps)
    await page.mouse.move(x0 + random.uniform(-5, 5), y0 + random.uniform(-5, 5))
    await page.wait_for_timeout(random.randint(25, 85))
    for i in range(1, steps + 1):
        t = i / steps
        e = t * t * (3 - 2 * t)                       # 缓入缓出
        e = max(0.0, min(1.0, e + random.uniform(-0.07, 0.07)))
        bx = ((1 - e) ** 3 * x0 + 3 * (1 - e) ** 2 * e * c1[0]
              + 3 * (1 - e) * e * e * c2[0] + e ** 3 * x1)
        by = ((1 - e) ** 3 * y0 + 3 * (1 - e) ** 2 * e * c1[1]
              + 3 * (1 - e) * e * e * c2[1] + e ** 3 * y1)
        j = jitter * (1.0 if e < 0.85 else 0.45)
        await page.mouse.move(bx + random.uniform(-j, j), by + random.uniform(-j, j))
        await page.wait_for_timeout(random.randint(7, 30))
    await page.mouse.move(x1 + random.uniform(-0.9, 0.9), y1 + random.uniform(-0.9, 0.9))


async def human_click(page, target, pad=0.32):
    """拟人化点击。target 可以是 Locator，也可以是 (x, y)。点击点在元素内随机取，不走正中。"""
    if hasattr(target, "bounding_box"):
        box = await target.bounding_box()
        if not box:
            return False
        cx = box["x"] + box["width"] / 2
        cy = box["y"] + box["height"] / 2
        tx = cx + random.uniform(-1, 1) * box["width"] * pad
        ty = cy + random.uniform(-1, 1) * box["height"] * pad
    else:
        tx, ty = target
    key = id(page)
    x0, y0 = _MOUSE_POS.get(key, (tx, ty))
    await human_move(page, x0, y0, tx, ty)
    await page.mouse.down()
    await page.wait_for_timeout(random.randint(45, 115))   # 按下到抬起有停顿
    await page.mouse.up()
    _MOUSE_POS[key] = (tx, ty)
    return True


# 阿里云智能验证弹窗的状态探针
# 注意：弹窗是 position:fixed，offsetParent 恒为 null，绝不能用它判可见！
ALIYUN_STATE_JS = r"""
() => {
  const pop = document.querySelector('#aliyunCaptcha-window-popup');
  if (!pop) return {exists:false, visible:false, text:''};
  const st = getComputedStyle(pop);
  const r = pop.getBoundingClientRect();
  const visible = st.display !== 'none' && st.visibility !== 'hidden'
                  && st.opacity !== '0' && r.width > 50 && r.height > 20
                  && (st.position === 'fixed' || pop.offsetParent !== null);
  const txt = (pop.innerText || '').trim();
  const has = s => txt.indexOf(s) >= 0;
  const ifr = [...pop.querySelectorAll('iframe')].filter(f => {
    const b = f.getBoundingClientRect();
    return b.width > 20 && b.height > 20;
  });
  // 注意：不能用 .aliyunCaptcha-checkbox-checked 判断通过，那只是容器自带的样式类
  return {
    exists: true, visible, text: txt.slice(0, 160),
    start: has('点击开始智能验证') || has('开始智能验证'),
    fail: has('验证失败') || has('请刷新'),
    success: has('验证成功') || has('通过验证'),
    loading: !!pop.querySelector('#aliyunCaptcha-loading'),
    challengeFrame: ifr.length > 0
  };
}
"""

# 问卷星风控提示：这类 layui 弹层不能当普通提示点掉就算完
SAFETY_HINT_JS = r"""
() => {
  const els = document.querySelectorAll('.layui-layer-content, [class*="dialog"] [class*="content"], '
    + '[class*="popup"] [class*="content"], .jqBox, [class*="tips"]');
  for (const el of els) {
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden') continue;
    const t = (el.innerText || '').trim();
    if (!t || t.length > 120) continue;
    if (/需要安全校验|请重新提交|安全校验/.test(t)) return {kind:'recheck', text:t};
    if (/提交频繁|同一\s*IP|不能重复|已提交过|次数过多|超出限制|稍后再试|提交失败/.test(t))
      return {kind:'blocked', text:t};
  }
  return null;
}
"""

# 问卷星的验证入口一般是个复选框「点击开始智能验证」，点了才弹出滑块 / 点选
VERIFY_START_JS = r"""
() => {
  const words = ['点击开始智能验证','开始智能验证','智能验证','点击开始验证','安全验证'];
  const vis = e => e.offsetParent !== null;
  for (const cb of document.querySelectorAll('input[type=checkbox]')) {
    if (!vis(cb)) continue;
    const wrap = cb.closest('div') || cb.parentElement || cb;
    const t = (wrap.innerText || '');
    if (words.some(w => t.includes(w))) { cb.click(); return 'checkbox:' + t.trim().slice(0, 24); }
  }
  // 问卷星用的是自绘的方框（不是真 checkbox），找带文字的容器点一下
  for (const el of document.querySelectorAll('[class*=checkbox],[class*=check],[class*=tick],label,.label,[class*=verify]')) {
    if (!vis(el)) continue;
    const t = (el.innerText || '').trim();
    if (t && t.length < 40 && words.some(w => t.includes(w))) { el.click(); return 'box:' + t.slice(0, 24); }
  }
  for (const el of document.querySelectorAll('span,div,label,p,a')) {
    if (!vis(el)) continue;
    const t = (el.innerText || '').trim();
    if (t && t.length < 30 && words.some(w => t.includes(w))) { el.click(); return 'text:' + t.slice(0, 24); }
  }
  return null;
}
"""

DISMISS_JS = r"""
() => {
  const words = ['确定','确认','我知道了','知道了','继续','继续提交','关闭','OK','好'];
  const sels = '.layui-layer, [class*="popup"], [class*="Popup"], [class*="dialog"], ' +
               '[class*="Dialog"], [class*="modal"], [class*="Modal"], ' +
               '[id*="alert"], [id*="Alert"], #mask, .jqBox, [class*="tips"]';
  for (const box of document.querySelectorAll(sels)) {
    const st = getComputedStyle(box);
    if (st.display === 'none' || st.visibility === 'hidden') continue;
    if (box.offsetParent === null && st.position !== 'fixed') continue;
    if (/安全验证|智能验证|请完成验证|滑动验证|验证码/.test(box.innerText || '')) continue;  // 验证弹窗不能当普通提示点掉
    const btns = [...box.querySelectorAll('a,button,input[type=button],span,div')]
      .filter(e => words.includes(((e.innerText||'') + (e.value||'')).trim()) && e.offsetParent !== null)
      .sort((a,b) => (a.tagName === 'DIV' ? 1 : 0) - (b.tagName === 'DIV' ? 1 : 0));
    if (btns.length) {
      const t = ((btns[0].innerText||'') + (btns[0].value||'')).trim();
      btns[0].click();
      return t;
    }
  }
  return null;
}
"""


async def _accept_dialog(dialog, notes):
    try:
        notes.append(f"原生弹窗[{dialog.message[:60]}]已点确定")
        await dialog.accept()
    except Exception:
        pass


async def dismiss_popups(page, notes):
    for _ in range(4):
        try:
            txt = await page.evaluate(DISMISS_JS)
        except Exception:
            break
        if not txt:
            break
        notes.append(f"弹窗[{txt}]已点确定")
        await page.wait_for_timeout(300)


SLIDER_SELS = [".tc_slider", ".slide", ".slider", ".drag", ".verify_move",
               "[class*=slider]", "[class*=slide]", "[class*=drag]", "[class*=move]",
               "[class*=btn_slide]", ".btn_slide"]
TRACK_SELS = ["[class*=slide_box]", "[class*=slider_box]", "[class*=slideBar]",
              "[class*=bg]", ".tc_slider_bg", ".verify-container", "[class*=container]",
              "[class*=track]", "[class*=bar]"]


async def try_auto_slider(page, notes):
    """在所有 frame 里找滑块，用拟人轨迹拖到底。部分验证能直接过，过不了就交给你手动点。"""
    for frame in page.frames:
        try:
            el = None
            for s in SLIDER_SELS:
                loc = frame.locator(s).first
                if await loc.count():
                    bb = await loc.bounding_box()
                    if bb and 10 < bb["width"] < 120:
                        el = loc
                        break
            if el is None:
                continue
            eb = await el.bounding_box()
            if not eb or eb["width"] < 5:
                continue
            tb = None
            for s in TRACK_SELS:
                tr = frame.locator(s).first
                if await tr.count():
                    tb = await tr.bounding_box()
                    if tb:
                        break
            x0, y0 = eb["x"] + eb["width"] / 2, eb["y"] + eb["height"] / 2
            span = (tb["width"] - eb["width"]) if tb else eb["width"] * 3
            key = id(page)
            mx, my = _MOUSE_POS.get(key, (x0, y0))
            await human_move(page, mx, my, x0, y0, jitter=1.6)
            await page.mouse.down()
            await page.wait_for_timeout(random.randint(60, 140))
            steps = random.randint(16, 30)
            for i in range(1, steps + 1):
                t = i / steps
                e = t * t * (3 - 2 * t)
                e = max(0.0, min(1.0, e + random.uniform(-0.05, 0.05)))
                px = x0 + span * e + random.uniform(-2.2, 2.2)
                py = y0 + random.uniform(-2.6, 2.6) * (1 - e)
                await page.mouse.move(px, py)
                await page.wait_for_timeout(random.randint(9, 26))
            over = random.uniform(2, 12)
            await page.mouse.move(x0 + span + over, y0 + random.uniform(-1.5, 1.5))
            await page.wait_for_timeout(random.randint(40, 110))
            await page.mouse.move(x0 + span, y0)
            await page.wait_for_timeout(random.randint(60, 150))
            await page.mouse.up()
            _MOUSE_POS[key] = (x0 + span, y0)
            notes.append("已尝试自动拖动验证滑块")
            return True
        except Exception:
            continue
    return False


async def handle_captcha(page, notes, state):
    """处理阿里云智能验证弹窗。

    1) 报「验证失败，请刷新」-> 点刷新 / 重来
    2) 显示「点击开始智能验证」-> 拟人化真实点击进去
    """
    if state.get("fail"):
        for sel in (ALIYUN_REFRESH, ALIYUN_CHECK, "#aliyunCaptcha-checkbox-icon"):
            loc = page.locator(sel)
            try:
                if await loc.count() and await loc.is_visible():
                    await human_click(page, loc)
                    notes.append(f"验证失败，已点刷新重来({sel})")
                    await page.wait_for_timeout(1400)
                    return
            except Exception:
                continue
    if state.get("start"):
        for sel in (ALIYUN_CHECK, "#aliyunCaptcha-checkbox-body",
                    "#aliyunCaptcha-checkbox-text", "#aliyunCaptcha-checkbox-element"):
            loc = page.locator(sel)
            try:
                if await loc.count() and await loc.is_visible():
                    await human_click(page, loc)
                    notes.append(f"已点击「开始智能验证」({sel})")
                    await page.wait_for_timeout(1100)
                    return
            except Exception:
                continue


async def wait_result(page, notes, timeout=150, limiter=None):
    """提交后等结果。判定优先级：URL 跳转 > 风控提示 > 正文文案 > 验证弹窗。

    - 成功页一定跳到 /wjx/join/completemobile2.aspx，比中文文案可靠
    - 「需要安全校验，请重新提交！」是问卷星风控提示：点确认后必须立刻重提交，
      重提交才会弹阿里云智能验证，不能点掉就撒手不管
    - 阿里云弹窗 -> 自动点 + 自动重试，实在过不了等你手动过
    - 一直没动静就快速放弃，不干等几分钟
    """
    hard_deadline = time.time() + timeout + CFG["VERIFY_MAX_WAIT"]
    started = time.time()
    verify_clicked = False
    warned = False
    resubmit = 0
    last_slider_try = 0
    click_round = 0
    last_progress = started      # 最近一次「有进展」的时刻
    last_try = started

    def _signal(reason):
        # 即时上报，不等这份结束，好让其它并发任务马上减速
        if limiter:
            limiter.throttle(reason)

    while time.time() < hard_deadline:
        # 1) URL 判定：主 frame 和所有子 frame 都看
        try:
            urls = [page.url] + [f.url for f in page.frames]
        except Exception:
            urls = []
        if any(m in u for u in urls for m in SUCCESS_URL_MARKS):
            return "ok"

        # 2) 正文判定（兜底）
        try:
            body = (await page.locator("body").inner_text())[:1500]
        except Exception:
            await page.wait_for_timeout(800)
            continue
        if any(k in body for k in SUCCESS_WORDS):
            return "ok"
        if any(k in body for k in BLOCK_WORDS):
            return "ip_blocked"

        # 3) 问卷星风控提示（layui 弹层）
        try:
            hint = await page.evaluate(SAFETY_HINT_JS)
        except Exception:
            hint = None
        if hint:
            last_progress = time.time()
            if hint.get("kind") == "blocked":
                notes.append(f"被限制[{hint.get('text','')}]")
                _signal("IP/频率限制")
                return "ip_blocked"
            notes.append(f"需要安全校验[{hint.get('text','')}]，点确认后重提交")
            _signal("问卷星要求安全校验")
            await dismiss_popups(page, notes)
            await page.wait_for_timeout(400)
            try:
                b = page.locator("#ctlNext")
                if await b.count() and await b.is_visible():
                    await b.click(timeout=4000)
                    notes.append("已按提示重新提交")
            except Exception:
                pass
            await page.wait_for_timeout(900)
            continue

        # 4) 阿里云智能验证弹窗
        try:
            state = await page.evaluate(ALIYUN_STATE_JS)
        except Exception:
            state = {"exists": False, "visible": False}
        if state.get("visible"):
            verify_clicked = True
            last_progress = time.time()
            # 弹窗还在就一定要去点它；「通过」以弹窗消失为准，别用 CSS 类猜
            click_round += 1
            if click_round <= 15:
                await handle_captcha(page, notes, state)
            if time.time() - last_slider_try > 5:
                last_slider_try = time.time()
                await try_auto_slider(page, notes)
            if not warned:
                warned = True
                notes.append("检测到安全验证，正在自动尝试；没过的话请在窗口里手动过一次")
                _signal("阿里云智能验证")
                try:
                    await page.bring_to_front()
                except Exception:
                    pass
            await page.wait_for_timeout(1000)
            continue

        # 5) 没弹窗也没结果 -> 主动重试提交（首次 2 秒，之后每 6 秒）
        gap = 2 if resubmit == 0 else 6
        if resubmit < CFG["MAX_RESUBMIT"] and time.time() - last_try > gap:
            last_try = time.time()
            resubmit += 1
            try:
                b = page.locator("#ctlNext")
                if await b.count() and await b.is_visible():
                    await b.click(timeout=4000)
                    notes.append(f"未收到提交结果，第 {resubmit} 次重试提交")
                    last_progress = time.time()
                    await page.wait_for_timeout(1200)
                    continue
            except Exception:
                pass

        # 6) 一直没进展就快速放弃，别把并发卡死几分钟
        if time.time() - last_progress > CFG["IDLE_TIMEOUT"]:
            notes.append(f"页面 {CFG['IDLE_TIMEOUT']} 秒无响应，已跳过这一份")
            _signal("页面无响应")
            return "no_response"
        await page.wait_for_timeout(900)

    return "need_verify" if verify_clicked else "timeout"


async def submit_one(idx, browser, limiter, sampler, results):
    async with limiter:
        notes, started, status = [], datetime.now(), "unknown"
        answers = await sampler.next_answers()
        as_text = fmt_answers(answers)
        ids_map = {q: (m.get("ids") or [f"{q}_{i + 1}" for i in range(len(m.get("opts") or []))])
                   for q, m in sampler.meta.items()}
        text_ids_map = {q: (m.get("text_ids") or []) for q, m in sampler.meta.items()}
        payload = {"ans": answers, "ids": ids_map, "textIds": text_ids_map}
        proxy ={"server": CFG["PROXIES"][(idx - 1) % len(CFG["PROXIES"])]} if CFG["PROXIES"] else None
        ua = UA_EDGE if CFG["BROWSER_CHANNEL"] == "msedge" else UA_CHROME
        ctx = await browser.new_context(user_agent=ua,
                                        viewport={"width": 1366, "height": 900},
                                        locale="zh-CN", timezone_id="Asia/Shanghai",
                                        proxy=proxy)
        await ctx.add_init_script(STEALTH_JS)
        page = await ctx.new_page()
        page.on("dialog", lambda d: asyncio.create_task(_accept_dialog(d, notes)))
        try:
            await page.goto(CFG["URL"], wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(1100)
            await dismiss_popups(page, notes)
            applied = await page.evaluate(APPLY_JS, payload)
            missing = [q for q, v in applied.items() if isinstance(v, str) and v.startswith("0/")]
            if missing:
                notes.append(f"页面未找到这些题的选项，问卷可能改版: {','.join(missing)}")
            await page.wait_for_timeout(350)
            for _ in range(10):
                btn = page.locator("#ctlNext")
                if not await btn.count() or not await btn.is_visible():
                    status = "no_submit_button"
                    notes.append("没找到 #ctlNext，需按页面校准提交按钮")
                    break
                label = (await btn.inner_text()).strip()
                if "下一页" in label or "下一题" in label:
                    await btn.click(timeout=3000)
                    await page.wait_for_timeout(900)
                    await page.evaluate(APPLY_JS, payload)
                    await dismiss_popups(page, notes)
                    continue
                if CFG["DRY_RUN"]:
                    status = "dry_run"
                    notes.append("DRY_RUN：只填未提交")
                    break
                await btn.click(timeout=5000)
                await page.wait_for_timeout(1200)
                # 「需要安全校验，请重新提交！」：点掉确认后必须立刻重提交，
                # 重提交才会弹阿里云验证，不能点掉就撒手不管
                try:
                    hint = await page.evaluate(SAFETY_HINT_JS)
                except Exception:
                    hint = None
                if hint and hint.get("kind") == "recheck":
                    notes.append(f"风控提示[{hint.get('text','')}]")
                    if limiter:
                        limiter.throttle("问卷星要求安全校验")
                    await dismiss_popups(page, notes)
                    await page.wait_for_timeout(400)
                    try:
                        await page.locator("#ctlNext").click(timeout=4000)
                        notes.append("已按提示重新提交")
                    except Exception:
                        pass
                else:
                    await dismiss_popups(page, notes)
                status = await wait_result(page, notes, timeout=CFG["VERIFY_TIMEOUT"], limiter=limiter)
                break
        except Exception as e:
            status = "error"
            notes.append(f"{type(e).__name__}: {str(e)[:160]}")
        finally:
            limiter.finish(status)
            # 窗口一律由脚本自己收尾：留着不关的话你得手动关，还挡着下一份
            keep = (status in ("need_verify", "timeout") and not CFG["HEADLESS"]
                    and CFG["KEEP_WINDOW_ON_FAIL"])
            if keep:
                notes.append("浏览器窗口已保留，请手动过完验证后自行关闭")
            else:
                try:
                    await ctx.close()
                except Exception:
                    pass
            row = {"序号": idx, "时间": started.strftime("%H:%M:%S"), "状态": status,
                   "耗时秒": round((datetime.now() - started).total_seconds(), 1),
                   "答案": as_text, "备注": " | ".join(dict.fromkeys(notes))}
            results.append(row)
            cn = STATUS_CN.get(status, status)
            print(f"[{idx:>3}/{CFG['TOTAL']}] {cn:<10} {row['耗时秒']:>5}s  {as_text}  {row['备注'][:80]}", flush=True)
            await asyncio.sleep(random.uniform(*limiter.gap()))


async def run(sampler):
    from playwright.async_api import async_playwright

    results = []
    async with async_playwright() as p:
        # 优先用本机 Edge / Chrome，省掉下载 Chromium；都没有再回退 Playwright 自带浏览器
        browser, last_err = None, None
        for channel in ("msedge", "chrome", None):
            try:
                kw = {"channel": channel} if channel else {}
                browser = await p.chromium.launch(
                    headless=CFG["HEADLESS"],
                    args=["--disable-blink-features=AutomationControlled",
                          "--disable-features=IsolateOrigins,site-per-process",
                          "--start-maximized"],
                    ignore_default_args=["--enable-automation"],
                    **kw)
                CFG["BROWSER_CHANNEL"] = channel or "chromium"
                print(f"[浏览器] 使用 {'本机 ' + channel if channel else 'Playwright Chromium'}", flush=True)
                break
            except Exception as e:
                last_err = e
        if browser is None:
            raise RuntimeError(f"没找到可用浏览器（{last_err}）。执行 playwright install chromium，或安装 Edge / Chrome")
        limiter = AdaptiveLimiter(
            CFG["CONCURRENCY"],
            min_limit=CFG["ADAPTIVE_MIN_CONCURRENCY"],
            cooldown=CFG["THROTTLE_COOLDOWN"],
            throttle_gap_factor=CFG["THROTTLE_GAP_FACTOR"],
            recover_after=CFG["ADAPTIVE_RECOVER_AFTER"],
            base_min_gap=CFG["MIN_GAP"], base_max_gap=CFG["MAX_GAP"],
            enabled=CFG["ADAPTIVE"])
        mode = "风控触发时自动降速" if limiter.enabled else "已关闭（固定并发 + 固定间隔）"
        print(f"[自适应] 初始并发 {limiter.limit}，{mode}", flush=True)
        await asyncio.gather(
            *(submit_one(i + 1, browser, limiter, sampler, results) for i in range(CFG["TOTAL"])),
            return_exceptions=True)
        await browser.close()
    print(f"[自适应统计] {limiter.snapshot()}", flush=True)
    results.sort(key=lambda r: r["序号"])
    out = BASE_DIR / CFG["OUT_CSV"]
    with out.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["序号", "时间", "状态", "耗时秒", "答案", "备注"])
        w.writeheader()
        w.writerows(results)
    print(f"\n完成：{sum(1 for r in results if r['状态']=='ok')}/{len(results)} 份判定为提交成功，逐份答案见 {out}", flush=True)


def main():
    ap = argparse.ArgumentParser(description="问卷星自有测试问卷批量填写（题目与重心都在 JSON 里）")
    ap.add_argument("--bias", type=float, help="整体重心 -1.0(最轻) ~ +1.0(最重)")
    ap.add_argument("--sharpness", type=float)
    ap.add_argument("--total", type=int)
    ap.add_argument("--concurrency", type=int)
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-adaptive", action="store_true",
                    help="关闭自适应节流，回到固定并发 + 固定间隔（便于 A/B 对比）")
    ap.add_argument("--preview", action="store_true", help="只离线看分布，不开浏览器")
    ap.add_argument("--profile", help="配置文件，默认 answer_profile.json")
    args = ap.parse_args()

    if args.profile:
        CFG["PROFILE"] = args.profile
    path = Path(CFG["PROFILE"])
    if not path.is_absolute():
        path = BASE_DIR / path
    if args.bias is not None:
        CFG["BIAS_OVERRIDE"] = max(-1.0, min(1.0, args.bias))
    if args.sharpness is not None:
        CFG["SHARP_OVERRIDE"] = max(0.0, min(8.0, args.sharpness))

    sampler = Sampler(path)
    p = sampler.profile
    CFG["URL"] = p.get("url") or CFG["URL"]
    if args.total:
        CFG["TOTAL"] = args.total
    elif p.get("total"):
        CFG["TOTAL"] = int(p["total"])
    if args.concurrency:
        CFG["CONCURRENCY"] = args.concurrency
    elif p.get("concurrency"):
        CFG["CONCURRENCY"] = int(p["concurrency"])
    if args.headless or p.get("headless"):
        CFG["HEADLESS"] = True
    if args.dry_run:
        CFG["DRY_RUN"] = True
    if args.no_adaptive:
        CFG["ADAPTIVE"] = False
    if p.get("verify_timeout"):
        CFG["VERIFY_TIMEOUT"] = max(20, int(p["verify_timeout"]))

    if args.preview:
        preview(CFG["TOTAL"], sampler)
        return
    asyncio.run(run(sampler))


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    main()
