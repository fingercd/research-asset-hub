"""Token/cost estimation and in-process budget enforcement."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from threading import Lock


class BudgetExceeded(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ModelPricing:
    input_per_million: float = 0.0
    output_per_million: float = 0.0
    currency: str = "CNY"
    version: str = "unversioned"


@dataclass(frozen=True, slots=True)
class BudgetPolicy:
    max_request_tokens: int = 32_000
    max_monthly_tokens: int = 2_000_000
    max_request_cost: float = 5.0
    max_daily_cost: float = 10.0
    max_monthly_cost: float = 50.0
    safety_margin: float = 0.20

    def __post_init__(self) -> None:
        if not 0 <= self.safety_margin < 1:
            raise ValueError("safety_margin must be in [0, 1)")


@dataclass(frozen=True, slots=True)
class BudgetEstimate:
    input_tokens: int
    output_tokens: int
    estimated_cost: float

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


def estimate_tokens(text: str) -> int:
    """Conservative language-neutral estimate without a tokenizer dependency."""

    return max(1, (len(text) + 2) // 3)


def estimate_budget(
    text: str,
    *,
    max_output_tokens: int,
    pricing: ModelPricing,
) -> BudgetEstimate:
    input_tokens = estimate_tokens(text)
    cost = (
        input_tokens * pricing.input_per_million
        + max_output_tokens * pricing.output_per_million
    ) / 1_000_000
    return BudgetEstimate(input_tokens, max_output_tokens, cost)


class BudgetLedger:
    def __init__(
        self,
        policy: BudgetPolicy | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.policy = policy or BudgetPolicy()
        self.clock = clock or (lambda: datetime.now(UTC))
        self.month_tokens = 0
        self.month_cost = 0.0
        self.day_cost = 0.0
        now = self.clock()
        self._day: date = now.date()
        self._month: tuple[int, int] = (now.year, now.month)
        self._lock = Lock()

    def _roll_periods(self) -> None:
        now = self.clock()
        today = now.date()
        month = (now.year, now.month)
        if today != self._day:
            self.day_cost = 0.0
            self._day = today
        if month != self._month:
            self.month_tokens = 0
            self.month_cost = 0.0
            self._month = month

    def _authorize_unlocked(self, estimate: BudgetEstimate) -> None:
        self._roll_periods()
        cost_factor = 1.0 - self.policy.safety_margin
        request_cost_limit = self.policy.max_request_cost * cost_factor
        daily_cost_limit = self.policy.max_daily_cost * cost_factor
        monthly_cost_limit = self.policy.max_monthly_cost * cost_factor
        if estimate.total_tokens > self.policy.max_request_tokens:
            raise BudgetExceeded("Request token budget exceeded")
        if estimate.estimated_cost > request_cost_limit:
            raise BudgetExceeded("Request cost budget exceeded")
        if self.month_tokens + estimate.total_tokens > self.policy.max_monthly_tokens:
            raise BudgetExceeded("Monthly token budget exceeded")
        if self.day_cost + estimate.estimated_cost > daily_cost_limit:
            raise BudgetExceeded("Daily cost budget exceeded")
        if self.month_cost + estimate.estimated_cost > monthly_cost_limit:
            raise BudgetExceeded("Monthly cost budget exceeded")

    def authorize(self, estimate: BudgetEstimate) -> None:
        with self._lock:
            self._authorize_unlocked(estimate)

    def record(self, estimate: BudgetEstimate) -> None:
        with self._lock:
            self._authorize_unlocked(estimate)
            self.month_tokens += estimate.total_tokens
            self.day_cost += estimate.estimated_cost
            self.month_cost += estimate.estimated_cost

    def snapshot(self) -> dict[str, int | float | str]:
        with self._lock:
            self._roll_periods()
            return {
                "day": self._day.isoformat(),
                "month": f"{self._month[0]:04d}-{self._month[1]:02d}",
                "month_tokens": self.month_tokens,
                "day_cost": self.day_cost,
                "month_cost": self.month_cost,
            }


@dataclass(frozen=True, slots=True)
class PricingCatalog:
    """Versioned prices keyed by ``(provider, model)``.

    A missing entry is intentionally different from a zero-cost entry.  The
    service blocks generation when :meth:`resolve` returns ``None``.
    """

    prices: Mapping[tuple[str, str], ModelPricing]

    def resolve(self, provider: str, model: str) -> ModelPricing | None:
        return self.prices.get((provider, model))
