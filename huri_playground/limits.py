import time
from collections import deque
from typing import Callable, Deque


class SessionSlots:
    """Caps concurrent visitor sessions: each one holds a full HuRI pipeline.

    Single event loop, so a plain counter is enough — there is no await between
    the check and the increment.
    """

    def __init__(self, max_sessions: int):
        self.max_sessions = max_sessions
        self.in_use = 0

    def try_acquire(self) -> bool:
        if self.in_use >= self.max_sessions:
            return False
        self.in_use += 1
        return True

    def release(self) -> None:
        self.in_use = max(0, self.in_use - 1)


class RateLimiter:
    """At most ``max_per_minute`` events in any sliding 60 s window."""

    def __init__(
        self, max_per_minute: int, clock: Callable[[], float] = time.monotonic
    ):
        self.max_per_minute = max_per_minute
        self.clock = clock
        self._stamps: Deque[float] = deque()

    def allow(self) -> bool:
        now = self.clock()
        while self._stamps and now - self._stamps[0] >= 60.0:
            self._stamps.popleft()
        if len(self._stamps) >= self.max_per_minute:
            return False
        self._stamps.append(now)
        return True
