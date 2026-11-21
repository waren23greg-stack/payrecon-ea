import pytest

from payrecon.adapters.base import Issue, Rejected
from payrecon.adapters.mpesa_statement import (
    STATEMENT_HEADER_HINTS,
    adapt_statement,
    classify,
    type_phrase,
)
from payrecon.csv_utils import BadRow, CsvError, Row, parse_table
from payrecon.schema import PAYMENT_COLUMNS, PAYMENT_KEY, column_names

FAKE_NAME = "Wanjiku Testperson"


def line(number=1, **overrides):
    data = {
        "receipt_no": "QWE1234ABC",
        "completion_time": "2026-10-29 08:00:00",
        "details": f"Funds received from - 254700***001 {FAKE_NAME}",
        "transaction_status": "Completed",
        "paid_in": "270.00",
        "withdrawn": "",
        "balance": "270.00",
    }
    data.update(overrides)
    return Row(number, data)


def adapt(*rows, bad_rows=()):
    return adapt_statement(rows, bad_rows, source_file="fake.pdf")


@pytest.mark.parametrize(
    ("details", "kind", "method"),
    [
        ("OverDraft of Credit Party", "OVERDRAFT_DRAW", "OTHER"),
        ("OD Loan Repayment to 123456 - Fuliza Overdraw", "OVERDRAFT_REPAYMENT", "OTHER"),
        ("Pay Bill Charge", "CHARGE", "OTHER"),
        ("Customer Transfer of Funds Charge", "CHARGE", "OTHER"),
        ("HELB Upkeep charge", "CHARGE", "OTHER"),
        ("Send Money Reversal via API", "REVERSAL", "OTHER"),
        (f"Funds received from - 254700***001 {FAKE_NAME}", "PAYMENT", "P2P"),
        ("Customer Transfer Fuliza MPesa to - 254700***000 Test Person", "PAYMENT", "P2P"),
        ("Customer Send Money to Micro SME Business with Fuliza MPesa to - 254700***000 X", "PAYMENT", "P2P"),
        ("Pay Bill Online Fuliza M-Pesa to 123456 - Test Co Acc. 000111", "PAYMENT", "PAYBILL"),
        ("Pay Bill to 123456 - Test Co Acc. 000111", "PAYMENT", "PAYBILL"),
        ("Merchant Payment to 654321 - Test Shop", "PAYMENT", "TILL"),
        ("Merchant Payment Fuliza M-Pesa Online to 654321 - Test Shop", "PAYMENT", "TILL"),
        ("Merchant Customer Payment from - 254700***002 Test Person", "PAYMENT", "TILL"),
        ("Business Payment from 111111 - Test Employer", "PAYMENT", "OTHER"),
        ("Customer Payment to Small Business to 654321 - Test", "PAYMENT", "OTHER"),
        ("Customer Bundle Purchase with Fuliza to 999999 TEST BUNDLES by - 254700***003 Owner", "PAYMENT", "OTHER"),
        ("Customer Withdrawal At Agent Till to 123456 - Test Agent", "OTHER", "OTHER"),
        ("Deposit of Funds at Agent Till", "OTHER", "OTHER"),
        ("Something nobody has ever seen", "OTHER", "OTHER"),
        ("", "OTHER", "OTHER"),
    ],
)
def test_classify(details, kind, method):
    assert classify(details) == (kind, method)


def test_type_phrase_stops_before_parties_and_numbers():
    assert type_phrase("Customer Send Money to Micro SME Business to - 2547***716 X") == "Customer Send Money"
    assert type_phrase("Pay Bill Charge") == "Pay Bill Charge"
    assert type_phrase("OD Loan Repayment to 123456 - Overdraw") == "OD Loan Repayment"


