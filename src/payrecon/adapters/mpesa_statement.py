"""M-Pesa statement rows -> canonical payment lines.

Input is csv_utils.Row objects, whether they came from a CSV export or from
pdf_table.read_pdf_statement, so one adapter serves both. Expected columns:
receipt_no, completion_time, details, transaction_status, paid_in, withdrawn
(balance is ignored).

Rules learned from a real statement:

* A receipt number is NOT unique per line. One receipt can carry a payment, its
  charge and a Fuliza overdraft draw, so lines are keyed by
  (provider, receipt_no, kind, direction); see schema.PAYMENT_KEY.
* Overdraft draws are credit, not customer money, so they get their own kind.
* ``details`` is free text containing third-party names. Only machine-readable
  parts are kept (line type, masked phone hint, account reference); the text
  itself is never stored.
* Withdrawals print as negative in PDFs and may be positive in CSVs; both are
  accepted. Amounts are stored positive with a direction.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from payrecon.adapters.base import AdaptedPayments, Issue, Rejected
from payrecon.csv_utils import BadRow, CsvError, Row
from payrecon.normalize import (
    NormalizationError,
    normalize_bill_ref,
    normalize_payment_status,
    parse_money,
    parse_phone_hint,
    parse_timestamp,
)
from payrecon.schema import COUNTRIES, PAYMENT_COLUMNS, PROVIDERS, blank_row

STATEMENT_HEADER_HINTS = (
    "receipt_no", "completion_time", "details", "transaction_status",
    "paid_in", "withdrawn", "balance",
)
REQUIRED_COLUMNS = frozenset(
    {"receipt_no", "completion_time", "details", "transaction_status", "paid_in", "withdrawn"}
)

# First match wins. Patterns are searched in the "type phrase": the details text
# up to the first ' to ', ' from ', ' by ', ' - ' or digit. Order matters.
_RULES: tuple[tuple[str, str, str], ...] = (
    (r"overdraft of credit party", "OVERDRAFT_DRAW", "OTHER"),
    (r"od loan repayment", "OVERDRAFT_REPAYMENT", "OTHER"),
    (r"\bcharge\b", "CHARGE", "OTHER"),
    (r"\breversal\b", "REVERSAL", "OTHER"),
    (r"funds received", "PAYMENT", "P2P"),
    (r"merchant customer payment", "PAYMENT", "TILL"),
    (r"business payment", "PAYMENT", "OTHER"),
    (r"pay ?bill", "PAYMENT", "PAYBILL"),
    (r"merchant payment", "PAYMENT", "TILL"),
    (r"customer (?:transfer|send money)", "PAYMENT", "P2P"),
    (r"customer payment", "PAYMENT", "OTHER"),
    (r"customer bundle purchase", "PAYMENT", "OTHER"),
)
_COMPILED = tuple((re.compile(p, re.IGNORECASE), kind, method) for p, kind, method in _RULES)
_EXPECTED_DIRECTION = {"OVERDRAFT_DRAW": "IN", "OVERDRAFT_REPAYMENT": "OUT", "CHARGE": "OUT"}

_PHRASE_END = re.compile(r"\s(?:to|from|by)\s|\s-\s|\d")
_COUNTERPARTY_HINT = re.compile(r"\b(?:to|from)\s*-\s*(\d{4,6}\*{3}\d{3})\b")
_ACCOUNT_REF = re.compile(r"\bacc\.?\s+([A-Za-z0-9][A-Za-z0-9-]*)", re.IGNORECASE)


def type_phrase(details: str) -> str:
    """The transaction-type words at the start of ``details``, before any party."""
    return _PHRASE_END.split(details, maxsplit=1)[0].strip()


def classify(details: str) -> tuple[str, str]:
    """(kind, method) for a statement line. Unrecognized lines are OTHER/OTHER."""
    phrase = type_phrase(details)
    for pattern, kind, method in _COMPILED:
        if pattern.search(phrase):
            return kind, method
    return "OTHER", "OTHER"


def _amount(raw: str, currency: str, field: str, number: int, issues: list[Issue]):
    """(present, minor units). A present-but-unusable amount is reported."""
    try:
        value = parse_money(raw, currency)
    except NormalizationError as exc:
        issues.append(Issue(number, field, exc.code))
        return True, None
    return value is not None, value


def adapt_statement(
    rows: Iterable[Row],
    bad_rows: Iterable[BadRow] = (),
    *,
    source_file: str,
    provider: str = "MPESA_KE",
    country: str = "KE",
) -> AdaptedPayments:
    """Map statement rows to PAYMENT_COLUMNS dicts, reporting issues and rejects."""
    if provider not in PROVIDERS:
        raise ValueError(f"unknown provider: {provider}")
    if country not in COUNTRIES:
        raise ValueError(f"unknown country: {country}")
    currency = COUNTRIES[country].currency
    receipt_pattern = re.compile(PROVIDERS[provider].receipt_pattern or r".*")
    result = AdaptedPayments()
    for bad in bad_rows:
        result.rejected.append(Rejected(bad.number, bad.reason, list(bad.cells)))

    seen_columns = False
    for row in rows:
        if not seen_columns:
            missing = REQUIRED_COLUMNS - set(row.data)
            if missing:
                raise CsvError("missing_columns", f"Missing columns: {sorted(missing)}")
            seen_columns = True
        data, number, issues = row.data, row.number, result.issues
        raw_in, raw_out = data["paid_in"], data["withdrawn"]
        in_present, in_value = _amount(raw_in, currency, "paid_in", number, issues)
        out_present, out_value = _amount(raw_out, currency, "withdrawn", number, issues)
        brief = [data["receipt_no"], data["completion_time"], raw_in, raw_out, data["transaction_status"]]
        if in_present and out_present:
            result.rejected.append(Rejected(number, "both_amounts", brief))
            issues[:] = [i for i in issues if i.source_row != number]
            continue
        if not (in_present or out_present):
            result.rejected.append(Rejected(number, "no_amount", brief))
            continue
        status = normalize_payment_status(data["transaction_status"])
        if status is None:
            result.rejected.append(Rejected(number, "missing_status", brief))
            issues[:] = [i for i in issues if i.source_row != number]
            continue

        direction = "IN" if in_present else "OUT"
        value = in_value if in_present else out_value
        if direction == "IN" and value is not None and value < 0:
            issues.append(Issue(number, "paid_in", "unexpected_sign"))
        kind, method = classify(data["details"])
        expected = _EXPECTED_DIRECTION.get(kind)
        if expected and expected != direction:
            issues.append(Issue(number, "direction", "unexpected_direction"))

        receipt = data["receipt_no"].strip().upper() or None
        if receipt is None:
            issues.append(Issue(number, "receipt_no", "missing_receipt"))
        elif not receipt_pattern.match(receipt):
            issues.append(Issue(number, "receipt_no", "bad_receipt_format"))

        paid_ts_utc = None
        try:
            paid_ts_utc = parse_timestamp(data["completion_time"], country)
        except NormalizationError as exc:
            issues.append(Issue(number, "paid_ts", exc.code))

        hint = None
        found_hint = _COUNTERPARTY_HINT.search(data["details"])
        if found_hint:
            hint = parse_phone_hint(found_hint.group(1))
        found_ref = _ACCOUNT_REF.search(data["details"])
        bill_ref_raw = found_ref.group(1) if found_ref else None

        payment = blank_row(PAYMENT_COLUMNS)
        payment.update(
            provider=provider,
            receipt_no=receipt,
            kind=kind,
            direction=direction,
            method=method,
            bill_ref_raw=bill_ref_raw,
            bill_ref=normalize_bill_ref(bill_ref_raw),
            phone_hint=hint,
            paid_ts_raw=data["completion_time"] or None,
            paid_ts_utc=paid_ts_utc,
            amount_minor=None if value is None else abs(value),
            currency=currency,
            status=status,
            source_file=source_file,
            source_row=number,
        )
        result.payments.append(payment)
    return result
