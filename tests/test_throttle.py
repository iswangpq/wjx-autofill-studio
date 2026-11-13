# -*- coding: utf-8 -*-
"""AdaptiveLimiter 单测：纯 asyncio，不联网、不开浏览器。"""
import asyncio
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import throttle as th  # noqa: E402
from throttle import AdaptiveLimiter  # noqa: E402

NOOP = lambda m: None  # noqa: E731


def make(**kw):
    opts = dict(cooldown=0.05, poll=0.01, log=NOOP)
    opts.update(kw)
    return AdaptiveLimiter(**opts)


class TestAdaptiveLimiter(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        th._task_throttled.set(False)

    async def test_initial_concurrency_cap(self):
        lim = make(initial=2, enabled=False)
        peak = 0

        async def worker():
            nonlocal peak
            async with lim:
                peak = max(peak, lim.active)
                await asyncio.sleep(0.03)

        await asyncio.gather(*(worker() for _ in range(6)))
        self.assertLessEqual(peak, 2)
        self.assertEqual(lim.active, 0)

    async def test_context_manager_releases_on_exception(self):
        lim = make(initial=1, enabled=False)
        with self.assertRaises(ValueError):
            async with lim:
                raise ValueError("boom")
        self.assertEqual(lim.active, 0)

    def test_throttle_reduces_limit_and_gap(self):
        lim = make(initial=3, throttle_gap_factor=2.5, base_min_gap=1.0, base_max_gap=4.0)
        self.assertTrue(lim.throttle("验证弹窗"))
        self.assertEqual(lim.limit, 2)
        self.assertEqual(lim.gap(), (2.5, 10.0))
        self.assertGreater(lim.cooldown_until, time.monotonic())
        self.assertEqual(lim.throttles, 1)

    def test_throttle_dedup_within_one_task(self):
        lim = make(initial=3)
        self.assertTrue(lim.throttle("安全校验"))
        self.assertFalse(lim.throttle("验证弹窗"))
        self.assertFalse(lim.throttle("状态=ok"))
        self.assertEqual(lim.limit, 2, "同一份的多个信号只能降一次")

    async def test_different_tasks_each_throttle_once(self):
        lim = make(initial=3)

        async def worker():
            lim.throttle("风控")

        await asyncio.gather(*(worker() for _ in range(3)))
        self.assertEqual(lim.limit, 1, "三份各自降一次，3→1（受 min_limit 限制）")
        self.assertEqual(lim.throttles, 3)

    def test_finish_clean_recovers_after_n(self):
        lim = make(initial=3, recover_after=3)
        lim.throttle("风控")          # 3→2
        lim.finish("ok")              # 清掉本份标记，干净计数从 0 开始
        for _ in range(2):
            lim.finish("ok")
        self.assertEqual(lim.limit, 2)
        lim.finish("ok")              # 第 3 次干净成功触发恢复
        self.assertEqual(lim.limit, 3)
        self.assertEqual(lim.recovers, 1)
        self.assertEqual(lim.gap(), (1.0, 4.0))

    def test_finish_after_throttle_resets_clean_streak(self):
        lim = make(initial=3, recover_after=2)
        for _ in range(1):
            lim.finish("ok")          # streak=1
        lim.throttle("风控")
        self.assertEqual(lim.clean_streak, 0)
        lim.finish("ok")              # 这份被标记过，不累加
        self.assertEqual(lim.clean_streak, 0)

    def test_finish_non_ok_throttles_when_no_signal_yet(self):
        lim = make(initial=3)
        lim.finish("no_response")
        self.assertEqual(lim.limit, 2)
        self.assertEqual(lim.throttles, 1)

    def test_non_risk_status_does_not_throttle(self):
        lim = make(initial=3)
        for s in ("dry_run", "no_submit_button"):
            lim.finish(s)
        self.assertEqual(lim.limit, 3)
        self.assertEqual(lim.throttles, 0)
        self.assertEqual(lim.clean_streak, 0)

    def test_every_risk_status_is_covered(self):
        for s in th.THROTTLE_STATUSES:
            th._task_throttled.set(False)   # 真实运行时每份是独立 Task，这里模拟新的一份
            lim = make(initial=3)
            lim.finish(s)
            self.assertEqual(lim.limit, 2, f"{s} 应触发降速")

    def test_disabled_is_exactly_fixed_behaviour(self):
        lim = make(initial=3, enabled=False)
        self.assertFalse(lim.throttle("风控"))
        lim.finish("ok")
        lim.finish("ip_blocked")
        self.assertEqual(lim.limit, 3)
        self.assertEqual(lim.throttles, 0)
        self.assertEqual(lim.recovers, 0)
        self.assertEqual(lim.gap(), (1.0, 4.0))
        self.assertEqual(lim.cooldown_until, 0.0)

    async def test_cooldown_blocks_new_acquire(self):
        lim = make(initial=3, cooldown=0.08)
        lim.throttle("风控")
        t0 = time.monotonic()
        await lim.acquire()
        waited = time.monotonic() - t0
        self.assertGreaterEqual(waited, 0.04, "冷却期内不该放行新任务")
        lim.release()

    def test_snapshot_fields(self):
        lim = make(initial=3)
        s = lim.snapshot()
        for k in ("limit", "max_limit", "active", "gap_mult", "clean_streak",
                  "cooldown_left", "throttles", "recovers", "enabled"):
            self.assertIn(k, s)
        self.assertEqual(s["limit"], 3)
        self.assertEqual(s["active"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
