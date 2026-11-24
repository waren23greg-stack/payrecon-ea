import pytest

from payrecon.adapters.base import Issue, Rejected
from payrecon.adapters.orders import (
    COLUMN_ALIASES,
    ORDER_HEADER_HINTS,
    REQUIRED_FIELDS,
    TEMPLATE_HEADER,
    adapt_orders,
    map_columns,
    template_csv,
)
from payrecon.csv_utils import BadRow, CsvError, Row, parse_table
from payrecon.normalize import clean_text, hash_phone
from payrecon.schema import ORDER_COLUMNS, column_names

SALT = "test-salt"
PRIVATE_NAME = "Wanjiku Testperson"


def order_row(number=1, **overrides):
    data = {
        "order_id": " ord-1001 ",
        "channel": "web",
        "customer_id": "C-001",
        "phone": "0712 345 678",
        "order_time": "31/10/2026 14:05",
        "amount": "KES 1,500.00",
        "currency": "",
        "status": "Complete",
        "bill_ref": "ord 1001",
    }
    data.update(overrides)
    return Row(number, data)


def adapt(*rows, bad_rows=(), **options):
    options.setdefault("salt", SALT)
    return adapt_orders(rows, bad_rows, source_file="orders_web.csv", **options)


def test_a_clean_row_maps_to_the_canonical_columns():
    result = adapt(order_row(4))
    assert not result.issues and not result.rejected
    (order,) = result.orders
    assert tuple(order) == column_names(ORDER_COLUMNS)
    assert (order["order_id"], order["order_id_raw"]) == ("ORD-1001", " ord-1001 ")
    assert (order["channel"], order["status"], order["currency"]) == ("WEB", "COMPLETED", "KES")
    assert order["amount_minor"] == 150000
    assert order["order_ts_raw"] == "31/10/2026 14:05"
    assert order["order_ts_utc"] == "2026-10-31T11:05:00Z"
    assert (order["customer_id"], order["bill_ref"]) == ("C-001", "ORD1001")
    assert order["phone_hint"] == "254712***678"
    assert order["phone_hash"] == hash_phone("+254712345678", SALT)
    assert (order["source_file"], order["source_row"]) == ("orders_web.csv", 4)


def test_the_raw_phone_number_is_never_stored():
    result = adapt(order_row())
    assert "0712" not in repr(result.orders) and "712345678" not in repr(result.orders)
    assert "0712" not in repr(result.issues) + repr(result.rejected)


def test_without_a_salt_the_hash_is_skipped_once_and_the_hint_still_works():
    result = adapt(order_row(1), order_row(2), salt=None)
    assert [o["phone_hash"] for o in result.orders] == [None, None]
    assert [o["phone_hint"] for o in result.orders] == ["254712***678"] * 2
    assert result.issues == [Issue(0, "phone_hash", "no_phone_salt")]


def test_customer_names_are_never_read():
    result = adapt(order_row(customer_name=PRIVATE_NAME, customer_id="C-009"))
    assert PRIVATE_NAME not in repr(result)
    assert result.orders[0]["customer_id"] == "C-009"


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (["order_number", "total", "msisdn", "created_at", "order_status", "account_number"],
         {"order_id": "order_number", "amount": "total", "phone": "msisdn", "order_time": "created_at",
          "status": "order_status", "bill_ref": "account_number"}),
        (["id", "grand_total", "mobile", "date", "state", "source"],
         {"order_id": "id", "amount": "grand_total", "phone": "mobile", "order_time": "date",
          "status": "state", "channel": "source"}),
        (["order_id", "order", "amount"], {"order_id": "order_id", "amount": "amount"}),  # first alias wins, no reuse
        (["whatever", "else"], {}),
    ],
)
def test_map_columns(header, expected):
    assert map_columns(header) == expected


def test_aliases_are_unique_across_fields_and_the_hints_cover_them_all():
    flat = [a for aliases in COLUMN_ALIASES.values() for a in aliases]
    assert len(flat) == len(set(flat))
    assert set(ORDER_HEADER_HINTS) == set(flat)
    assert set(REQUIRED_FIELDS) <= set(COLUMN_ALIASES)


def test_alias_columns_end_to_end():
    row = Row(1, {"order_number": "A1", "total": "20.50", "msisdn": "0722000111", "created_at": "2026-10-31 10:00:00",
                  "order_status": "paid-and-done", "mobile_extra": "x"})
    result = adapt(row, channel="USSD")
    (order,) = result.orders
    assert (order["order_id"], order["amount_minor"], order["channel"]) == ("A1", 2050, "USSD")
    assert order["status"] == "PAID AND DONE"  # unknown values are kept for invalid_status
    assert result.columns == {"order_id": "order_number", "amount": "total", "phone": "msisdn",
                              "order_time": "created_at", "status": "order_status"}


def test_missing_required_columns_raise_a_coded_error():
    with pytest.raises(CsvError) as exc:
        adapt(Row(1, {"order_id": "A1", "status": "x"}))
    assert exc.value.code == "missing_columns" and "amount" in str(exc.value)


def test_no_rows_is_an_empty_result():
    result = adapt_orders([], source_file="f.csv")
    assert (result.orders, result.issues, result.rejected, result.columns) == ([], [], [], {})


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"order_id": "  "}, "missing_order_id"),
        ({"order_id": "N/A"}, "missing_order_id"),
        ({"channel": ""}, "missing_channel"),
        ({"status": ""}, "missing_status"),
        ({"currency": "XYZ"}, "unknown_currency"),
    ],
)
def test_rows_that_cannot_be_staged_are_rejected_not_dropped(overrides, reason):
    result = adapt(order_row(3, **overrides))
    assert result.orders == []
    assert [(r.source_row, r.reason) for r in result.rejected] == [(3, reason)]


