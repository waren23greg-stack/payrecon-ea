"""Normalizers for payrecon-ea: ids, references, money, phones, timestamps.

Pure functions, standard library only.

Contract
    * Blank input (None, "", "-", "N/A", "NaN") returns None: a gap, not an error.
    * Input that is present but unusable raises NormalizationError with a stable
      ``code``. The loader catches it, records the field and keeps the row.
    * Phone errors never carry the number itself, so logs stay free of PII.

Money rules (parse_money)
    Currency words and symbols (KES, Ksh, UGX, USh, $ ...) and spaces are ignored.
    "(1,500.00)", "-1,500.00" and "1,500.00-" are negative.
    If both "." and "," appear, the last one is the decimal point.
    A lone "," followed by 3 digits is a thousands separator ("1,500").
    A lone "." followed by 3 digits ("1.500") is ambiguous and is rejected.
    Anything finer than the currency's minor unit is rejected, never rounded.

Date rules (parse_timestamp)
    Numeric dates are read day-first (31/10/2026), the East African convention.
    ISO strings keep their own offset. Strings with no offset are taken as local
    time in the given country. Output is always UTC, "YYYY-MM-DDTHH:MM:SSZ".
"""

from __future__ import annotations

import hashlib
import hmac
import re
from datetime import datetime, timedelta, timezone

from decimal import Decimal, InvalidOperation

from payrecon.schema import (
    COUNTRIES,
    CURRENCIES,
    DEFAULT_COUNTRY,
    NATIONAL_NUMBER_LENGTH,
    minor_per_major,
)

MIN_YEAR = 2000  # earlier dates are almost always 1900/1970 placeholder junk


class NormalizationError(ValueError):
    """Present-but-unusable input. ``code`` is stable and machine-readable."""

    def __init__(self, code: str, value: object = None) -> None:
        super().__init__(code if value is None else f"{code}: {value!r}")
        self.code = code
        self.value = value


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_BLANK_TOKENS = frozenset({"", "-", "--", "N/A", "NA", "NULL", "NONE", "NAN"})
_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\ufeff"), None)


def _clean(raw: object) -> str | None:
    """Trim, drop zero-width characters, turn NBSP into a space; None if blank."""
    if raw is None:
        return None
    s = str(raw).translate(_ZERO_WIDTH).replace("\u00a0", " ").strip()
    return None if s.upper() in _BLANK_TOKENS else s


# ---------------------------------------------------------------------------
# Ids and references
# ---------------------------------------------------------------------------


def normalize_order_id(raw: object) -> str | None:
    """Trim and upper-case: ' ord-1001 ' -> 'ORD-1001'."""
    s = _clean(raw)
    return None if s is None else s.upper()


def normalize_bill_ref(raw: object) -> str | None:
    """Upper-case, keep letters and digits only: 'ord 1001' -> 'ORD1001'.

    Payers type Paybill account numbers by hand, so spacing and punctuation are
    noise. Fuzzy matching (O vs 0, transpositions) belongs in reconcile.py.
    """
    s = _clean(raw)
    if s is None:
        return None
    return re.sub(r"[^A-Z0-9]", "", s.upper()) or None


# ---------------------------------------------------------------------------
# Status and channel
# ---------------------------------------------------------------------------

_ORDER_STATUS = {
    "COMPLETE": "COMPLETED", "FULFILLED": "COMPLETED", "DELIVERED": "COMPLETED",
    "PROCESSING": "PENDING", "AWAITING PAYMENT": "PENDING",
    "CANCELED": "CANCELLED", "VOIDED": "CANCELLED",
    "REFUND": "REFUNDED",
}
_PAYMENT_STATUS = {
    "SUCCESSFUL": "SUCCESS", "COMPLETED": "SUCCESS", "PAID": "SUCCESS",
    "CONFIRMED": "SUCCESS",
    "FAILURE": "FAILED", "DECLINED": "FAILED", "REJECTED": "FAILED",
    "CANCELLED": "FAILED", "CANCELED": "FAILED", "TIMEOUT": "FAILED",
    "EXPIRED": "FAILED",
    "PROCESSING": "PENDING", "INITIATED": "PENDING",
    "REVERSAL": "REVERSED",
}
_CHANNEL = {
    "WEBSITE": "WEB", "ONLINE": "WEB", "BROWSER": "WEB",
    "MOBILE": "APP", "MOBILE APP": "APP", "ANDROID": "APP", "IOS": "APP",
    "STORE": "POS", "SHOP": "POS",
}


def _label(raw: object) -> str | None:
    s = _clean(raw)
    return None if s is None else re.sub(r"[\s_-]+", " ", s.upper())


def normalize_order_status(raw: object) -> str | None:
    """Map synonyms onto ORDER_STATUSES. Unknown values are kept upper-cased
    so the invalid_status check can see them."""
    s = _label(raw)
    return None if s is None else _ORDER_STATUS.get(s, s)


def normalize_payment_status(raw: object) -> str | None:
    """Map synonyms onto PAYMENT_STATUSES. Unknown values are kept upper-cased."""
    s = _label(raw)
    return None if s is None else _PAYMENT_STATUS.get(s, s)


