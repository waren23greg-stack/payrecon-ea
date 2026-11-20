import pytest

from payrecon.normalize import (
    NormalizationError,
    detect_currency,
    hash_phone,
    normalize_bill_ref,
    normalize_channel,
    normalize_order_id,
    normalize_order_status,
    normalize_payment_status,
    normalize_phone,
    parse_money,
    parse_timestamp,
    phone_to_hash,
)

BLANKS = [None, "", "   ", "-", "N/A", "NaN", "null", "\u200b", "\u00a0"]

MONEY_OK = [
    ("KES 1,500.00", "KES", 150000),
    ("Ksh. 1,500", "KES", 150000),
    ("KSh1500", "KES", 150000),
    ("1500.5", "KES", 150050),
    ("1.5", "KES", 150),
    ("0.50", "KES", 50),
    ("0,500", "KES", 50),
    ("0", "KES", 0),
    ("1500.000", "KES", 150000),
    ("UGX 25,000", "UGX", 25000),
    ("USh 2,500,000", "UGX", 2500000),
    ("25,000.00", "UGX", 25000),
    ("1.500.000,50", "TZS", 150000050),
    ("(1,500.00)", "KES", -150000),
    ("-1,500.00", "KES", -150000),
    ("1,500.00-", "KES", -150000),
    ("\u22121,500", "KES", -150000),
    ("1 500", "KES", 150000),
    ("1\u00a0500,50", "KES", 150050),
    ("$12.50", "USD", 1250),
]

MONEY_ERR = [
    ("1.500", "KES", "ambiguous_separator"),
    ("150.000", "UGX", "ambiguous_separator"),
    ("1,50.00", "KES", "bad_grouping"),
    ("1,500,00", "KES", "bad_grouping"),
    ("1500.505", "KES", "too_many_decimals"),
    ("1500.50", "UGX", "too_many_decimals"),
    ("abc", "KES", "not_numeric"),
    ("KES", "KES", "not_numeric"),
    (".", "KES", "not_numeric"),
    ("12", "XXX", "unknown_currency"),
]

PHONE_OK = [
    ("0712345678", "KE", "+254712345678"),
    ("712345678", "KE", "+254712345678"),
    ("254712345678", "KE", "+254712345678"),
    ("+254712345678", "KE", "+254712345678"),
    ("+254 712 345 678", "KE", "+254712345678"),
    ("00254712345678", "KE", "+254712345678"),
    ("+254 0712 345 678", "KE", "+254712345678"),
    ("0712-345-678", "KE", "+254712345678"),
    ("  0712 345 678 ", "KE", "+254712345678"),
    ("0112345678", "KE", "+254112345678"),
    ("0772123456", "UG", "+256772123456"),
    ("+256772123456", "KE", "+256772123456"),
    ("250788123456", "KE", "+250788123456"),
]

PHONE_ERR = [
    ("2547****5678", "KE", "masked_phone"),
    ("254712XXX678", "KE", "masked_phone"),
    ("2.54712E+11", "KE", "scientific_notation"),
    ("07123456", "KE", "bad_phone_length"),
    ("25471234567", "KE", "bad_phone_length"),
    ("+254 00712345678", "KE", "bad_phone_length"),
    ("0712345678", "ZZ", "unknown_country"),
]

# Kenya is UTC+3, Rwanda UTC+2. Numeric dates are day-first.
TS_OK = [
    ("2026-10-31 14:05:09", "KE", "2026-10-31T11:05:09Z"),
    ("2026-10-31T14:05:09", "KE", "2026-10-31T11:05:09Z"),
    ("2026-10-31T14:05:09Z", "KE", "2026-10-31T14:05:09Z"),
    ("2026-10-31T14:05:09+03:00", "KE", "2026-10-31T11:05:09Z"),
    ("2026-10-31T14:05:09+00:00", "KE", "2026-10-31T14:05:09Z"),
    ("31/10/2026 14:05", "KE", "2026-10-31T11:05:00Z"),
    ("31/10/2026 14:05:09", "KE", "2026-10-31T11:05:09Z"),
    ("31-10-2026", "KE", "2026-10-30T21:00:00Z"),
    ("31.10.2026 08:00", "KE", "2026-10-31T05:00:00Z"),
    ("03/04/2026 12:00", "KE", "2026-04-03T09:00:00Z"),
    ("1/2/2026 9:05", "KE", "2026-02-01T06:05:00Z"),
    ("31 Oct 2026 2:05 PM", "KE", "2026-10-31T11:05:00Z"),
    ("31 Oct 2026 12:05 am", "KE", "2026-10-30T21:05:00Z"),
    ("31-Oct-2026 14:05:09", "KE", "2026-10-31T11:05:09Z"),
    ("31 October 2026", "KE", "2026-10-30T21:00:00Z"),
    ("Oct 31, 2026 09:00", "KE", "2026-10-31T06:00:00Z"),
    ("20261031140509", "KE", "2026-10-31T11:05:09Z"),
    ("2026-10-31 14:05:09", "RW", "2026-10-31T12:05:09Z"),
    ("  31/10/2026   14:05 ", "KE", "2026-10-31T11:05:00Z"),
]

