# -*- coding: utf-8 -*-
"""零依赖回归测试：python -m unittest discover -s tests -v

只测纯逻辑，不开浏览器、不联网、不碰真实问卷。
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import wjx_autofill as core          # noqa: E402
import wjx_studio as studio         # noqa: E402
import validate_profile as vp  # noqa: E402
from validate_profile import validate_profile as validate  # noqa: E402


def profile(meta=None, rules=None, **kw):
    p = {
        "url": "https://v.wjx.cn/vm/test.aspx",
        "total": 20, "concurrency": 3, "headless": False,
        "bias": 0.6, "sharpness": 2.0,
        "questions_meta": meta if meta is not None else {
            "q1": {"title": "程度", "kind": "radio", "opts": ["轻", "中", "重"],
                   "ids": ["q1_1", "q1_2", "q1_3"]},
        },
        "questions": rules if rules is not None else {
            "q1": {"mode": "trend", "dir": 1, "strength": 1.0},
        },
    }
    p.update(kw)
    return p


class TestSampler(unittest.TestCase):
    def setUp(self):
        self.s = core.Sampler(profile_dict=profile())

    def test_default_cfg_neutral_title(self):
        cfg = core.default_question_cfg({"title": "您的性别", "kind": "radio", "opts": ["男", "女"]})
        self.assertEqual(cfg["mode"], "ratio")
        self.assertEqual(cfg["ratio"], [0.5, 0.5])

    def test_default_cfg_checkbox(self):
        cfg = core.default_question_cfg({"title": "原因", "kind": "checkbox", "opts": ["a", "b", "c"]})
        self.assertEqual(cfg["mode"], "multi")
        self.assertEqual(cfg["max"], 3)

    def test_default_cfg_text(self):
        self.assertEqual(core.default_question_cfg({"title": "建议", "kind": "text", "opts": []})["mode"], "text")

    def test_weights_bias_zero_is_uniform(self):
        s = core.Sampler(profile_dict=profile(bias=0.0))
        w = s._weights("q1", 3)
        self.assertAlmostEqual(w[0], w[1], places=6)
        self.assertAlmostEqual(w[1], w[2], places=6)

    def test_weights_dir_positive_prefers_last(self):
        self.assertGreater(self.s._weights("q1", 3)[-1], self.s._weights("q1", 3)[0])

    def test_weights_dir_negative_prefers_first(self):
        s = core.Sampler(profile_dict=profile(
            rules={"q1": {"mode": "trend", "dir": -1, "strength": 1.0}}))
        self.assertGreater(s._weights("q1", 3)[0], s._weights("q1", 3)[-1])

    def test_weights_sharpness_zero_uniform(self):
        s = core.Sampler(profile_dict=profile(sharpness=0.0))
        w = s._weights("q1", 3)
        self.assertAlmostEqual(sum(w), 1.0, places=6)
        self.assertAlmostEqual(w[0], w[2], places=6)

    def test_weights_ratio_normalized(self):
        s = core.Sampler(profile_dict=profile(
            rules={"q1": {"mode": "ratio", "ratio": [1, 1, 2]}}))
        w = s._weights("q1", 3)
        self.assertAlmostEqual(sum(w), 1.0, places=6)
        self.assertAlmostEqual(w[2], 0.5, places=6)

    def test_weights_off_is_uniform(self):
        s = core.Sampler(profile_dict=profile(rules={"q1": {"mode": "off"}}))
        self.assertAlmostEqual(s._weights("q1", 3)[0], 1 / 3, places=6)

    def test_ratio_exact_quota(self):
        meta = {"q1": {"title": "程度", "kind": "radio",
                       "opts": ["a", "b", "c", "d", "e"], "ids": []}}
        s = core.Sampler(profile_dict=profile(
            meta=meta, rules={"q1": {"mode": "ratio", "ratio": [0, 0, 0.2, 0.3, 0.5]}}))
        for _ in range(20):
            s._build()
        self.assertEqual(s.counts["q1"], [0, 0, 4, 6, 10])

    def test_pick_multi_respects_min_max(self):
        meta = {"q1": {"title": "原因", "kind": "checkbox",
                       "opts": ["a", "b", "c", "d", "e"], "ids": []}}
        cfg = {"mode": "multi", "prob": [0.0] * 5, "min": 2, "max": 3}
        s = core.Sampler(profile_dict=profile(meta=meta, rules=cfg))
        for _ in range(30):
            got = s._pick_multi("q1", 5, cfg)
            self.assertTrue(2 <= len(got) <= 3)
            self.assertEqual(got, sorted(set(got)))
            self.assertTrue(all(1 <= v <= 5 for v in got))

    def test_text_pool_custom_is_exact(self):
        s = core.Sampler(profile_dict=profile())
        for _ in range(20):
            self.assertEqual(s._pick_text({"texts": ["固定建议"]}), "固定建议")

    def test_text_pool_default_is_neutral(self):
        # 换问卷也不能冒出「经常熬夜」这类主题相关、答非所问的文案
        pool = {core.Sampler(profile_dict=profile(bias=0.9))._pick_text({}) for _ in range(40)}
        self.assertTrue(pool)
        self.assertTrue(pool.issubset(set(core.GENERIC_TEXTS)))
        self.assertFalse(any("熬夜" in t for t in pool))

    def test_text_pool_blank_rate(self):
        s = core.Sampler(profile_dict=profile())
        self.assertEqual(s._pick_text({"texts": ["x"], "blank_rate": 1.0}), "")
        self.assertEqual(s._pick_text({"texts": ["x"], "blank_rate": 0.0}), "x")

    def test_text_pool_follow_bias_is_opt_in(self):
        heavy = {core.Sampler(profile_dict=profile(bias=0.9))._pick_text({"follow_bias": True}) for _ in range(40)}
        light = {core.Sampler(profile_dict=profile(bias=-0.9))._pick_text({"follow_bias": True}) for _ in range(40)}
        self.assertTrue(heavy.isdisjoint(light))
        self.assertTrue(any("熬夜" in t for t in heavy))

    def test_severity_excludes_ratio_and_off(self):
        p = profile(rules={"q1": {"mode": "ratio", "ratio": [1, 0, 0]}})
        s = core.Sampler(profile_dict=p)
        samples = [s._build() for _ in range(30)]
        self.assertEqual(core.severity_index(samples, s.meta, p), 0.0)

    def test_severity_trend_tracks_bias(self):
        def sev(bias):
            p = profile(bias=bias)
            s = core.Sampler(profile_dict=p)
            samples = [s._build() for _ in range(200)]
            return core.severity_index(samples, s.meta, p)
        self.assertLess(sev(-0.9), sev(0.9))

    def test_distribution_shape(self):
        p = profile()
        s = core.Sampler(profile_dict=p)
        d = core.distribution(s, 50)
        self.assertEqual(len(d["dist"]["q1"]["pct"]), 3)
        self.assertAlmostEqual(sum(d["dist"]["q1"]["pct"]), 100.0, delta=0.2)
        self.assertTrue(0 <= d["severity"] <= 1)

    def test_hot_reload_resets_counters(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "p.json"
            p = profile(bias=-0.8)
            path.write_text(json.dumps(p, ensure_ascii=False), encoding="utf-8")
            s = core.Sampler(path)
            with contextlib.redirect_stdout(io.StringIO()):
                for _ in range(10):
                    s._build()
            self.assertEqual(s.done, 10)
            p["bias"] = 0.8
            path.write_text(json.dumps(p, ensure_ascii=False), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                s._build()
            self.assertEqual(s.done, 1, "配置变化后累计应清零")
            self.assertEqual(sum(s.counts["q1"]), 1)
            self.assertAlmostEqual(s.profile["bias"], 0.8)

    def test_broken_json_keeps_last_config(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "p.json"
            path.write_text(json.dumps(profile(bias=0.3), ensure_ascii=False), encoding="utf-8")
            s = core.Sampler(path)
            path.write_text("{broken", encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                s._reload()
            self.assertAlmostEqual(s.profile["bias"], 0.3)


class TestValidate(unittest.TestCase):
    def test_valid_profile(self):
        r = validate(profile())
        self.assertTrue(r["ok"], r["errors"])
        self.assertEqual(r["summary"]["errors"], 0)

    def test_orphan_rule_is_error(self):
        p = profile()
        p["questions"]["q99"] = {"mode": "trend", "dir": 1}
        r = validate(p)
        self.assertFalse(r["ok"])
        self.assertIn("orphan_rule", [e["code"] for e in r["errors"]])

    def test_missing_rule_is_warning(self):
        p = profile()
        del p["questions"]["q1"]
        r = validate(p)
        self.assertTrue(r["ok"])
        self.assertIn("missing_rule", [w["code"] for w in r["warnings"]])

    def test_ratio_length_mismatch(self):
        p = profile(rules={"q1": {"mode": "ratio", "ratio": [0.5, 0.5]}})
        self.assertIn("ratio_length", [e["code"] for e in validate(p)["errors"]])

    def test_ratio_all_zero(self):
        p = profile(rules={"q1": {"mode": "ratio", "ratio": [0, 0, 0]}})
        self.assertIn("ratio_all_zero", [e["code"] for e in validate(p)["errors"]])

    def test_ratio_negative(self):
        p = profile(rules={"q1": {"mode": "ratio", "ratio": [-1, 1, 1]}})
        self.assertIn("ratio_negative", [e["code"] for e in validate(p)["errors"]])

    def test_bad_dir_and_mode(self):
        p = profile(rules={"q1": {"mode": "trend", "dir": 7}})
        self.assertIn("bad_dir", [e["code"] for e in validate(p)["errors"]])
        p = profile(rules={"q1": {"mode": "nonsense"}})
        self.assertIn("bad_mode", [e["code"] for e in validate(p)["errors"]])

    def test_meta_ids_mismatch(self):
        p = profile()
        p["questions_meta"]["q1"]["ids"] = ["q1_1"]
        self.assertIn("meta_ids_mismatch", [e["code"] for e in validate(p)["errors"]])

    def test_top_level_invalid(self):
        p = profile(url="abc", bias=9, total=0, concurrency="3")
        codes = [e["code"] for e in validate(p)["errors"]]
        for c in ("url_invalid", "bias_out_of_range", "total_invalid", "concurrency_invalid"):
            self.assertIn(c, codes)

    def test_direction_hint_is_fact_not_guess(self):
        r = validate(profile())
        self.assertEqual(r["hints"][0]["focus"], "重心偏重时倾向最后一个选项「重」")
        p = profile(rules={"q1": {"mode": "trend", "dir": -1, "strength": 1.0}})
        self.assertEqual(validate(p)["hints"][0]["focus"], "重心偏重时倾向第一个选项「轻」")

    def test_ratio_is_reported_as_bias_locked(self):
        p = profile(rules={"q1": {"mode": "ratio", "ratio": [1, 1, 1]}})
        r = validate(p)
        self.assertIn("q1", r["summary"]["bias_locked"])
        self.assertIn("ratio_locks_bias", [w["code"] for w in r["warnings"]])

    def test_non_dict_input(self):
        self.assertFalse(validate([1, 2])["ok"])


SAMPLE_HTML = """
<div class='field ui-field-contain' topic='1' type='1'>
  <div class='topichtml'>1. 您的性别：</div>
  <input type='radio' value='1' id='q1_1' name='q1'><div class='label' for='q1_1'>男</div>
  <input type='radio' value='2' id='q1_2' name='q1'><div class='label' for='q1_2'>女</div>
