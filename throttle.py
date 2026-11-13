# -*- coding: utf-8 -*-
"""自适应节流：检测到风控/验证就自动降并发、拉长间隔、短暂冷却；连续干净成功后逐步恢复。

设计要点（改之前先读）：
- 单事件循环内用「检查 + 自增之间不 await」保证原子，不需要 Condition/Lock
- contextvars 按 Task 隔离：同一份问卷里多个风控信号只降速一次，不同份各自降速
- enabled=False 时完全等价于固定并发 + 固定间隔，便于 A/B 对比和回归
"""
from __future__ import annotations

import asyncio
import contextvars
import time

_task_throttled = contextvars.ContextVar("wjx_task_throttled", default=False)

# 只有这些状态代表服务端风控/页面卡死，才值得降速；
# dry_run、问卷改版(no_submit_button) 属于本地配置问题，降速没有意义
THROTTLE_STATUSES = {"ip_blocked", "need_verify", "no_response", "timeout", "error"}


def _default_log(msg):
    print(msg, flush=True)


class AdaptiveLimiter:
    def __init__(self, initial, *, min_limit=1, max_limit=None, cooldown=6.0,
                 throttle_gap_factor=2.5, max_gap_mult=4.0, recover_after=5,
                 base_min_gap=1.0, base_max_gap=4.0, enabled=True, log=None,
                 poll=0.2):
        initial = max(1, int(initial))
        self.max_limit = max(initial, int(max_limit or initial))
        self.limit = initial
        self.min_limit = max(1, min(int(min_limit), initial))
        self.active = 0
        self.cooldown_seconds = max(0.0, float(cooldown))
        self.cooldown_until = 0.0
        self.throttle_gap_factor = max(1.0, float(throttle_gap_factor))
        self.max_gap_mult = max(1.0, float(max_gap_mult))
        self.gap_mult = 1.0
        self.recover_after = max(1, int(recover_after))
        self.clean_streak = 0
        self.base_min_gap = float(base_min_gap)
        self.base_max_gap = float(base_max_gap)
        self.enabled = bool(enabled)
        self.poll = max(0.01, float(poll))
        self._log = log or _default_log
        self.throttles = 0
        self.recovers = 0

    # ---- 并发闸 ----
    async def acquire(self):
        while True:
            now = time.monotonic()
            # 无 await 区间：检查和自增是原子的，不会被同一事件循环里的其它协程插入
            if self.active < self.limit and now >= self.cooldown_until:
                self.active += 1
                return
            await asyncio.sleep(self.poll)

    def release(self):
        self.active = max(0, self.active - 1)

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, *exc):
        self.release()
        return False

    # ---- 状态调整 ----
    def throttle(self, reason):
        """本份触发了风控信号。同一 Task 内只生效一次。返回是否真的降速。"""
        if not self.enabled or _task_throttled.get():
            return False
        _task_throttled.set(True)
        old = self.limit
        self.limit = max(self.min_limit, self.limit - 1)
        self.gap_mult = min(self.max_gap_mult, self.gap_mult * self.throttle_gap_factor)
        self.cooldown_until = time.monotonic() + self.cooldown_seconds
        self.clean_streak = 0
        self.throttles += 1
        self._log(f"[自适应降速] 并发 {old}→{self.limit}｜间隔×{self.gap_mult:.1f}"
                  f"｜冷却 {self.cooldown_seconds:.0f}s｜原因: {reason}")
        return True

    def finish(self, status):
        """一份结束时结算：干净成功累计恢复，非成功状态补一次降速。"""
        throttled = _task_throttled.get()
        _task_throttled.set(False)
        if not self.enabled:
            return
        if status == "dry_run":
            return
        if status != "ok":
            if status in THROTTLE_STATUSES and not throttled:
                self.throttle(f"状态={status}")
            else:
                self.clean_streak = 0
            return
        if throttled:
            self.clean_streak = 0
            return
        self.clean_streak += 1
        if self.clean_streak >= self.recover_after:
            self.clean_streak = 0
            if self.limit < self.max_limit:
                old = self.limit
                self.limit += 1
                self.gap_mult = max(1.0, self.gap_mult / self.throttle_gap_factor)
                self.recovers += 1
                self._log(f"[自适应恢复] 并发 {old}→{self.limit}｜间隔×{self.gap_mult:.1f}")

    # ---- 查询 ----
    def gap(self):
        """当前这一份结束后该 sleep 的随机间隔范围。"""
        return (self.base_min_gap * self.gap_mult, self.base_max_gap * self.gap_mult)

    def snapshot(self):
        return {
            "limit": self.limit, "max_limit": self.max_limit, "active": self.active,
            "gap_mult": round(self.gap_mult, 2), "clean_streak": self.clean_streak,
            "cooldown_left": round(max(0.0, self.cooldown_until - time.monotonic()), 1),
            "throttles": self.throttles, "recovers": self.recovers, "enabled": self.enabled,
        }
