"""Money helpers.

All monetary amounts are stored as integer *micros* (1 USD = 1_000_000 micros).
Integers avoid float drift when summing thousands of small ad-spend rows, and
micros are the native unit of the Google Ads API.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

MICROS = 1_000_000


def usd_to_micros(amount: float | int | str | Decimal) -> int:
    """Convert a USD amount to micros, rounding half-up at the micro."""
    return int(
        (Decimal(str(amount)) * MICROS).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    )


def micros_to_usd(micros: int) -> float:
    """Convert micros to a float USD amount (display only)."""
    return round(micros / MICROS, 6)


def cents_to_micros(cents: int | str) -> int:
    """Meta reports spend in account-currency minor units (cents)."""
    return int(Decimal(str(cents)) * 10_000)


def micros_to_cents(micros: int) -> int:
    return int(
        (Decimal(micros) / 10_000).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    )


def fmt_usd(micros: int) -> str:
    return f"${micros_to_usd(micros):,.2f}"


def safe_div(numerator: float, denominator: float, default: float = 0.0) -> float:
    return numerator / denominator if denominator else default