</div>
<div class='field ui-field-contain' topic='2' type='2'>
  <div class='topichtml'>2. 熬夜原因？【多选题】</div>
  <input type='checkbox' value='1' id='q2_1' name='q2'><div class='label' for='q2_1'>学习</div>
  <input type='checkbox' value='2' id='q2_2' name='q2'><div class='label' for='q2_2'>失眠</div>
</div>
<div class='field ui-field-contain' topic='3' type='3'>
  <div class='topichtml'>3. 补充说明</div>
  <textarea id='q3_1' name='q3'></textarea>
</div>
"""


class TestStudio(unittest.TestCase):
    def test_parse_html_three_kinds(self):
        meta = studio.parse_html(SAMPLE_HTML)
        self.assertEqual(set(meta), {"q1", "q2", "q3"})
        self.assertEqual(meta["q1"]["kind"], "radio")
        self.assertEqual(meta["q1"]["opts"], ["男", "女"])
        self.assertEqual(meta["q1"]["ids"], ["q1_1", "q1_2"])
        self.assertEqual(meta["q2"]["kind"], "checkbox")
        self.assertEqual(meta["q3"]["kind"], "text")
        self.assertEqual(meta["q3"]["text_ids"], ["q3_1"], "开放题要抓到真实输入框 id")

    def test_parse_html_empty(self):
        self.assertEqual(studio.parse_html("<html><body>无题目</body></html>"), {})

    def test_save_profile_is_atomic(self):
        with tempfile.TemporaryDirectory() as td:
            old = studio.PROFILE
            studio.PROFILE = Path(td) / "answer_profile.json"
            try:
                studio.save_profile(profile(bias=0.42))
                self.assertTrue(studio.PROFILE.exists())
                self.assertAlmostEqual(json.loads(studio.PROFILE.read_text(encoding="utf-8"))["bias"], 0.42)
                self.assertFalse((Path(td) / "answer_profile.json.tmp").exists())
            finally:
                studio.PROFILE = old

    def test_read_profile_fallback_when_missing(self):
        with tempfile.TemporaryDirectory() as td:
            old = studio.PROFILE
            studio.PROFILE = Path(td) / "nope.json"
            try:
                d = studio.read_profile()
                self.assertEqual(d["total"], 20)
                self.assertEqual(d["questions_meta"], {})
            finally:
                studio.PROFILE = old


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestNewFeatures(unittest.TestCase):
    """并发上限 + 开放题校验。"""

    def test_limits_constants(self):
        self.assertEqual(vp.MAX_CONCURRENCY, 4)
        self.assertEqual(vp.RECOMMENDED_CONCURRENCY, 3)
        self.assertIn("公网 IP", vp.CONCURRENCY_NOTE)

    def test_concurrency_above_max_is_error(self):
        r = validate(profile(concurrency=5))
        self.assertIn("concurrency_invalid", [e["code"] for e in r["errors"]])
        self.assertTrue(validate(profile(concurrency=4))["ok"])

    def test_text_pool_validation(self):
        meta = {"q8": {"title": "你的建议", "kind": "text", "opts": [],
                       "ids": [], "text_ids": ["q8_1"]}}
        rules = {"q8": {"mode": "text", "texts": ["建议A", "建议B"], "blank_rate": 0.0}}
        r = validate(profile(meta=meta, rules=rules))
        self.assertTrue(r["ok"], r["errors"])

    def test_text_pool_empty_warns(self):
        meta = {"q8": {"title": "你的建议", "kind": "text", "opts": [],
                       "ids": [], "text_ids": ["q8_1"]}}
        r = validate(profile(meta=meta, rules={"q8": {"mode": "text"}}))
        self.assertIn("text_pool_empty", [w["code"] for w in r["warnings"]])

    def test_text_pool_bad_and_blank_rate(self):
        meta = {"q8": {"title": "建议", "kind": "text", "opts": [],
                       "ids": [], "text_ids": ["q8_1"]}}
        r = validate(profile(meta=meta, rules={"q8": {"mode": "text", "texts": [""], "blank_rate": 3}}))
        codes = [e["code"] for e in r["errors"]]
        self.assertIn("text_pool_bad", codes)
        self.assertIn("blank_rate_bad", codes)

    def test_text_meta_without_id_warns(self):
        meta = {"q8": {"title": "建议", "kind": "text", "opts": [], "ids": []}}
        r = validate(profile(meta=meta, rules={"q8": {"mode": "text", "texts": ["x"]}}))
        self.assertIn("meta_text_no_id", [w["code"] for w in r["warnings"]])


class TestConcurrencyCap(unittest.TestCase):
    """命令行/配置并发必须和界面上限一致。"""

    def cap(self, value, allow=False):
        with contextlib.redirect_stdout(io.StringIO()):
            return core.resolve_concurrency(value, allow)

    def test_within_limit_unchanged(self):
        for n in (1, 2, 3, 4):
            self.assertEqual(self.cap(n), n)

    def test_above_limit_clamps(self):
        self.assertEqual(self.cap(8), vp.MAX_CONCURRENCY)
        self.assertEqual(self.cap(99), vp.MAX_CONCURRENCY)

    def test_allow_over_keeps_value(self):
        self.assertEqual(self.cap(8, allow=True), 8)

    def test_below_one_and_invalid(self):
        self.assertEqual(self.cap(0), 1)
        self.assertEqual(self.cap(-5), 1)
        self.assertEqual(self.cap("abc"), 3)

    def test_clamp_prints_warning(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            core.resolve_concurrency(8)
        self.assertIn("并发收敛", buf.getvalue())


class TestVerifyOpenText(unittest.TestCase):
    """开放题离线自检的纯函数部分。"""

    def test_analyze_detects_text_question_and_ids(self):
        import verify_open_text as v
        r = v.analyze(SAMPLE_HTML, "建议A")
        self.assertIn("q3", r["text_questions"])
        self.assertEqual(r["text_questions"]["q3"]["text_ids"], ["q3_1"])
        self.assertEqual(r["payload"]["ans"]["__text"]["q3"], "建议A")
        self.assertEqual(r["payload"]["textIds"]["q3"], ["q3_1"])

    def test_analyze_profile_has_no_errors(self):
        import verify_open_text as v
        r = v.analyze(SAMPLE_HTML, "建议A")
        self.assertEqual(r["report"]["summary"]["errors"], 0, r["report"]["errors"])

    def test_analyze_no_open_text(self):
        import verify_open_text as v
        r = v.analyze("<div class='field ui-field-contain' topic='1'>"
                      "<div class='topichtml'>1. 性别</div>"
                      "<input type='radio' id='q1_1' name='q1'>"
                      "<div class='label' for='q1_1'>男</div>"
                      "<input type='radio' id='q1_2' name='q1'>"
                      "<div class='label' for='q1_2'>女</div></div>", "x")
        self.assertEqual(r["text_questions"], {})
        self.assertEqual(r["payload"]["ans"]["__text"], {})


MULTI_TEXT_HTML = """<html><body>
<div class='field ui-field-contain' topic='1'><div class='topichtml'>1. 您的性别：</div>
 <input type='radio' id='q1_1' name='q1'><div class='label' for='q1_1'>男</div>
 <input type='radio' id='q1_2' name='q1'><div class='label' for='q1_2'>女</div></div>
