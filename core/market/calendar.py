"""Minimal trading calendar / session helpers.

Minute-level replay only needs to know the A-share session boundaries and which
days are trading days. Holidays can be supplied explicitly; weekends are always
excluded.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Iterable

MORNING_OPEN = time(9, 30)
MORNING_CLOSE = time(11, 30)
AFTERNOON_OPEN = time(13, 0)
AFTERNOON_CLOSE = time(15, 0)


@dataclass
class TradingCalendar:
    holidays: set[date] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.holidays = {h if isinstance(h, date) else date.fromisoformat(str(h)) for h in self.holidays}

    def is_trading_day(self, day: date) -> bool:
        return day.weekday() < 5 and day not in self.holidays

    def trading_days(self, start: date, end: date) -> list[date]:
        days: list[date] = []
        cur = start
        while cur <= end:
            if self.is_trading_day(cur):
                days.append(cur)
            cur += timedelta(days=1)
        return days

    def next_trading_day(self, day: date) -> date:
        cur = day + timedelta(days=1)
        for _ in range(400):
            if self.is_trading_day(cur):
                return cur
            cur += timedelta(days=1)
        raise RuntimeError("no trading day found within 400 days")

    def sessions(self, day: date) -> list[tuple[datetime, datetime]]:
        if not self.is_trading_day(day):
            return []
        return [
            (datetime.combine(day, MORNING_OPEN), datetime.combine(day, MORNING_CLOSE)),
            (datetime.combine(day, AFTERNOON_OPEN), datetime.combine(day, AFTERNOON_CLOSE)),
        ]

    def is_session_time(self, ts: datetime) -> bool:
        t = ts.time()
        return (MORNING_OPEN <= t < MORNING_CLOSE) or (AFTERNOON_OPEN <= t < AFTERNOON_CLOSE)

    def expected_minutes(self, day: date) -> int:
        return 240 if self.is_trading_day(day) else 0

    def iter_minutes(self, start: date, end: date) -> Iterable[datetime]:
        for day in self.trading_days(start, end):
            for begin, finish in self.sessions(day):
                cur = begin
                while cur < finish:
                    yield cur
                    cur += timedelta(minutes=1)
