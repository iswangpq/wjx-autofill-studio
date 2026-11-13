# -*- coding: utf-8 -*-
"""配置体检：静态校验 answer_profile.json，找出换问卷后的规则失配与不会被重心影响的题。

纯函数、零依赖、不 import wjx_autofill（避免循环依赖）。
刻意**不猜选项语义**：方向只做「确定事实」的描述——按当前 bias/sharpness/dir
推导出重心偏重时实际会倾向第几个选项，由人核对，不自动判定对错、也不自动改配置。
"""
from __future__ import annotations

VALID_MODES = {"trend", "ratio", "multi", "off", "uniform", "text"}
VALID_KINDS = {"radio", "checkbox", "text"}

# 「同时开几个」的单一数据源：界面下拉、后端校验、体检提示都读这里
RECOMMENDED_CONCURRENCY = 3
MAX_CONCURRENCY = 4
CONCURRENCY_NOTE = (
    "推荐 3。上限 4：所有窗口共用同一公网 IP，并发越高单位时间提交越密，"
    "问卷星/阿里云越容易弹安全验证；实测并发≥5 时验证几乎每份必现，"
    "自适应节流会把有效并发压回 1~4，单份耗时反而从约 4 秒涨到 11~15 秒。"
    "想真正跑更高并发，需在问卷设置里关掉 IP 限制/智能验证，或配代理池。"
)


