"""Small, dependency-free utility functions."""
from __future__ import annotations

import uuid
from decimal import Decimal, InvalidOperation
from typing import Mapping

_MONEY_TYPES = (int, float, str, Decimal)


def to_decimal(value: int | float | str | Decimal) -> Decimal:
    """Convert a value to Decimal safely (never silently via binary float unless given a float)."""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def non_negative_decimal(value: int | float | str | Decimal, name: str = "value") -> Decimal:
    """Validate that a value is a finite, non-negative number and return it as Decimal."""
    try:
        result = to_decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not result.is_finite():
        raise ValueError(f"{name} must be finite")
    if result < 0:
        raise ValueError(f"{name} must be >= 0")
    return result


def positive_decimal(value: int | float | str | Decimal, name: str = "value") -> Decimal:
    """Validate that a value is a finite, strictly positive number and return it as Decimal."""
    result = non_negative_decimal(value, name)
    if result <= 0:
        raise ValueError(f"{name} must be > 0")
    return result


def non_negative_int(value: int, name: str = "value") -> int:
    if not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if value < 0:
        raise ValueError(f"{name} must be >= 0")
    return value


def positive_int(value: int, name: str = "value") -> int:
    result = non_negative_int(value, name)
    if result == 0:
        raise ValueError(f"{name} must be > 0")
    return result


def new_id(prefix: str = "ORD") -> str:
    """Generate a unique identifier, e.g. ``ORD_1f2c3d4e...``.

    Used for order and trade ids so the paper system has stable references.
    """
    return f"{prefix}_{uuid.uuid4().hex}"


def notional(quantity: int, price: int | float | str | Decimal, multiplier: int) -> Decimal:
    """Compute the rupee notional of a trade: ``quantity * price * multiplier``."""
    return positive_int(quantity, "quantity") * positive_decimal(price, "price") * positive_int(
        multiplier, "multiplier"
    )


def sum_values(values: Mapping[str, Decimal]) -> Decimal:
    """Sum a mapping of Decimals, returning a Decimal (empty maps sum to zero)."""
    total = Decimal("0")
    for value in values.values():
        total += value
    return total