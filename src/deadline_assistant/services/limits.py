"""Ограничитель для одного процесса; ключи не содержат исходных секретов."""

import math
import time

from deadline_assistant.domain import RateLimited


class RateLimiter:
    def __init__(self) -> None:
        self.windows: dict[str, tuple[float, int]] = {}

    def check(self, key: str, *, limit: int, seconds: int) -> None:
        now = time.monotonic()
        if len(self.windows) >= 10000:
            self.windows = {k: v for k, v in self.windows.items() if v[0] > now}
            if len(self.windows) >= 10000:
                raise RateLimited(seconds)
        end, count = self.windows.get(key, (now + seconds, 0))
        if end <= now:
            end, count = now + seconds, 0
        if count >= limit:
            raise RateLimited(math.ceil(end - now))
        self.windows[key] = (end, count + 1)
