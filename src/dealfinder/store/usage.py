"""Metered API usage and the budget guard.

RentCast bills every successful request and has no cap of its own, so the cap is enforced
here: before a metered request goes out the guard checks what has been spent in the current
billing period and refuses if the request would go past the cap. Usage is written in its own
transaction straight away, so money spent is recorded even if the run then fails.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from dealfinder.store.tables import ApiUsageRow, utcnow


class BudgetExceeded(Exception):
    """A metered request was refused because it would go past the cap."""

    def __init__(self, provider: str, used: int, cap: int, wanted: int = 1) -> None:
        super().__init__(
            f"{provider}: {used} of {cap} requests used this billing period, so {wanted} more "
            "would go over the cap. Raise budgets in settings or wait for the next period."
        )
        self.provider = provider
        self.used = used
        self.cap = cap


def billing_period_start(today: date, cycle_day: int) -> date:
    """First day of the billing period that contains ``today``.

    ``cycle_day`` is the day of the month the plan renews. In a month too short to have that
    day the period starts on the month's last day.
    """

    def clamp(year: int, month: int) -> date:
        first_of_next = date(year + (month == 12), month % 12 + 1, 1)
        last_day = (first_of_next - timedelta(days=1)).day
        return date(year, month, min(cycle_day, last_day))

    this_month = clamp(today.year, today.month)
    if today >= this_month:
        return this_month
    previous = today.replace(day=1) - timedelta(days=1)
    return clamp(previous.year, previous.month)


class BudgetGuard:
    """Counts billable units for one provider and refuses requests past the cap.

    ``cap`` None means no cap: usage is still recorded.
    """

    def __init__(
        self,
        engine: Engine,
        provider: str,
        *,
        cap: int | None,
        cycle_day: int = 1,
        now: Callable[[], datetime] = utcnow,
        run_id: int | None = None,
    ) -> None:
        self._engine = engine
        self.provider = provider
        self.cap = cap
        self.cycle_day = cycle_day
        self._now = now
        self.run_id = run_id

    def period_start(self) -> datetime:
        start = billing_period_start(self._now().date(), self.cycle_day)
        return datetime.combine(start, time.min, tzinfo=UTC)

    def used(self) -> int:
        with Session(self._engine) as session:
            total = session.scalar(
                select(func.coalesce(func.sum(ApiUsageRow.units), 0)).where(
                    ApiUsageRow.provider == self.provider,
                    ApiUsageRow.billable.is_(True),
                    ApiUsageRow.requested_at >= self.period_start(),
                )
            )
        return int(total or 0)

    def remaining(self) -> int | None:
        return None if self.cap is None else max(0, self.cap - self.used())

    def check(self, units: int = 1) -> None:
        """Raise BudgetExceeded when ``units`` more would go past the cap."""
        if self.cap is None:
            return
        used = self.used()
        if used + units > self.cap:
            raise BudgetExceeded(self.provider, used, self.cap, units)

    def record(self, endpoint: str, *, units: int = 1, billable: bool = True) -> None:
        with Session(self._engine) as session:
            session.add(
                ApiUsageRow(
                    provider=self.provider,
                    endpoint=endpoint,
                    requested_at=self._now(),
                    billable=billable,
                    units=units,
                    run_id=self.run_id,
                )
            )
            session.commit()