<div class='field ui-field-contain' topic='2'><div class='topichtml'>2. 你的建议？（中间位置）</div>
 <textarea id='q2_1' name='q2'></textarea></div>
<div class='field ui-field-contain' topic='3'><div class='topichtml'>3. 几点睡：</div>
 <input type='radio' id='q3_1' name='q3'><div class='label' for='q3_1'>10点</div>
 <input type='radio' id='q3_2' name='q3'><div class='label' for='q3_2'>12点</div></div>
<div class='field ui-field-contain' topic='4'><div class='topichtml'>4. 原因【多选】：</div>
 <input type='checkbox' id='q4_1' name='q4'><div class='label' for='q4_1'>学习</div>
 <input type='checkbox' id='q4_2' name='q4'><div class='label' for='q4_2'>游戏</div></div>
<div class='field ui-field-contain' topic='5'><div class='topichtml'>5. 有什么好想法？（中间位置）</div>
 <input type='text' id='q5' name='q5'></div>
<div class='field ui-field-contain' topic='6'><div class='topichtml'>6. 熬夜天数：</div>
 <input type='radio' id='q6_1' name='q6'><div class='label' for='q6_1'>不熬夜</div>
 <input type='radio' id='q6_2' name='q6'><div class='label' for='q6_2'>每天</div></div>
