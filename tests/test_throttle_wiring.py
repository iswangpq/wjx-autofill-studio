# -*- coding: utf-8 -*-
"""集成级验证：wait_result 的风控分支是否真的把信号传给了 AdaptiveLimiter。

不需要浏览器/网络，用假 page 精确触发各分支，验证「接线」而不只是工具类本身。
轮次由 page.url 的访问次数驱动（wait_result 每轮恰好访问一次）， deterministic。
"""
import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import wjx_autofill as core  # noqa: E402
from throttle import AdaptiveLimiter  # noqa: E402

SURVEY_URL = "https://v.wjx.cn/vm/test.aspx"
SUCCESS_URL = "https://v.wjx.cn/wjx/join/completemobile2.aspx?activityid=abc&joinactivity=1"

NOOP = lambda m: None  # noqa: E731


class FakeLocator:
    async def inner_text(self):
        return "大学生熬夜情况调查 1. 您的性别 男 女 提交"

    async def count(self):
        return 1

    async def is_visible(self):
        return True

    async def click(self, **kw):
        return None


class FakePage:
    def __init__(self, urls, hints=None, aliyun_states=None):
        self.urls = list(urls)
        self.hints = list(hints or [None])
        self.aliyun_states = list(aliyun_states or [{"visible": False}])
        self.frames = []
        self._url_hits = 0

    @property
    def url(self):
        i = min(self._url_hits, len(self.urls) - 1)
        self._url_hits += 1
        return self.urls[i]

    @property
    def round(self):
        return max(0, self._url_hits - 1)

    def locator(self, sel):
        return FakeLocator()

    async def evaluate(self, js, *args):
        i = min(self.round, len(self.hints) - 1)
        j = min(self.round, len(self.aliyun_states) - 1)
        if js is core.SAFETY_HINT_JS:
            return self.hints[i]
        if js is core.ALIYUN_STATE_JS:
            return self.aliyun_states[j]
        return None

    async def wait_for_timeout(self, ms):
        await asyncio.sleep(0)   # 不真的等，只让出事件循环

    async def bring_to_front(self):
        return None


def make_limiter():
    return AdaptiveLimiter(3, cooldown=0.01, log=NOOP, poll=0.01)


class TestThrottleWiring(unittest.IsolatedAsyncioTestCase):
    async def run_case(self, page, limiter):
        notes = []
        with patch.object(core, "dismiss_popups", new=AsyncMock(return_value=None)):
            status = await core.wait_result(page, notes, timeout=5, limiter=limiter)
        return status, notes

    async def test_safety_hint_triggers_throttle(self):
        page = FakePage(
            urls=[SURVEY_URL, SUCCESS_URL],
            hints=[{"kind": "recheck", "text": "需要安全校验，请重新提交！"}, None])
        lim = make_limiter()
        status, notes = await self.run_case(page, lim)
        self.assertEqual(status, "ok")
        self.assertEqual(lim.throttles, 1, "风控提示应即时降速一次")
        self.assertEqual(lim.limit, 2)
        self.assertTrue(any("需要安全校验" in n for n in notes))

    async def test_clean_path_does_not_throttle(self):
        page = FakePage(urls=[SUCCESS_URL], hints=[None])
        lim = make_limiter()
        status, _ = await self.run_case(page, lim)
        self.assertEqual(status, "ok")
        self.assertEqual(lim.throttles, 0)
        self.assertEqual(lim.limit, 3)

    async def test_multiple_signals_in_one_task_only_throttle_once(self):
        # 轮0 风控提示，轮1 阿里云验证弹窗，轮2 成功页
        page = FakePage(
            urls=[SURVEY_URL, SURVEY_URL, SUCCESS_URL],
            hints=[{"kind": "recheck", "text": "需要安全校验"}, None, None],
            aliyun_states=[{"visible": False}, {"visible": True, "start": True},
                           {"visible": False}])
        lim = make_limiter()
        status, notes = await self.run_case(page, lim)
        self.assertEqual(status, "ok")
        self.assertEqual(lim.throttles, 1, "同一份里多个信号只能降一次")
        self.assertEqual(lim.limit, 2)
        self.assertTrue(any("安全验证" in n for n in notes))

    async def test_blocked_hint_triggers_throttle(self):
        page = FakePage(urls=[SURVEY_URL],
                        hints=[{"kind": "blocked", "text": "提交频繁"}])
        lim = make_limiter()
        status, _ = await self.run_case(page, lim)
        self.assertEqual(status, "ip_blocked")
        self.assertEqual(lim.throttles, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