def test_an_incoming_line_maps_to_the_canonical_columns():
    result = adapt(line(7))
    assert not result.issues and not result.rejected
    (payment,) = result.payments
    assert tuple(payment) == column_names(PAYMENT_COLUMNS)
    assert payment["provider"] == "MPESA_KE" and payment["currency"] == "KES"
    assert (payment["kind"], payment["direction"], payment["method"]) == ("PAYMENT", "IN", "P2P")
    assert payment["amount_minor"] == 27000
    assert payment["status"] == "SUCCESS"
    assert payment["paid_ts_raw"] == "2026-10-29 08:00:00"
    assert payment["paid_ts_utc"] == "2026-10-29T05:00:00Z"  # statement times are East Africa Time
    assert payment["phone_hint"] == "254700***001"
    assert payment["phone_hash"] is None and payment["bill_ref"] is None
    assert (payment["source_file"], payment["source_row"]) == ("fake.pdf", 7)


@pytest.mark.parametrize("withdrawn", ["-30.00", "30.00", "(30.00)"])
def test_outgoing_amounts_are_stored_positive_whatever_sign_the_export_uses(withdrawn):
    result = adapt(line(paid_in="", withdrawn=withdrawn, details="Merchant Payment to 654321 - Test Shop"))
    assert not result.issues
    (payment,) = result.payments
    assert (payment["direction"], payment["amount_minor"], payment["method"]) == ("OUT", 3000, "TILL")


def test_names_from_the_details_text_are_never_kept():
    result = adapt(line(), line(2, details=f"Pay Bill to 123456 - {FAKE_NAME} Acc. 000111", paid_in="", withdrawn="-5.00"))
    everything = repr(result.payments) + repr(result.issues) + repr(result.rejected)
    assert "Wanjiku" not in everything and "Testperson" not in everything
    assert "Funds received" not in everything


def test_account_reference_is_extracted_for_bill_payments():
    result = adapt(line(details="Pay Bill to 123456 - Test Co Acc. ab-1234567", paid_in="", withdrawn="-5.00"))
    (payment,) = result.payments
    assert (payment["bill_ref_raw"], payment["bill_ref"]) == ("ab-1234567", "AB1234567")


def test_a_word_that_merely_starts_with_acc_is_not_an_account_reference():
    result = adapt(line(details="Pay Bill to 123456 - ACCOUNTS DEPT", paid_in="", withdrawn="-5.00"))
    assert result.payments[0]["bill_ref_raw"] is None


def test_the_owners_own_number_after_by_is_not_taken_as_the_counterparty():
    details = "Customer Bundle Purchase to 999999 TEST BUNDLES by - 254700***003 Owner"
    result = adapt(line(details=details, paid_in="", withdrawn="-20.00"))
    assert result.payments[0]["phone_hint"] is None


def test_a_receipt_with_several_lines_keeps_a_unique_line_key():
    lines = [
        line(1, details="Customer Transfer Fuliza to - 254700***000 X", paid_in="", withdrawn="-30.00"),
        line(2, details="OverDraft of Credit Party", paid_in="30.00"),
        line(3, details="Pay Bill Charge", paid_in="", withdrawn="-1.00"),
    ]
    payments = adapt(*lines).payments
    assert {p["receipt_no"] for p in payments} == {"QWE1234ABC"}
    keys = [tuple(p[c] for c in PAYMENT_KEY) for p in payments]
    assert len(set(keys)) == 3
    assert [p["kind"] for p in payments] == ["PAYMENT", "OVERDRAFT_DRAW", "CHARGE"]


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"paid_in": "5.00", "withdrawn": "-5.00"}, "both_amounts"),
        ({"paid_in": "", "withdrawn": ""}, "no_amount"),
        ({"transaction_status": ""}, "missing_status"),
    ],
)
def test_lines_that_cannot_be_staged_are_rejected_not_dropped(overrides, reason):
    result = adapt(line(3, **overrides))
    assert result.payments == []
    assert [(r.source_row, r.reason) for r in result.rejected] == [(3, reason)]
    assert not result.issues


def test_rejected_cells_leave_out_the_details_text():
    result = adapt(line(paid_in="", withdrawn=""))
    assert FAKE_NAME not in repr(result.rejected)