def _num(v, lo, hi):
    """把值转成 float 并检查范围；不合法返回 None，不抛异常。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if isinstance(v, bool) or not lo <= f <= hi:
        return None
    return f


def validate_profile(profile):
    """返回 {ok, errors, warnings, hints, summary}。

    error  = 确定的结构/取值问题，配置跑起来会出错或答错题
    warn   = 能跑但值得注意（规则缺失、被锁死、题型与模式不匹配等）
    hints  = 每题 trend 的方向事实，供人工核对
    """
    errors, warnings, hints = [], [], []

    def add(level, q, code, msg):
        (errors if level == "error" else warnings).append(
            {"level": level, "q": q, "code": code, "msg": msg})

    if not isinstance(profile, dict):
        return {"ok": False, "errors": [{"level": "error", "q": None,
                "code": "profile_not_dict", "msg": "配置不是 JSON 对象"}],
                "warnings": [], "hints": [], "summary": {"errors": 1, "warnings": 0}}

    meta = profile.get("questions_meta")
    rules = profile.get("questions")
    if not isinstance(meta, dict):
        add("error", None, "meta_missing", "questions_meta 缺失或不是对象，请先抓取题目")
        meta = {}
    if not isinstance(rules, dict):
        add("error", None, "rules_missing", "questions 缺失或不是对象")
        rules = {}

    # ---- 顶层运行参数 ----
    url = profile.get("url")
    if not url:
        add("warn", None, "url_empty", "还没填问卷链接，真跑会用代码里的默认地址")
    elif not (isinstance(url, str) and url.startswith(("http://", "https://"))):
        add("error", None, "url_invalid", "问卷链接要以 http:// 或 https:// 开头")

    for key, code, lo, hi in (("total", "total_invalid", 1, 10000),
                              ("concurrency", "concurrency_invalid", 1, MAX_CONCURRENCY)):
        v = profile.get(key)
        if v is not None and (not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi):
            tip = f"（上限 {hi}；{CONCURRENCY_NOTE}）" if key == "concurrency" else ""
            add("error", None, code, f"{key} 要是 {lo}~{hi} 的整数，当前 {v!r}{tip}")

    bias = _num(profile.get("bias", 0.0), -1.0, 1.0)
    sharp = _num(profile.get("sharpness", 2.0), 0.0, 8.0)
    if bias is None:
        add("error", None, "bias_out_of_range", "bias 要是 -1.0 ~ 1.0 的数字")
        bias = 0.0
    if sharp is None:
        add("error", None, "sharpness_out_of_range", "sharpness 要是 0 ~ 8 的数字")
        sharp = 2.0

    if not meta:
        add("warn", None, "meta_empty", "还没识别到题目，界面第 1 步先粘链接抓取")

    # ---- 题目结构 ----
    for q, m in meta.items():
        if not isinstance(m, dict):
            add("error", q, "meta_bad_item", "题目结构不是对象")
            continue
        kind = m.get("kind")
        if kind not in VALID_KINDS:
            add("error", q, "meta_bad_kind", f"未知题型 {kind!r}，只支持 radio/checkbox/text")
        opts = m.get("opts")
        if not isinstance(opts, list):
            add("error", q, "meta_opts_bad", "opts 要是数组")
            opts = []
        ids = m.get("ids") or []
        if ids and len(ids) != len(opts):
            add("error", q, "meta_ids_mismatch",
                f"真实 input id 有 {len(ids)} 个，但选项有 {len(opts)} 个，答题会点不中")
        if kind == "radio" and len(opts) < 2:
            add("warn", q, "meta_radio_single", "单选题少于 2 个选项，无法体现重心")
        if kind == "text" and opts:
            add("warn", q, "meta_text_with_opts", "填空题却有选项，会被当成文本填写")
        if kind == "text" and not (m.get("text_ids") or []):
            add("warn", q, "meta_text_no_id",
                "开放题没抓到输入框 id，只能按题号容器猜位置；建议点「用源码识别」重新抓一次")

    # ---- 规则与题目的对应关系 ----
    for q in sorted(set(rules) - set(meta)):
        add("error", q, "orphan_rule",
            "配置里有这题的规则，但题目已不存在（换问卷后题号重排），规则不会生效")
    for q in sorted(set(meta) - set(rules)):
        add("warn", q, "missing_rule", "这题没有规则，运行时会退回默认（trend dir=1）")

    # ---- 每题规则 ----
    counts = {"trend": 0, "ratio": 0, "multi": 0, "off": 0, "uniform": 0, "text": 0, "other": 0}
    locked = []
    for q, cfg in rules.items():
        if q not in meta:
            continue
        if not isinstance(cfg, dict):
            add("error", q, "rule_bad_item", "规则不是对象")
            continue
        m = meta[q]
        n = len(m.get("opts") or [])
        kind = m.get("kind")
        mode = cfg.get("mode", "trend")
        counts[mode if mode in counts else "other"] += 1

        if mode not in VALID_MODES:
            add("error", q, "bad_mode", f"未知模式 {mode!r}")
            continue

        if mode == "ratio":
            r = cfg.get("ratio")
            if not isinstance(r, list) or not r:
                add("error", q, "ratio_missing", "ratio 模式要有非空 ratio 数组")
            else:
                if n and len(r) != n:
                    add("error", q, "ratio_length", f"ratio 有 {len(r)} 项，但题目有 {n} 个选项")
                if any((not isinstance(x, (int, float))) or isinstance(x, bool) for x in r):
                    add("error", q, "ratio_non_numeric", "ratio 里有非数字")
                elif any(x < 0 for x in r):
                    add("error", q, "ratio_negative", "ratio 不能有负数")
                elif sum(r) <= 0:
                    add("error", q, "ratio_all_zero", "ratio 全为 0，这题永远选不出来")
            locked.append(q)
            add("warn", q, "ratio_locks_bias",
                "固定比例：不受整体重心影响，也不计入「整体倾向值」")

        elif mode in ("off", "uniform"):
            locked.append(q)
            add("warn", q, "off_locks_bias",
                "不跟随：不受整体重心影响，也不计入「整体倾向值」")

        elif mode == "trend":
            d = cfg.get("dir", 1)
            if d not in (1, -1):
                add("error", q, "bad_dir", f"dir 只能是 1 或 -1，当前 {d!r}")
                d = 1
            strength = _num(cfg.get("strength", 1.0), 0.0, 8.0)
            if strength is None:
                add("error", q, "bad_strength", "strength 要是 0 ~ 8 的数字")
                strength = 1.0
            if kind == "checkbox":
                add("warn", q, "checkbox_as_trend", "多选题用 trend 只会选一项，建议改成「多选概率」")
            if kind == "text":
                add("warn", q, "text_as_trend", "填空题用 trend 无效，建议改成 text")
            # 方向事实：k>0 权重随序号递增，k<0 递减，k=0 均匀。不猜语义。
            k = sharp * 1.0 * d * strength
            opts = m.get("opts") or []
            if k > 1e-9 and opts:
                focus = f"重心偏重时倾向最后一个选项「{opts[-1]}」"
            elif k < -1e-9 and opts:
                focus = f"重心偏重时倾向第一个选项「{opts[0]}」"
            else:
                focus = "当前参数下各选项接近均匀"
            hints.append({"q": q, "dir": d, "bias": bias, "focus": focus,
                          "title": m.get("title", q)})

        elif mode == "multi":
            prob = cfg.get("prob") or []
            if n and len(prob) != n:
                add("error", q, "multi_prob_length", f"prob 有 {len(prob)} 项，但题目有 {n} 个选项")
            if any((not isinstance(x, (int, float))) or isinstance(x, bool) or not 0 <= x <= 1
                   for x in prob):
                add("error", q, "multi_prob_range", "prob 每项要是 0 ~ 1 的数字")
            lo = cfg.get("min", 1)
            hi = cfg.get("max", min(3, n or 3))
            if (not isinstance(lo, int) or not isinstance(hi, int)
                    or lo < 1 or hi < lo or (n and hi > n)):
                add("error", q, "multi_bounds", f"min/max 不合法（min={lo}, max={hi}, 选项数={n}）")
            if kind != "checkbox":
                add("warn", q, "multi_on_radio", "单选题用了多选模式，提交时可能只认一个值")

        elif mode == "text":
            if kind != "text":
                add("warn", q, "text_on_choice", "选择题用了 text 模式，会被当成填空处理")
            texts = cfg.get("texts")
            if texts is None or texts == []:
                add("warn", q, "text_pool_empty",
                    "开放题没配自定义文案，将使用中性通用短句；建议按题意在界面里填几行")
            elif not isinstance(texts, list) or any(not isinstance(x, str) or not x.strip() for x in texts):
                add("error", q, "text_pool_bad", "texts 要是非空字符串数组（每行一条）")
            br = _num(cfg.get("blank_rate", 0.0), 0.0, 1.0)
            if br is None:
                add("error", q, "blank_rate_bad", "blank_rate（留白比例）要是 0 ~ 1 的数字")
            tids = m.get("text_ids")
            if tids and not (isinstance(tids, list) and all(isinstance(x, str) for x in tids)):
                add("error", q, "text_ids_bad", "text_ids 要是字符串数组（抓取到的输入框 id）")

            # 复合题（一题多个输入框）：每个框一条文案
            slots = int(m.get("slot_count") or len(tids or []) or 1)
            slot_texts = cfg.get("slot_texts")
            if slot_texts is not None:
                if not isinstance(slot_texts, list):
                    add("error", q, "slot_texts_bad", "slot_texts 要是数组（每个输入框一条文案列表）")
                else:
                    for i, pool in enumerate(slot_texts):
                        if not isinstance(pool, list) or any(
                                not isinstance(x, str) or not x.strip() for x in pool):
                            add("error", q, "slot_texts_item_bad",
                                f"slot_texts 第 {i + 1} 项要是非空字符串数组（每行一条）")
                    if len(slot_texts) > slots:
                        add("warn", q, "slot_texts_too_many",
                            f"配了 {len(slot_texts)} 栏文案，但这题只有 {slots} 个输入框，多余的不会生效")
            if slots > 1 and not slot_texts:
                add("warn", q, "composite_text_no_slots",
                    f"这题有 {slots} 个输入框（复合题），但没按框单独配文案；"
                    "所有框会用同一批文案随机填，可能出现「姓名和建议填了同一句」")

    summary = {
        "questions": len(meta), "modes": counts, "bias_locked": locked,
        "trend_hints": len(hints), "errors": len(errors), "warnings": len(warnings),
    }
    return {"ok": not errors, "errors": errors, "warnings": warnings,
            "hints": hints, "summary": summary}
