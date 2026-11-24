"""Order exports (web, app, USSD, POS) -> canonical orders.

Input is csv_utils.Row objects. Column names vary between shop systems, so each
canonical field has a list of accepted aliases (COLUMN_ALIASES); the aliases are
educated guesses until checked against real exports. Only ``order_id`` and
``amount`` must be present as columns. Other gaps are handled per row:

* A blank amount, time or phone is staged as NULL so the completeness checks can
  see it. An unreadable one is staged as NULL and reported as an issue.
* A row without an order id, channel, status or a known currency cannot be
  staged and is rejected with a reason.
* Phones are stored as a keyed hash plus a masked hint. Without a salt the hash
  is skipped (reported once as a file-level issue) but the hint still works.
* Customer names are never read; only ``customer_id``.
* Amounts keep their sign: a negative order total is data the checks must see.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable
from dataclasses import dataclass, field

from payrecon.adapters.base import Issue, Rejected
from payrecon.csv_utils import BadRow, CsvError, Row
from payrecon.normalize import (
    NormalizationError,
    clean_text,
    detect_currency,
    hash_phone,
    normalize_bill_ref,
    normalize_channel,
    normalize_order_id,
    normalize_order_status,
    normalize_phone,
    parse_money,
    parse_timestamp,
    phone_hint,
)
from payrecon.schema import COUNTRIES, CURRENCIES, ORDER_COLUMNS, blank_row

# Earlier aliases win. Aliases are unique across fields (a test enforces it), so a
# header can only ever belong to one field.
COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "order_id": ("order_id", "order_number", "order_no", "order_ref", "order", "id"),
    "channel": ("channel", "sales_channel", "source", "platform", "origin"),
    "customer_id": ("customer_id", "customer_no", "customer_number", "client_id"),
    "phone": ("phone", "phone_number", "msisdn", "mobile", "mobile_number", "customer_phone", "tel"),
    "order_time": ("order_time", "order_date", "created_at", "date_time", "datetime", "timestamp", "date"),
    "amount": ("amount", "total", "order_total", "grand_total", "total_amount"),
    "currency": ("currency", "currency_code"),
    "status": ("status", "order_status", "state"),
    "bill_ref": ("bill_ref", "account_ref", "account_number", "account_no", "payment_reference", "reference"),
}
ORDER_HEADER_HINTS = tuple(dict.fromkeys(a for aliases in COLUMN_ALIASES.values() for a in aliases))
REQUIRED_FIELDS = ("order_id", "amount")

TEMPLATE_HEADER = (
    "order_id", "channel", "customer_id", "phone", "order_time",
    "amount", "currency", "status", "bill_ref",
)
_TEMPLATE_ROWS = (
    ("ORD-1001", "WEB", "C-001", "0712345678", "2026-10-31 14:05:09", "1500.00", "KES", "COMPLETED", "ORD1001"),
    ("ORD-1002", "APP", "C-002", "0722000111", "2026-10-31 15:20:00", "250.00", "KES", "COMPLETED", ""),
)


def template_csv() -> str:
    """A small example file in the documented template layout (synthetic data)."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(TEMPLATE_HEADER)
    writer.writerows(_TEMPLATE_ROWS)
    return out.getvalue()


@dataclass
class AdaptedOrders:
    orders: list[dict[str, object]] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    rejected: list[Rejected] = field(default_factory=list)
    columns: dict[str, str] = field(default_factory=dict)  # canonical field -> header used


def map_columns(header: Iterable[str]) -> dict[str, str]:
    """Match canonical fields to the headers present (already normalized by csv_utils)."""
    present = set(header)
    mapping: dict[str, str] = {}
    for name, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in present:
                mapping[name] = alias
                break
    return mapping


def adapt_orders(
    rows: Iterable[Row],
    bad_rows: Iterable[BadRow] = (),
    *,
    source_file: str,
    channel: str | None = None,
    country: str = "KE",
    currency: str | None = None,
    default_status: str | None = None,
    salt: str | None = None,
) -> AdaptedOrders:
    """Map order rows to ORDER_COLUMNS dicts.

    ``channel`` and ``default_status`` fill in for files that have no such column
    (an export named orders_web.csv usually has no channel column).
    """
    if country not in COUNTRIES:
        raise ValueError(f"unknown country: {country}")
    fallback_currency = currency or COUNTRIES[country].currency
    if fallback_currency not in CURRENCIES:
        raise ValueError(f"unknown currency: {fallback_currency}")
    fallback_channel = normalize_channel(channel)
    fallback_status = normalize_order_status(default_status)

    result = AdaptedOrders()
    for bad in bad_rows:
        result.rejected.append(Rejected(bad.number, bad.reason, list(bad.cells)))

    mapping: dict[str, str] | None = None
    for row in rows:
        if mapping is None:
            mapping = map_columns(row.data)
            missing = [name for name in REQUIRED_FIELDS if name not in mapping]
            if missing:
                raise CsvError("missing_columns", f"Missing columns for: {missing}")
            result.columns = dict(mapping)
            if "phone" in mapping and salt is None:
                result.issues.append(Issue(0, "phone_hash", "no_phone_salt"))

        def cell(name: str, _row: Row = row, _map: dict[str, str] = mapping) -> str:
            return _row.data[_map[name]] if name in _map else ""

        number, issues = row.number, result.issues
        raw_id, raw_time, raw_amount = cell("order_id"), cell("order_time"), cell("amount")
        brief = [raw_id, raw_time, raw_amount]

        order_id = normalize_order_id(raw_id)
        if order_id is None:
            result.rejected.append(Rejected(number, "missing_order_id", brief))
            continue
        channel_value = normalize_channel(cell("channel")) or fallback_channel
        if channel_value is None:
            result.rejected.append(Rejected(number, "missing_channel", brief))
            continue
        status = normalize_order_status(cell("status")) or fallback_status
        if status is None:
            result.rejected.append(Rejected(number, "missing_status", brief))
            continue
        raw_currency = cell("currency").strip()
        row_currency = detect_currency(raw_currency) if raw_currency else fallback_currency
        if row_currency is None:
            result.rejected.append(Rejected(number, "unknown_currency", brief))
            continue

        amount_minor = None
        try:
            amount_minor = parse_money(raw_amount, row_currency)
        except NormalizationError as exc:
            issues.append(Issue(number, "amount", exc.code))
        order_ts_utc = None
        try:
            order_ts_utc = parse_timestamp(raw_time, country)
        except NormalizationError as exc:
            issues.append(Issue(number, "order_ts", exc.code))
        e164 = None
        try:
            e164 = normalize_phone(cell("phone"), country)
        except NormalizationError as exc:
            issues.append(Issue(number, "phone", exc.code))

        order = blank_row(ORDER_COLUMNS)
        order.update(
            order_id=order_id,
            order_id_raw=raw_id,
            channel=channel_value,
            customer_id=clean_text(cell("customer_id")),
            bill_ref=normalize_bill_ref(cell("bill_ref")),
            phone_hash=hash_phone(e164, salt) if e164 and salt else None,
            phone_hint=phone_hint(e164) if e164 else None,
            order_ts_raw=clean_text(raw_time),
            order_ts_utc=order_ts_utc,
            amount_minor=amount_minor,
            currency=row_currency,
            status=status,
            source_file=source_file,
            source_row=number,
        )
        result.orders.append(order)
    return result