@pytest.mark.parametrize(
    ("overrides", "field", "code"),
    [
        ({"paid_in": "12x"}, "paid_in", "not_numeric"),
        ({"completion_time": "soon"}, "paid_ts", "unparseable_timestamp"),
        ({"paid_in": "-270.00"}, "paid_in", "unexpected_sign"),
        ({"details": "OverDraft of Credit Party", "paid_in": "", "withdrawn": "-5.00"}, "direction", "unexpected_direction"),
        ({"receipt_no": ""}, "receipt_no", "missing_receipt"),
        ({"receipt_no": "short"}, "receipt_no", "bad_receipt_format"),
    ],
)
def test_unusable_fields_are_reported_and_the_line_is_still_staged(overrides, field, code):
    result = adapt(line(4, **overrides))
    assert len(result.payments) == 1 and not result.rejected
    assert result.issues == [Issue(4, field, code)]


def test_bad_amount_keeps_the_direction_but_not_a_made_up_amount():
    (payment,) = adapt(line(paid_in="12x")).payments
    assert (payment["direction"], payment["amount_minor"]) == ("IN", None)


def test_unparseable_time_keeps_the_raw_text_for_the_check_to_see():
    (payment,) = adapt(line(completion_time="soon")).payments
    assert (payment["paid_ts_raw"], payment["paid_ts_utc"]) == ("soon", None)


def test_reader_bad_rows_become_rejections():
    result = adapt(line(), bad_rows=[BadRow(9, ["x", "y"], "wrong_field_count")])
    assert result.rejected == [Rejected(9, "wrong_field_count", ["x", "y"])]
    assert len(result.payments) == 1


def test_missing_columns_raise_a_coded_error():
    with pytest.raises(CsvError) as exc:
        adapt_statement([Row(1, {"receipt_no": "QWE1234ABC"})], source_file="f.csv")
    assert exc.value.code == "missing_columns"


def test_no_rows_is_an_empty_result():
    result = adapt_statement([], source_file="f.csv")
    assert (result.payments, result.issues, result.rejected) == ([], [], [])


def test_unknown_provider_or_country_is_a_programming_error():
    with pytest.raises(ValueError):
        adapt_statement([], source_file="f", provider="NOPE")
    with pytest.raises(ValueError):
        adapt_statement([], source_file="f", country="ZZ")


def test_other_countries_use_their_own_currency_and_clock():
    row = line(paid_in="25,000", details="Funds received from - 2547***001 X")
    result = adapt_statement([row], source_file="f.csv", provider="MPESA_TZ", country="TZ")
    (payment,) = result.payments
    assert (payment["currency"], payment["amount_minor"]) == ("TZS", 2500000)
    assert payment["phone_hint"] == "2547***001"


def test_csv_statement_with_title_lines_end_to_end():
    text = (
        "Statement of Account\n"
        "Customer: TEST SHOP\n"
        "\n"
        "Receipt No.,Completion Time,Details,Transaction Status,Paid In,Withdrawn,Balance\n"
        'QWE1234ABC,2026-10-29 08:00:00,Funds received from - 254700***001 Test Person,Completed,"1,100.00",,"1,100.00"\n'
        "ASD5555FGH,2026-10-29 09:15:00,Merchant Payment to 654321 - Test Shop,Completed,,-70.00,1030.00\n"
    )
    table = parse_table(text.encode(), STATEMENT_HEADER_HINTS)
    result = adapt_statement(table.rows, table.bad_rows, source_file="statement.csv")
    assert not result.issues and not result.rejected
    assert [(p["direction"], p["amount_minor"]) for p in result.payments] == [("IN", 110000), ("OUT", 7000)]


def test_pdf_statement_end_to_end():
    pytest.importorskip("pdfplumber")
    from pdf_fixture import statement_pdf

    from payrecon.pdf_table import read_pdf_statement

    statement = read_pdf_statement(statement_pdf())
    result = adapt_statement(statement.rows, statement.bad_rows, source_file="fixture.pdf")
    assert not result.rejected and len(result.payments) == 5
    assert [p["kind"] for p in result.payments] == [
        "PAYMENT", "OVERDRAFT_DRAW", "OTHER", "PAYMENT", "CHARGE",
    ]
    assert [p["direction"] for p in result.payments] == ["OUT", "IN", "OUT", "IN", "OUT"]
    keys = [tuple(p[c] for c in PAYMENT_KEY) for p in result.payments]
    assert len(set(keys)) == len(keys)
    assert result.payments[0]["phone_hint"] == "254700***000"
    assert "Test Person" not in repr(result)
