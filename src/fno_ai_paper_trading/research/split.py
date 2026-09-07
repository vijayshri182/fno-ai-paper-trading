"""In-sample / validation / out-of-sample splitting.

The split is purely chronological — the training segment always comes first.
This prevents future-data leakage because the backtest engine only ever exposes
``bars[:i+1]`` to the strategy at bar ``i``, so evaluating a model on the test
segment can never see out-of-sample bars ahead of time.

How the split works
-------------------
Given ``n`` bars and fractions ``train`` / ``validation`` / ``test`` (which must
sum to 1):

* ``train_end`` = ``floor(n * train)``
* ``validation_end`` = ``floor(n * (train + validation))``
* the test segment runs to the end of the data

The splits are contiguous, non-overlapping, and preserve bar order. The
fractions are floor-adjusted so the three segments always cover exactly the
original series. If a fraction is 0, its segment is empty (``[]``).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.utils.functions import non_negative_decimal, positive_int


@dataclass(frozen=True)
class SplitScheme:
    """Proportions of the data assigned to each period (must sum to 1)."""

    train: Decimal = Decimal("0.6")
    validation: Decimal = Decimal("0.2")
    test: Decimal = Decimal("0.2")

    def __post_init__(self) -> None:
        object.__setattr__(self, "train", non_negative_decimal(self.train, "train"))
        object.__setattr__(self, "validation", non_negative_decimal(self.validation, "validation"))
        object.__setattr__(self, "test", non_negative_decimal(self.test, "test"))
        if self.train + self.validation + self.test != Decimal("1"):
            raise ValueError("train + validation + test fractions must sum to 1")


@dataclass(frozen=True)
class Split:
    """Chronological in-sample / validation / out-of-sample segments."""

    train: list[MarketPrice]
    validation: list[MarketPrice]
    test: list[MarketPrice]
    train_range: tuple[datetime, datetime] | None = None
    validation_range: tuple[datetime, datetime] | None = None
    test_range: tuple[datetime, datetime] | None = None

    @property
    def boundaries(self) -> dict[str, int]:
        return {
            "train_end": len(self.train),
            "validation_end": len(self.train) + len(self.validation),
            "test_end": len(self.train) + len(self.validation) + len(self.test),
        }


def _range(bars: list[MarketPrice]) -> tuple[datetime, datetime] | None:
    if not bars:
        return None
    return bars[0].timestamp, bars[-1].timestamp


def split_bars(bars: list[MarketPrice], scheme: SplitScheme | None = None) -> Split:
    """Split a bar series into train / validation / test segments."""
    scheme = scheme or SplitScheme()
    n = len(bars)

    train_end = int(Decimal(n) * scheme.train)
    validation_end = train_end + int(Decimal(n) * scheme.validation)

    return Split(
        train=bars[:train_end],
        validation=bars[train_end:validation_end],
        test=bars[validation_end:],
        train_range=_range(bars[:train_end]),
        validation_range=_range(bars[train_end:validation_end]),
        test_range=_range(bars[validation_end:]),
    )


def split_indices(n: int, scheme: SplitScheme | None = None) -> tuple[int, int, int, int, int, int]:
    """Return half-open index ranges ``(train_start, train_end, val_start, val_end, test_start, test_end)``."""
    n = positive_int(n, "n")
    scheme = scheme or SplitScheme()
    train_end = int(Decimal(n) * scheme.train)
    val_end = train_end + int(Decimal(n) * scheme.validation)
    return 0, train_end, train_end, val_end, val_end, n