<div class='field ui-field-contain' topic='7'><div class='topichtml'>7. 精神状态：</div>
 <input type='radio' id='q7_1' name='q7'><div class='label' for='q7_1'>没影响</div>
 <input type='radio' id='q7_2' name='q7'><div class='label' for='q7_2'>困</div></div>
<div class='field ui-field-contain' topic='8'><div class='topichtml'>8. 补充说明（末尾）：</div>
 <input type='text' id='q8' name='q8'></div>
<input type="text" placeholder="请注明..." id="matrixinput" />
<input type="text" id="clipVoteCode" readonly="readonly"/>
</body></html>"""


class TestOpenTextReuse(unittest.TestCase):
    """开放题位置无关、数量无关：第 2/5/8 题各一道，尾部还有全局输入框。"""

    def test_three_text_questions_at_any_position(self):
        meta = studio.parse_html(MULTI_TEXT_HTML)
        found = [(q, m.get("topic")) for q, m in meta.items() if m["kind"] == "text"]
        self.assertEqual(found, [("q2", "2"), ("q5", "5"), ("q8", "8")])

    def test_text_ids_are_per_question_and_globals_filtered(self):
        meta = studio.parse_html(MULTI_TEXT_HTML)
        self.assertEqual(meta["q2"]["text_ids"], ["q2_1"])
        self.assertEqual(meta["q5"]["text_ids"], ["q5"])
        self.assertEqual(meta["q8"]["text_ids"], ["q8"],
                         "末尾开放题不能把 matrixinput / clipVoteCode 收进来")

    def test_non_text_questions_keep_their_options(self):
        meta = studio.parse_html(MULTI_TEXT_HTML)
        self.assertEqual(meta["q1"]["opts"], ["男", "女"])
        self.assertEqual(meta["q4"]["kind"], "checkbox")

    def test_sampler_generates_text_for_every_question(self):
        import verify_open_text as v
        r = v.analyze(MULTI_TEXT_HTML, "建议X")
        s = core.Sampler(profile_dict=r["profile"])
        ans = s._build()
        self.assertEqual(set(ans["__text"]), {"q2", "q5", "q8"})
        self.assertTrue(all(x == "建议X" for x in ans["__text"].values()))

    def test_analyze_payload_and_report_cover_all(self):
        import verify_open_text as v
        r = v.analyze(MULTI_TEXT_HTML, "建议X")
        self.assertEqual(set(r["payload"]["ans"]["__text"]), {"q2", "q5", "q8"})
        self.assertEqual(r["report"]["summary"]["errors"], 0, r["report"]["errors"])


# 复合题：同一题块里多个输入框（姓名 + 建议 + 联系方式）
COMPOSITE_HTML = """<html><body>
<div class='field ui-field-contain' topic='1'><div class='topichtml'>1. 您的性别：</div>
 <input type='radio' id='q1_1' name='q1'><div class='label' for='q1_1'>男</div>
 <input type='radio' id='q1_2' name='q1'><div class='label' for='q1_2'>女</div></div>