def test_rejected_cells_hold_only_id_time_and_amount():
    result = adapt(order_row(3, order_id="", customer_id=PRIVATE_NAME, phone="0712345678"))
    assert result.rejected == [Rejected(3, "missing_order_id", ["", "31/10/2026 14:05", "KES 1,500.00"])]


def test_defaults_fill_in_for_missing_columns():
    row = Row(1, {"order_id": "A1", "amount": "10.00"})
    result = adapt(row, channel="Mobile App", default_status="completed")
    (order,) = result.orders
    assert (order["channel"], order["status"]) == ("APP", "COMPLETED")
    assert order["phone_hint"] is None and order["order_ts_raw"] is None and order["order_ts_utc"] is None
    assert not result.issues


@pytest.mark.parametrize(
    ("overrides", "field", "code"),
    [
        ({"amount": "12x"}, "amount", "not_numeric"),
        ({"amount": "1.500"}, "amount", "ambiguous_separator"),
        ({"order_time": "someday"}, "order_ts", "unparseable_timestamp"),
        ({"order_time": "01/01/1900"}, "order_ts", "implausible_timestamp"),
        ({"phone": "2547****678"}, "phone", "masked_phone"),
        ({"phone": "0712"}, "phone", "bad_phone_length"),
    ],
)
def test_unusable_fields_are_reported_and_the_row_is_still_staged(overrides, field, code):
    result = adapt(order_row(5, **overrides))
    assert len(result.orders) == 1 and not result.rejected
    assert result.issues == [Issue(5, field, code)]


def test_blank_fields_are_staged_as_null_without_an_issue():
    result = adapt(order_row(amount="", order_time="", phone="", customer_id="-", bill_ref=""))
    (order,) = result.orders
    assert not result.issues
    for column in ("amount_minor", "order_ts_raw", "order_ts_utc", "phone_hash", "phone_hint", "customer_id", "bill_ref"):
        assert order[column] is None, column


def test_a_bad_amount_is_null_not_invented_and_other_fields_survive():
    (order,) = adapt(order_row(amount="12x")).orders
    assert order["amount_minor"] is None and order["order_id"] == "ORD-1001"


def test_negative_and_zero_amounts_keep_their_value_for_the_checks():
    amounts = [o["amount_minor"] for o in adapt(order_row(1, amount="-5.00"), order_row(2, amount="0")).orders]
    assert amounts == [-500, 0]


def test_duplicate_order_ids_are_both_staged():
    result = adapt(order_row(1), order_row(2, order_id="ORD-1001"))
    assert [o["order_id"] for o in result.orders] == ["ORD-1001", "ORD-1001"]


def test_currency_column_and_country_defaults():
    ugx = adapt(order_row(currency="UGX", amount="25,000"), country="UG")
    assert (ugx.orders[0]["currency"], ugx.orders[0]["amount_minor"]) == ("UGX", 25000)
    tz = adapt(order_row(amount="25,000"), country="TZ")
    assert (tz.orders[0]["currency"], tz.orders[0]["amount_minor"]) == ("TZS", 2500000)
    assert adapt(order_row(currency="Ksh")).orders[0]["currency"] == "KES"


def test_reader_bad_rows_become_rejections():
    result = adapt(order_row(), bad_rows=[BadRow(8, ["a"], "wrong_field_count")])
    assert result.rejected == [Rejected(8, "wrong_field_count", ["a"])]


def test_unknown_country_or_currency_is_a_programming_error():
    with pytest.raises(ValueError):
        adapt_orders([], source_file="f", country="ZZ")
    with pytest.raises(ValueError):
        adapt_orders([], source_file="f", currency="XXX")


def test_the_template_round_trips_with_no_issues():
    text = template_csv()
    assert text.splitlines()[0] == ",".join(TEMPLATE_HEADER)
    table = parse_table(text.encode(), ORDER_HEADER_HINTS)
    result = adapt_orders(table.rows, table.bad_rows, source_file="template.csv", salt=SALT)
    assert not result.issues and not result.rejected and len(result.orders) == 2
    assert result.columns == {name: name for name in TEMPLATE_HEADER}
    assert [o["channel"] for o in result.orders] == ["WEB", "APP"]
    assert result.orders[0]["bill_ref"] == "ORD1001" and result.orders[1]["bill_ref"] is None


def test_an_export_with_title_lines_and_different_headers_end_to_end():
    text = (
        "Shop export\n"
        "Generated 31 Oct\n"
        "\n"
        "Order No,Mobile,Date,Total,Order Status,Customer Name\n"
        f"A-1,0712345678,31/10/2026 14:05,1500,Completed,{PRIVATE_NAME}\n"
        "A-2,,31/10/2026 15:00,250.50,Pending,Someone Else\n"
    )
    table = parse_table(text.encode(), ORDER_HEADER_HINTS)
    result = adapt_orders(table.rows, table.bad_rows, source_file="export.csv", channel="POS", salt=SALT)
    assert not result.rejected and len(result.orders) == 2
    assert [o["amount_minor"] for o in result.orders] == [150000, 25050]
    assert [o["status"] for o in result.orders] == ["COMPLETED", "PENDING"]
    assert PRIVATE_NAME not in repr(result) and "Someone Else" not in repr(result)


def test_clean_text():
    assert clean_text("  C-001 ") == "C-001"
    assert clean_text("\u200bC-001\u00a0") == "C-001"
    assert clean_text("-") is None and clean_text("N/A") is None and clean_text(None) is None
