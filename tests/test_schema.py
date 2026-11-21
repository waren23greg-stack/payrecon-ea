import re

import pytest

from payrecon import schema as s

TABLES = [("orders", s.ORDER_COLUMNS), ("payments", s.PAYMENT_COLUMNS)]


def test_minor_units():
    assert s.minor_per_major("KES") == 100
    assert s.minor_per_major("UGX") == 1
    assert s.minor_per_major("RWF") == 1


def test_countries_and_providers_refer_to_known_things():
    assert {c.currency for c in s.COUNTRIES.values()} <= set(s.CURRENCIES)
    for provider in s.PROVIDERS.values():
        assert set(provider.countries) <= set(s.COUNTRIES)
    assert s.DEFAULT_COUNTRY in s.COUNTRIES


def test_rwanda_is_two_hours_ahead_of_utc_and_kenya_three():
    assert s.COUNTRIES["RW"].utc_offset_minutes == 120
    assert s.COUNTRIES["KE"].utc_offset_minutes == 180


def test_national_numbers_fit_every_calling_code():
    codes = [c.calling_code for c in s.COUNTRIES.values()]
    assert len(set(codes)) == len(codes)
    assert not any(a != b and b.startswith(a) for a in codes for b in codes)


@pytest.mark.parametrize(("name", "columns"), TABLES)
def test_columns_are_well_formed(name, columns):
    names = s.column_names(columns)
    assert len(names) == len(set(names)), name
    assert {c.sql_type for c in columns} <= {"TEXT", "INTEGER"}
    assert all(c.doc for c in columns)
    assert set(s.required_columns(columns)) == {c.name for c in columns if not c.nullable}


def test_column_counts_are_pinned():
    assert len(s.ORDER_COLUMNS) == 14 and len(s.required_columns(s.ORDER_COLUMNS)) == 7
    assert len(s.PAYMENT_COLUMNS) == 18 and len(s.required_columns(s.PAYMENT_COLUMNS)) == 8


@pytest.mark.parametrize(("name", "columns"), TABLES)
def test_no_raw_phone_column_can_exist(name, columns):
    phone_columns = [n for n in s.column_names(columns) if "phone" in n]
    assert set(phone_columns) <= {"phone_hash", "phone_hint"}, name


@pytest.mark.parametrize(("name", "columns"), TABLES)
def test_blank_row_matches_the_contract(name, columns):
    row = s.blank_row(columns)
    assert tuple(row) == s.column_names(columns)
    assert set(row.values()) == {None}


def test_payment_key_columns_exist_and_receipt_is_not_the_whole_key():
    assert set(s.PAYMENT_KEY) <= set(s.column_names(s.PAYMENT_COLUMNS))
    assert s.PAYMENT_KEY != ("provider", "receipt_no")
    assert {"kind", "direction"} <= set(s.PAYMENT_KEY)


def test_enums_cover_what_statements_contain():
    assert s.DIRECTIONS == ("IN", "OUT")
    assert {"PAYMENT", "CHARGE", "OVERDRAFT_DRAW", "OVERDRAFT_REPAYMENT"} <= set(s.PAYMENT_KINDS)
    assert {"PAYBILL", "TILL", "P2P"} <= set(s.PAYMENT_METHODS)
    assert len(set(s.PAYMENT_KINDS)) == len(s.PAYMENT_KINDS)


def test_mpesa_receipt_pattern():
    pattern = re.compile(s.PROVIDERS["MPESA_KE"].receipt_pattern)
    assert pattern.match("QWE1234ABC")
    assert not pattern.match("qwe1234abc")
    assert not pattern.match("QWE1234AB")