<div class='field ui-field-contain' topic='2'><div class='topichtml'>2. 请填写个人信息</div>
 <input type='text' id='q2_1' name='q2_1'><span>姓名</span>
 <input type='text' id='q2_2' name='q2_2'><span>建议</span>
 <input type='text' id='q2_3' name='q2_3'><span>联系方式</span></div>
<div class='field ui-field-contain' topic='3'><div class='topichtml'>3. 普通开放题</div>
 <textarea id='q3_1' name='q3'></textarea></div>
<input type="text" placeholder="请注明..." id="matrixinput" />
<input type="text" id="clipVoteCode" readonly="readonly"/>
</body></html>"""


class TestCompositeText(unittest.TestCase):
    """复合题：同一题多个输入框，每个框都要填上自己的文案。"""

    def test_parse_detects_slot_count(self):
        meta = studio.parse_html(COMPOSITE_HTML)
        self.assertEqual(meta["q2"]["text_ids"], ["q2_1", "q2_2", "q2_3"])
        self.assertEqual(meta["q2"]["slot_count"], 3)
        self.assertEqual(meta["q3"]["slot_count"], 1, "单框题不该被当成复合题")

    def test_sampler_returns_one_value_per_slot(self):
        import verify_open_text as v
        r = v.analyze(COMPOSITE_HTML, "建议X")
        s = core.Sampler(profile_dict=r["profile"])
        ans = s._build()
        val = ans["__text"]["q2"]
        self.assertIsInstance(val, list, "复合题必须是一框一值的列表")
        self.assertEqual(len(val), 3)
        self.assertEqual(val, ["建议X1", "建议X2", "建议X3"])
        self.assertEqual(ans["__text"]["q3"], "建议X", "单框题仍是字符串，不改既有格式")

    def test_slot_texts_pick_independent_pools(self):
        meta = studio.parse_html(COMPOSITE_HTML)
        p = profile(meta=meta, rules={
            "q1": {"mode": "ratio", "ratio": [0.5, 0.5]},
            "q2": {"mode": "text", "texts": ["通用"],
                   "slot_texts": [["张三", "李四"], ["建议很好"], ["13800000000"]]},
            "q3": {"mode": "text", "texts": ["普通建议"]},
        })
        s = core.Sampler(profile_dict=p)
        val = s._build()["__text"]["q2"]
        self.assertEqual(len(val), 3)
        self.assertIn(val[0], ["张三", "李四"])
        self.assertEqual(val[1], "建议很好")
        self.assertEqual(val[2], "13800000000")

    def test_slot_missing_pool_falls_back_to_shared(self):
        """只配了第 1 栏，第 2/3 栏应退回共用文案池，不能空着。"""
        meta = studio.parse_html(COMPOSITE_HTML)
        p = profile(meta=meta, rules={
            "q1": {"mode": "ratio", "ratio": [0.5, 0.5]},
            "q2": {"mode": "text", "texts": ["通用文案"],
                   "slot_texts": [["专属文案"], []]},
            "q3": {"mode": "text", "texts": ["普通建议"]},
        })
        s = core.Sampler(profile_dict=p)
        val = s._build()["__text"]["q2"]
        self.assertEqual(val[0], "专属文案")
        self.assertEqual(val[1], "通用文案")
        self.assertEqual(val[2], "通用文案")

    def test_blank_rate_blanks_all_slots(self):
        meta = studio.parse_html(COMPOSITE_HTML)
        p = profile(meta=meta, rules={
            "q1": {"mode": "ratio", "ratio": [0.5, 0.5]},
            "q2": {"mode": "text", "texts": ["x"], "blank_rate": 1.0},
            "q3": {"mode": "text", "texts": ["普通建议"]},
        })
        s = core.Sampler(profile_dict=p)
        self.assertEqual(s._build()["__text"]["q2"], ["", "", ""])

    def test_single_slot_keeps_string_for_backward_compat(self):
        s = core.Sampler(profile_dict=profile())
        s.meta = {"q9": {"title": "建议", "kind": "text", "opts": [], "ids": [],
                         "text_ids": ["q9"], "slot_count": 1}}
        s.profile["questions"]["q9"] = {"mode": "text", "texts": ["只有一句"]}
        self.assertEqual(s._build()["__text"]["q9"], "只有一句")

    def test_validate_warns_when_composite_has_no_slot_texts(self):
        meta = studio.parse_html(COMPOSITE_HTML)
        p = profile(meta=meta, rules={
            "q1": {"mode": "ratio", "ratio": [0.5, 0.5]},
            "q2": {"mode": "text", "texts": ["通用"]},
            "q3": {"mode": "text", "texts": ["普通建议"]},
        })
        r = validate(p)
        codes = [w["code"] for w in r["warnings"]]
        self.assertIn("composite_text_no_slots", codes)

    def test_validate_rejects_bad_slot_texts(self):
        meta = studio.parse_html(COMPOSITE_HTML)
        p = profile(meta=meta, rules={
            "q1": {"mode": "ratio", "ratio": [0.5, 0.5]},
            "q2": {"mode": "text", "texts": ["通用"], "slot_texts": [["ok"], "不是数组"]},
            "q3": {"mode": "text", "texts": ["普通建议"]},
        })
        r = validate(p)
        codes = [e["code"] for e in r["errors"]]
        self.assertIn("slot_texts_item_bad", codes)

    def test_apply_js_fills_every_box_of_composite_question(self):
        """复合题必须一框一值（端到端浏览器验证见 test_browser_open_text.py）。"""
        import verify_open_text as v
        r = v.analyze(COMPOSITE_HTML, "建议X")
        payload = r["payload"]
        vals = payload["ans"]["__text"]["q2"]
        self.assertEqual(vals, ["建议X1", "建议X2", "建议X3"])
        self.assertEqual(payload["textIds"]["q2"], ["q2_1", "q2_2", "q2_3"])

    def test_readback_shares_locator_with_apply(self):
        """回读与填写必须用同一套定位函数，否则会出现「填了但读不到」。"""
        self.assertIn("findTextEls", core.APPLY_JS)
        self.assertIn("findTextEls", core.READBACK_JS)
        self.assertIn(core.TEXT_FIND_JS, core.APPLY_JS)
        self.assertIn(core.TEXT_FIND_JS, core.READBACK_JS)