def normalize_channel(raw: object) -> str | None:
    """Map synonyms onto CHANNELS. Unknown values are kept upper-cased."""
    s = _label(raw)
    return None if s is None else _CHANNEL.get(s, s)


# ---------------------------------------------------------------------------
# Money
# ---------------------------------------------------------------------------

_CURRENCY_TOKENS = {
    "KES": "KES", "KSH": "KES", "KSHS": "KES",
    "UGX": "UGX", "USH": "UGX", "USHS": "UGX",
    "TZS": "TZS", "TSH": "TZS", "TSHS": "TZS",
    "RWF": "RWF", "FRW": "RWF",
    "ETB": "ETB", "BIRR": "ETB",
    "USD": "USD",
}
_CURRENCY_RE = re.compile(
    r"(?<![A-Za-z])("
    + "|".join(sorted(_CURRENCY_TOKENS, key=len, reverse=True))
    + r")(?![A-Za-z])\.?",
    re.IGNORECASE,
)


def detect_currency(raw: object) -> str | None:
    """Currency named inside an amount string ('Ksh 1,500' -> 'KES'), if any."""
    s = _clean(raw)
    if s is None:
        return None
    m = _CURRENCY_RE.search(s)
    if m:
        return _CURRENCY_TOKENS[m.group(1).upper()]
    return "USD" if "$" in s else None


def _grouped(value: str, sep: str) -> bool:
    """True for thousands grouping like '1,500,000': 1-3 digits, then groups of 3."""
    parts = value.split(sep)
    return (
        len(parts) > 1
        and parts[0].isdigit()
        and len(parts[0]) <= 3
        and all(p.isdigit() and len(p) == 3 for p in parts[1:])
    )


def _decimal_string(s: str, original: str) -> str:
    """Resolve '.' and ',' in a string of digits into a plain Decimal string."""
    has_dot, has_comma = "." in s, "," in s
    if has_dot and has_comma:
        decimal = "." if s.rfind(".") > s.rfind(",") else ","
        group = "," if decimal == "." else "."
        head, _, tail = s.rpartition(decimal)
        if not _grouped(head, group):
            raise NormalizationError("bad_grouping", original)
        return head.replace(group, "") + "." + tail
    if not has_dot and not has_comma:
        return s
    sep = "." if has_dot else ","
    if s.count(sep) > 1:
        if not _grouped(s, sep):
            raise NormalizationError("bad_grouping", original)
        return s.replace(sep, "")
    head, _, tail = s.partition(sep)
    if len(tail) == 3 and re.fullmatch(r"[1-9][0-9]{0,2}", head):
        if sep == ".":
            raise NormalizationError("ambiguous_separator", original)
        return head + tail  # '1,500' is one thousand five hundred
    return head + "." + tail


def parse_money(raw: object, currency: str) -> int | None:
    """Parse an amount string into integer minor units of ``currency``.

    'KES 1,500.50' with KES -> 150050. 'UGX 25,000' with UGX -> 25000.
    """
    if currency not in CURRENCIES:
        raise NormalizationError("unknown_currency", currency)
    s = _clean(raw)
    if s is None:
        return None
    original = s
    s = _CURRENCY_RE.sub("", s).replace("$", "").replace("\u2212", "-")
    s = re.sub(r"\s+", "", s)
    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative, s = True, s[1:-1]
    if s.startswith("-"):
        negative, s = True, s[1:]
    elif s.endswith("-"):
        negative, s = True, s[:-1]
    elif s.startswith("+"):
        s = s[1:]
    if not re.fullmatch(r"[0-9.,]+", s):
        raise NormalizationError("not_numeric", original)
    try:
        major = Decimal(_decimal_string(s, original))
    except InvalidOperation:
        raise NormalizationError("not_numeric", original) from None
    minor = major * minor_per_major(currency)
    if minor != minor.to_integral_value():
        raise NormalizationError("too_many_decimals", original)
    return -int(minor) if negative else int(minor)


# ---------------------------------------------------------------------------
# Phones
# ---------------------------------------------------------------------------

_SCIENTIFIC_RE = re.compile(r"\+?\d(?:\.\d+)?e\+?\d+", re.IGNORECASE)


def normalize_phone(raw: object, default_country: str = DEFAULT_COUNTRY) -> str | None:
    """Return an E.164 number ('+254712345678') or None if blank.

    Accepts 0712..., 0112..., 712..., 254712..., +254 712..., 00254712... and the
    '+254 0712...' typo. Raises NormalizationError for masked numbers, Excel
    scientific notation and wrong lengths. The error never contains the number.
    """
    if default_country not in COUNTRIES:
        raise NormalizationError("unknown_country", default_country)
    s = _clean(raw)
    if s is None:
        return None
    if "*" in s or "x" in s.lower():
        raise NormalizationError("masked_phone")
    if _SCIENTIFIC_RE.fullmatch(s.replace(" ", "")):
        raise NormalizationError("scientific_notation")
    digits = re.sub(r"\D", "", s)
    if digits.startswith("00"):
        digits = digits[2:]
    for country in COUNTRIES.values():
        code = country.calling_code
        if digits.startswith(code):
            national = digits[len(code):]
            if national.startswith("0"):
                national = national[1:]
            if len(national) == NATIONAL_NUMBER_LENGTH:
                return _e164(code, national)
    if digits.startswith("0") and len(digits) == NATIONAL_NUMBER_LENGTH + 1:
        digits = digits[1:]
    if len(digits) == NATIONAL_NUMBER_LENGTH:
        return _e164(COUNTRIES[default_country].calling_code, digits)
    raise NormalizationError("bad_phone_length")


