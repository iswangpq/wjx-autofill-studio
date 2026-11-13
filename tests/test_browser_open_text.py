# -*- coding: utf-8 -*-
"""开放题端到端验证：真浏览器里跑 APPLY_JS 并回读。

严格离线：拦截所有外部请求，不提交、不碰任何真实问卷。
没装 playwright / 没有浏览器时整套自动跳过，不影响主测试。

单独跑：python -m unittest tests.test_browser_open_text -v
"""
import asyncio
import os
import sys
import unittest
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import wjx_autofill as core            # noqa: E402
import verify_open_text as v            # noqa: E402

# 复合题：一题三个输入框（姓名 / 建议 / 联系方式），另加一道普通开放题
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


def _browser_available():
    try:
        from playwright.async_api import async_playwright
    except Exception:
        return False
    import asyncio

    async def probe():
        async with async_playwright() as p:
            for ch in ("msedge", "chrome", None):
                try:
                    kw = {"channel": ch} if ch else {}
                    b = await p.chromium.launch(headless=True, **kw)
                    await b.close()
                    return True
                except Exception:
                    continue
        return False

    try:
        return asyncio.run(probe())
    except Exception:
        return False


HAVE_BROWSER = _browser_available()


@unittest.skipUnless(HAVE_BROWSER, "没装 playwright 或没有可用浏览器，跳过端到端验证")
class TestBrowserOpenText(unittest.TestCase):
    """真浏览器里验证开放题填写与回读。"""

    def test_composite_question_fills_every_box(self):
        r = v.analyze(COMPOSITE_HTML, "建议X")
        applied, readback = asyncio.run(
            v.verify_dom(COMPOSITE_HTML, r["payload"], r["text_questions"], "建议X"))
        self.assertEqual(applied.get("q2"), "text", f"复合题填写状态异常：{applied}")
        self.assertEqual(readback.get("q2"), "建议X1 | 建议X2 | 建议X3",
                         "复合题三个框必须逐格填上并回读出来")
        self.assertEqual(readback.get("q3"), "建议X", "普通开放题不受影响")

    def test_global_inputs_are_not_touched(self):
        """页面尾部的 matrixinput / clipVoteCode 不能被当成开放题填掉。"""
        r = v.analyze(COMPOSITE_HTML, "建议X")
        applied, _ = asyncio.run(
            v.verify_dom(COMPOSITE_HTML, r["payload"], r["text_questions"], "建议X"))
        self.assertNotIn("matrixinput", applied)
        self.assertNotIn("clipVoteCode", applied)

    def test_single_box_still_returns_plain_string(self):
        r = v.analyze(COMPOSITE_HTML, "建议X")
        self.assertEqual(r["payload"]["ans"]["__text"]["q3"], "建议X")


if __name__ == "__main__":
    unittest.main()
