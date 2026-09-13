"""Attributed paper-trade ledger used by the research & competition layer.

Every recorded closed paper trade must carry identification sufficient for
daily strategy/family attribution:

* ``strategy_id``   - registry id (e.g. ``moving_average_cross``)
* ``strategy_family`` - research family it competes in (e.g. TREND_FOLLOWING)
* ``strategy_version`` - version of that strategy
* ``configuration_version`` - exact parameter fingerprint
* ``signal``        - the decision that opened the trade (BUY/SELL/...)
* ``regime``        - regime label recorded at entry (``None`` when unknown)

The file stays backward compatible with ``seed_algorithm_ledger.py`` (which
reads the legacy keys via ``item.get``), so the ALGO READY monitor keeps
working unchanged.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

SCHEMA_VERSION = "2"


@dataclass(frozen=True)
class AttributedPaperTrade:
    strategy_id: str
    strategy_family: str
    strategy_version: str
    configuration_version: str
    entry_time: str
    exit_time: str
    side: str
    entry_price: Decimal
    exit_price: Decimal
    price_pnl: Decimal
    commission: Decimal
    net_pnl: Decimal
    signal: str = ""
    regime: str | None = None
    strategy_name: str = ""
    bucket: str = ""
    confidence: Decimal | None = None
    exit_reason: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "strategy_name", self.strategy_name or self.strategy_id)
        object.__setattr__(self, "bucket", self.bucket or "paper")

    @property
    def trading_date(self) -> str:
        return datetime.fromisoformat(self.exit_time).date().isoformat()

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "bucket": self.bucket,
            "strategy_id": self.strategy_id,
            "strategy_family": self.strategy_family,
            "strategy_name": self.strategy_name,
            "strategy_version": self.strategy_version,
            "configuration_version": self.configuration_version,
            "entry_time": self.entry_time,
            "exit_time": self.exit_time,
            "side": self.side,
            "entry_price": str(self.entry_price),
            "exit_price": str(self.exit_price),
            "price_pnl": str(self.price_pnl),
            "commission": str(self.commission),
            "net_pnl": str(self.net_pnl),
            "signal": self.signal,
            "regime": self.regime,
            "confidence": str(self.confidence) if self.confidence is not None else None,
            "exit_reason": self.exit_reason,
            "trading_date": self.trading_date,
        }
        if self.metadata:
            payload["metadata"] = dict(self.metadata)
        return payload

    @classmethod
    def from_dict(cls, item: Mapping[str, Any]) -> "AttributedPaperTrade":
        return cls(
            strategy_id=str(item.get("strategy_id", item.get("strategy_name", "unknown"))),
            strategy_family=str(item.get("strategy_family", "UNKNOWN")),
            strategy_version=str(item.get("strategy_version", "0.0.0")),
            configuration_version=str(item.get("configuration_version", "")),
            entry_time=str(item["entry_time"]),
            exit_time=str(item["exit_time"]),
            side=str(item.get("side", "LONG")),
            entry_price=Decimal(str(item["entry_price"])),
            exit_price=Decimal(str(item["exit_price"])),
            price_pnl=Decimal(str(item["price_pnl"])),
            commission=Decimal(str(item["commission"])),
            net_pnl=Decimal(str(item["net_pnl"])),
            signal=str(item.get("signal", "")),
            regime=item.get("regime"),
            strategy_name=str(item.get("strategy_name", "")),
            confidence=Decimal(str(item["confidence"])) if item.get("confidence") else None,
            exit_reason=str(item.get("exit_reason", "")),
            bucket=str(item.get("bucket", "")),
            metadata=dict(item.get("metadata") or {}),
        )


def load_paper_trades(path: Path) -> list[AttributedPaperTrade]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    entries = payload.get("trades", []) if isinstance(payload, dict) else payload
    if not isinstance(entries, list):
        raise ValueError(f"{path} trades must be a list")
    return [AttributedPaperTrade.from_dict(item) for item in entries]


def save_paper_trades(path: Path, trades: list[AttributedPaperTrade]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "deliverable": "attributed_paper_trade_ledger",
        "trades": [t.to_dict() for t in trades],
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def append_paper_trade(path: Path, trade: AttributedPaperTrade) -> list[AttributedPaperTrade]:
    trades = load_paper_trades(path)
    trades.append(trade)
    save_paper_trades(path, trades)
    return trades