TS_ERR = [
    ("not a date", "KE", "unparseable_timestamp"),
    ("32/13/2026", "KE", "unparseable_timestamp"),
    ("31102026", "KE", "unparseable_timestamp"),
    ("01/01/1900", "KE", "implausible_timestamp"),
    ("1970-01-01 00:00:00", "KE", "implausible_timestamp"),
    ("2026-10-31", "ZZ", "unknown_country"),
]


@pytest.mark.parametrize("blank", BLANKS)
def test_blank_input_is_a_gap_not_an_error(blank):
    assert normalize_order_id(blank) is None
    assert normalize_bill_ref(blank) is None
    assert normalize_order_status(blank) is None
    assert normalize_payment_status(blank) is None
    assert normalize_channel(blank) is None
    assert parse_money(blank, "KES") is None
    assert normalize_phone(blank) is None
    assert parse_timestamp(blank) is None
    assert detect_currency(blank) is None


def test_order_id_trims_and_uppercases():
    assert normalize_order_id(" ord-1001 ") == "ORD-1001"
    assert normalize_order_id("\u200bORD-1001\u00a0") == "ORD-1001"


def test_bill_ref_keeps_letters_and_digits_only():
    assert normalize_bill_ref("ord 1001") == "ORD1001"
    assert normalize_bill_ref("ORD-1001.") == "ORD1001"
    assert normalize_bill_ref("---") is None


def test_status_and_channel_synonyms_and_unknowns():
    assert normalize_order_status("Complete") == "COMPLETED"
    assert normalize_order_status("awaiting_payment") == "PENDING"
    assert normalize_order_status("weird") == "WEIRD"  # kept for invalid_status
    assert normalize_payment_status("Successful") == "SUCCESS"
    assert normalize_payment_status("cancelled") == "FAILED"
    assert normalize_channel("Mobile App") == "APP"
    assert normalize_channel("web") == "WEB"


@pytest.mark.parametrize(("raw", "currency", "minor"), MONEY_OK)
def test_parse_money(raw, currency, minor):
    assert parse_money(raw, currency) == minor


@pytest.mark.parametrize(("raw", "currency", "code"), MONEY_ERR)
def test_parse_money_rejects(raw, currency, code):
    with pytest.raises(NormalizationError) as exc:
        parse_money(raw, currency)
    assert exc.value.code == code


def test_detect_currency():
    assert detect_currency("Ksh 1,500") == "KES"
    assert detect_currency("TSh 1,500") == "TZS"
    assert detect_currency("$5") == "USD"
    assert detect_currency("1,500") is None


@pytest.mark.parametrize(("raw", "country", "e164"), PHONE_OK)
def test_normalize_phone(raw, country, e164):
    assert normalize_phone(raw, country) == e164


@pytest.mark.parametrize(("raw", "country", "code"), PHONE_ERR)
def test_normalize_phone_rejects(raw, country, code):
    with pytest.raises(NormalizationError) as exc:
        normalize_phone(raw, country)
    assert exc.value.code == code


def test_phone_errors_never_contain_the_number():
    with pytest.raises(NormalizationError) as exc:
        normalize_phone("07123456")
    assert exc.value.value is None
    assert "0712" not in str(exc.value)


def test_hash_phone_is_keyed_and_stable():
    h = hash_phone("+254712345678", "s3cret")
    assert len(h) == 64
    assert h == hash_phone("+254712345678", "s3cret")
    assert h != hash_phone("+254712345678", "other")
    assert phone_to_hash("0712 345 678", "s3cret") == h
    assert phone_to_hash(None, "s3cret") is None
    with pytest.raises(ValueError):
        hash_phone("+254712345678", "")


@pytest.mark.parametrize(("raw", "country", "utc"), TS_OK)
def test_parse_timestamp(raw, country, utc):
    assert parse_timestamp(raw, country) == utc


@pytest.mark.parametrize(("raw", "country", "code"), TS_ERR)
def test_parse_timestamp_rejects(raw, country, code):
    with pytest.raises(NormalizationError) as exc:
        parse_timestamp(raw, country)
    assert exc.value.code == code
