"""登录限速：按用户名与来源地址的滑动窗口计数。

限制在单进程内存中，重启即重置；当前管理 API 为单进程部署，够用且不引入
新的持久表。若将来横向扩展为多进程／多副本，必须改为共享存储，不能靠各
副本互不知情的本地计数冒充全局限速。
"""
from __future__ import annotations

import threading
import time
from collections import deque


class LoginRateLimiter:
    def __init__(self, max_attempts: int, window_seconds: int) -> None:
        self._max = max_attempts
        self._window = window_seconds
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> deque[float]:
        bucket = self._hits.setdefault(key, deque())
        cutoff = now - self._window
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        return bucket

    def retry_after(self, key: str) -> int:
        """返回还需要等待的秒数；0 表示当前允许尝试。"""
        now = time.monotonic()
        with self._lock:
            bucket = self._prune(key, now)
            if len(bucket) < self._max:
                return 0
            return max(1, int(self._window - (now - bucket[0])) + 1)

    def record_failure(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            self._prune(key, now).append(now)

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)


_limiter: LoginRateLimiter | None = None


def get_limiter(max_attempts: int, window_seconds: int) -> LoginRateLimiter:
    global _limiter
    if _limiter is None or _limiter._max != max_attempts or _limiter._window != window_seconds:
        _limiter = LoginRateLimiter(max_attempts, window_seconds)
    return _limiter
