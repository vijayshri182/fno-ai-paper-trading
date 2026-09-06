"""Domain enumerations for the paper trading system."""
from enum import Enum


class OrderSide(str, Enum):
    """Side of an order — BUY or SELL."""

    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(str, Enum):
    """Lifecycle status of an order."""

    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


class OrderType(str, Enum):
    """Type of order — only market-style orders for Phase 1."""

    MARKET = "MARKET"


class Signal(str, Enum):
    """Trading signal emitted by a strategy."""

    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


class InstrumentType(str, Enum):
    """Category of a financial instrument."""

    FUTURE = "FUTURE"
    OPTION_CE = "OPTION_CE"  # Call option
    OPTION_PE = "OPTION_PE"  # Put option


class RejectionReason(str, Enum):
    """Why a proposed order was rejected by the risk manager."""

    MAX_POSITION_QUANTITY_EXCEEDED = "MAX_POSITION_QUANTITY_EXCEEDED"
    MAX_ORDER_NOTIONAL_EXCEEDED = "MAX_ORDER_NOTIONAL_EXCEEDED"
    DAILY_LOSS_LIMIT_REACHED = "DAILY_LOSS_LIMIT_REACHED"
    UNKNOWN_INSTRUMENT = "UNKNOWN_INSTRUMENT"


class MarketPhase(str, Enum):
    """State of the market session at a point in time."""

    PRE_OPEN = "PRE_OPEN"  # Order entry phase just before continuous trading
    OPEN = "OPEN"  # Continuous trading is active
    CLOSED = "CLOSED"  # Trading has not started or has ended for the day
