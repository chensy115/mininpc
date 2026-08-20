from __future__ import annotations

from dataclasses import dataclass


WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
WEEKDAYS_ZH = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")
TICK_MINUTES = 10


@dataclass(frozen=True)
class ClockSnapshot:
    total_minutes: int

    @property
    def day(self) -> int:
        return self.total_minutes // 1440 + 1

    @property
    def weekday(self) -> str:
        return WEEKDAYS_ZH[(self.day - 1) % len(WEEKDAYS_ZH)]

    @property
    def weekday_key(self) -> str:
        return WEEKDAYS[(self.day - 1) % len(WEEKDAYS)]

    @property
    def hour(self) -> int:
        return (self.total_minutes % 1440) // 60

    @property
    def minute(self) -> int:
        return self.total_minutes % 60

    @property
    def time_text(self) -> str:
        return f"{self.hour:02d}:{self.minute:02d}"

    @property
    def label(self) -> str:
        return f"第 {self.day} 天 · {self.weekday} · {self.time_text}"


class WorldClock:
    def __init__(self, total_minutes: int = 480) -> None:
        self.total_minutes = total_minutes

    def advance(self, minutes: int = TICK_MINUTES) -> ClockSnapshot:
        self.total_minutes += minutes
        return self.snapshot()

    def snapshot(self) -> ClockSnapshot:
        return ClockSnapshot(self.total_minutes)