def _e164(calling_code: str, national: str) -> str:
    if national.startswith("0"):
        raise NormalizationError("bad_phone")
    return f"+{calling_code}{national}"


def hash_phone(e164: str, salt: str) -> str:
    """HMAC-SHA256 of an E.164 number, as hex.

    The salt is a secret key: keep it out of git. A bare hash of a phone number
    can be reversed by trying every possible number, so without the secret this
    is pseudonymization in name only.
    """
    if not salt:
        raise ValueError("salt must be non-empty")
    return hmac.new(salt.encode("utf-8"), e164.encode("utf-8"), hashlib.sha256).hexdigest()


def phone_to_hash(
    raw: object, salt: str, default_country: str = DEFAULT_COUNTRY
) -> str | None:
    """normalize_phone + hash_phone in one step; None for a blank phone."""
    e164 = normalize_phone(raw, default_country)
    return None if e164 is None else hash_phone(e164, salt)


# ---------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------

_TIMES = ("", " %H:%M:%S", " %H:%M", " %I:%M:%S %p", " %I:%M %p")
_DATES = (
    "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y",
    "%d %b %Y", "%d-%b-%Y", "%d %B %Y", "%d-%B-%Y", "%b %d, %Y", "%B %d, %Y",
)
_FORMATS = tuple(d + t for d in _DATES for t in _TIMES)


def _parse_datetime(s: str) -> datetime | None:
    if re.fullmatch(r"\d{14}", s):  # compact yyyymmddHHMMSS
        try:
            return datetime.strptime(s, "%Y%m%d%H%M%S")
        except ValueError:
            return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        pass
    for fmt in _FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def parse_timestamp(raw: object, country: str = DEFAULT_COUNTRY) -> str | None:
    """Parse a timestamp into UTC 'YYYY-MM-DDTHH:MM:SSZ', or None if blank.

    A date with no time means local midnight, which is the previous day in UTC.
    """
    if country not in COUNTRIES:
        raise NormalizationError("unknown_country", country)
    s = _clean(raw)
    if s is None:
        return None
    dt = _parse_datetime(re.sub(r"\s+", " ", s))
    if dt is None:
        raise NormalizationError("unparseable_timestamp", s)
    if dt.year < MIN_YEAR:
        raise NormalizationError("implausible_timestamp", s)
    if dt.tzinfo is None:
        local = timezone(timedelta(minutes=COUNTRIES[country].utc_offset_minutes))
        dt = dt.replace(tzinfo=local)
    try:
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, ValueError):
        raise NormalizationError("unparseable_timestamp", s) from None


# ---------------------------------------------------------------------------
# Masked phone hints
# ---------------------------------------------------------------------------
# Statements print counterparties as '254798***048' (6+3 digits) or '2547***716'
# (4+3 digits). A hint is a quasi-identifier, not a number: it can only help match
# a payment to an order when amount and time agree too.

_HINT_RE = re.compile(r"(\d{4,6})\*{3}(\d{3})")


def phone_hint(e164: str, prefix_digits: int = 6) -> str:
    """Mask an E.164 number like a statement does: '+254798123048' -> '254798***048'."""
    digits = re.sub(r"\D", "", e164)
    if not 4 <= prefix_digits <= 6 or len(digits) < prefix_digits + 3:
        raise NormalizationError("bad_phone_length")
    return f"{digits[:prefix_digits]}***{digits[-3:]}"


def parse_phone_hint(raw: object) -> str | None:
    """Canonical hint from printed text ('254798***048', 4-6 leading digits), or None if blank."""
    s = _clean(raw)
    if s is None:
        return None
    match = _HINT_RE.fullmatch(re.sub(r"\s+", "", s))
    if match is None:
        raise NormalizationError("bad_phone_hint")
    return f"{match.group(1)}***{match.group(2)}"


def hints_compatible(a: str, b: str) -> bool:
    """Could two hints belong to one number? Compare the shorter prefix and the suffix."""
    ma, mb = _HINT_RE.fullmatch(a), _HINT_RE.fullmatch(b)
    if ma is None or mb is None:
        return False
    shared = min(len(ma.group(1)), len(mb.group(1)))
    return ma.group(1)[:shared] == mb.group(1)[:shared] and ma.group(2) == mb.group(2)


# ---------------------------------------------------------------------------
# Free text
# ---------------------------------------------------------------------------


def clean_text(raw: object) -> str | None:
    """Trimmed text without zero-width characters; None for blanks like '-' or 'N/A'."""
    return _clean(raw)
