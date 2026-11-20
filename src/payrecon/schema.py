"""Canonical schema and static configuration for payrecon-ea.

Single source of truth for currencies, countries, payment providers,
allowed enum values and the column contract that every adapter must emit.
Plain data only: no I/O and no third-party imports.

Money is always an integer count of minor units (see Currency.exponent).
Timestamps are stored as UTC ISO-8601 strings. Phone numbers are never
stored raw, only as a salted hash.
"""

from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Currencies
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Currency:
    code: str
    name: str
    exponent: int  # digits after the decimal point (ISO 4217 minor unit)


CURRENCIES: dict[str, Currency] = {
    c.code: c
    for c in (
        Currency("KES", "Kenyan Shilling", 2),
        Currency("UGX", "Ugandan Shilling", 0),
        Currency("TZS", "Tanzanian Shilling", 2),
        Currency("RWF", "Rwandan Franc", 0),
        Currency("ETB", "Ethiopian Birr", 2),
        Currency("USD", "US Dollar", 2),
    )
}


def minor_per_major(currency: str) -> int:
    """Minor units in one major unit, e.g. 100 for KES and 1 for UGX."""
    return 10 ** CURRENCIES[currency].exponent


# ---------------------------------------------------------------------------
# Countries
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Country:
    iso2: str
    name: str
    calling_code: str  # digits only, no plus sign
    currency: str
    utc_offset_minutes: int  # fixed offset; none of these countries uses DST


# National numbers are 9 digits in all five countries, so a full E.164 number
# is len(calling_code) + 9 digits.
NATIONAL_NUMBER_LENGTH = 9

COUNTRIES: dict[str, Country] = {
    c.iso2: c
    for c in (
        Country("KE", "Kenya", "254", "KES", 180),
        Country("UG", "Uganda", "256", "UGX", 180),
        Country("TZ", "Tanzania", "255", "TZS", 180),
        Country("RW", "Rwanda", "250", "RWF", 120),  # CAT, UTC+2
        Country("ET", "Ethiopia", "251", "ETB", 180),
    )
}

DEFAULT_COUNTRY = "KE"


# ---------------------------------------------------------------------------
# Payment providers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Provider:
    code: str
    name: str
    countries: tuple[str, ...]
    receipt_pattern: str | None  # regex for the transaction id; None = unknown


# Receipt formats are listed only where observed. Verify against real exports
# before relying on them. Adding a provider = one line here + one adapter.
PROVIDERS: dict[str, Provider] = {
    p.code: p
    for p in (
        Provider("MPESA_KE", "M-Pesa (Safaricom)", ("KE",), r"^[A-Z0-9]{10}$"),
        Provider("MPESA_TZ", "M-Pesa (Vodacom)", ("TZ",), None),
        Provider("AIRTEL_MONEY", "Airtel Money", ("KE", "UG", "TZ", "RW"), None),
        Provider("MTN_MOMO", "MTN Mobile Money", ("UG", "RW"), None),
        Provider("CARD", "Card gateway", tuple(COUNTRIES), None),
    )
}


# ---------------------------------------------------------------------------
# Allowed values
# ---------------------------------------------------------------------------

CHANNELS = ("WEB", "APP", "USSD", "POS")
ORDER_STATUSES = ("COMPLETED", "PENDING", "CANCELLED", "REFUNDED")
PAYMENT_STATUSES = ("SUCCESS", "FAILED", "PENDING", "REVERSED")
PAYMENT_KINDS = ("PAYMENT", "REVERSAL")
PAYMENT_METHODS = ("PAYBILL", "TILL", "STK_PUSH", "CARD", "OTHER")


# ---------------------------------------------------------------------------
# Staging column contract
# ---------------------------------------------------------------------------
# Adapters emit dicts with exactly these keys. Database-generated row ids live
# in sql/schema.sql, not here. Columns with a completeness check are nullable
# on purpose so the check can see the gap. A row missing a NOT NULL field
# cannot be staged and goes to quarantine instead.


@dataclass(frozen=True)
class Column:
    name: str
    sql_type: str  # "TEXT" or "INTEGER"
    nullable: bool
    doc: str


ORDER_COLUMNS: tuple[Column, ...] = (
    Column("order_id", "TEXT", False, "Normalized order key (trimmed, upper-case)"),
    Column("order_id_raw", "TEXT", False, "Order key exactly as received"),
    Column("channel", "TEXT", False, "Origin channel, one of CHANNELS"),
    Column("customer_id", "TEXT", True, "Customer id from the source system"),
    Column("bill_ref", "TEXT", True, "Normalized reference the payer should quote"),
    Column("phone_hash", "TEXT", True, "Salted hash of the E.164 phone number"),
    Column("order_ts_raw", "TEXT", True, "Order timestamp exactly as received"),
    Column("order_ts_utc", "TEXT", True, "Order timestamp as UTC ISO-8601"),
    Column("amount_minor", "INTEGER", True, "Order total in minor units"),
    Column("currency", "TEXT", False, "ISO 4217 code, one of CURRENCIES"),
    Column("status", "TEXT", False, "Normalized status; unknown values are kept"),
    Column("source_file", "TEXT", False, "File the row came from"),
    Column("source_row", "INTEGER", False, "1-based data row number in that file"),
)

PAYMENT_COLUMNS: tuple[Column, ...] = (
    Column("provider", "TEXT", False, "Provider code, one of PROVIDERS"),
    Column("receipt_no", "TEXT", True, "Provider transaction id (idempotency key)"),
    Column("kind", "TEXT", False, "PAYMENT or REVERSAL"),
    Column("original_receipt_no", "TEXT", True, "For reversals: receipt reversed"),
    Column("method", "TEXT", False, "How the payer paid, one of PAYMENT_METHODS"),
    Column("bill_ref_raw", "TEXT", True, "Account number as the payer typed it"),
    Column("bill_ref", "TEXT", True, "Normalized bill reference"),
    Column("phone_hash", "TEXT", True, "Salted hash of the payer's E.164 phone"),
    Column("paid_ts_raw", "TEXT", True, "Payment timestamp exactly as received"),
    Column("paid_ts_utc", "TEXT", True, "Payment timestamp as UTC ISO-8601"),
    Column("amount_minor", "INTEGER", True, "Amount in minor units"),
    Column("fee_minor", "INTEGER", True, "Provider or gateway fee, if reported"),
    Column("currency", "TEXT", False, "ISO 4217 code, one of CURRENCIES"),
    Column("status", "TEXT", False, "Normalized status; unknown values are kept"),
    Column("source_file", "TEXT", False, "File the row came from"),
    Column("source_row", "INTEGER", False, "1-based data row number in that file"),
)


def column_names(columns: tuple[Column, ...]) -> tuple[str, ...]:
    return tuple(c.name for c in columns)


def required_columns(columns: tuple[Column, ...]) -> tuple[str, ...]:
    return tuple(c.name for c in columns if not c.nullable)


def blank_row(columns: tuple[Column, ...]) -> dict[str, None]:
    """A row with every column set to None, for adapters to fill in."""
    return {c.name: None for c in columns}
