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
