import sys

import pytest

pytest.importorskip("pdfplumber")

from pdf_fixture import (  # noqa: E402
    build_pdf,
    header_items,
    row_items,
    statement_pdf,
)

from payrecon.csv_utils import CsvError  # noqa: E402
from payrecon.pdf_table import PdfSupportError, read_pdf_statement  # noqa: E402

COLUMNS = {
    "receipt_no", "completion_time", "details", "transaction_status",
    "paid_in", "withdrawn", "balance",
}


def test_rows_use_the_csv_column_names_and_stay_text():
    statement = read_pdf_statement(statement_pdf())
    assert statement.pages == 2
    assert len(statement.rows) == 5 and not statement.bad_rows
    assert all(set(r.data) == COLUMNS for r in statement.rows)
    assert [r.number for r in statement.rows] == [1, 2, 3, 4, 5]
    last = statement.rows[4].data
    assert last["completion_time"] == "2026-10-28 07:30:10"
    assert last["withdrawn"] == "-1.00"  # raw text, typed later by normalize.py


def test_one_receipt_can_carry_several_lines():
    rows = read_pdf_statement(statement_pdf()).rows
    assert [r.data["receipt_no"] for r in rows[:2]] == ["QWE1234ABC", "QWE1234ABC"]
    assert rows[0].data["withdrawn"] == "-30.00" and rows[0].data["paid_in"] == ""
    assert rows[1].data["paid_in"] == "30.00" and rows[1].data["withdrawn"] == ""


def test_wrapped_details_are_joined_and_status_is_kept_apart():
    rows = read_pdf_statement(statement_pdf()).rows
    assert rows[0].data["details"] == "Customer Transfer Fuliza to - 254700***000 Test Person"
    assert rows[2].data["details"] == "Merchant Pay to 123456 TEST SHOP NAIROBI BRANCH"
    assert {r.data["transaction_status"] for r in rows} == {"Completed"}
    assert rows[2].data["balance"] == "1,030.00"


def test_summary_and_period_are_read_as_printed():
    statement = read_pdf_statement(statement_pdf())
    assert statement.period_text == "01 Oct 2026 - 31 Oct 2026"
    assert statement.summary == {
        "SEND MONEY": ("0.00", "100.00"),
        "RECEIVED MONEY": ("300.00", "0.00"),
        "TOTAL": ("300.00", "100.00"),
    }


def test_owner_details_and_footer_text_are_never_read():
    statement = read_pdf_statement(statement_pdf())
    text = repr(statement)
    for private in ("TEST CUSTOMER", "0700000000", "Verification", "ZZZZ9999", "Disclaimer"):
        assert private not in text


@pytest.mark.parametrize("shift", [-20, 25])
def test_column_boundaries_come_from_the_header_not_from_constants(shift):
    expected = [r.data for r in read_pdf_statement(statement_pdf()).rows]
    shifted = read_pdf_statement(statement_pdf(shift=shift))
    assert [r.data for r in shifted.rows] == expected


def test_later_pages_without_a_header_reuse_the_geometry_and_report_furniture():
    page1 = [*header_items(60), *row_items(85, "AAA1111111", "2026-10-30", "10:00:00", ["One"],
                                           paid_in="5.00", balance="5.00")]
    page2 = [(480, 20, "Page 2 of 2"),
             *row_items(40, "BBB2222222", "2026-10-29", "10:00:00", ["Two"],
                        withdrawn="-2.00", balance="3.00")]
    statement = read_pdf_statement(build_pdf([page1, page2]))
    assert [r.data["receipt_no"] for r in statement.rows] == ["AAA1111111", "BBB2222222"]
    assert statement.rows[1].data["withdrawn"] == "-2.00"
    assert statement.skipped_pages == []
    # page furniture is reported, and does not touch the good row above it
    assert [(b.number, b.reason) for b in statement.bad_rows] == [(2, "unattached_text")]
    assert statement.rows[0].data["balance"] == "5.00"


def test_leading_pages_without_a_table_are_reported():
    cover = [(50, 50, "Cover page with no table")]
    table = [*header_items(60), *row_items(85, "AAA1111111", "2026-10-30", "10:00:00", ["One"],
                                           paid_in="5.00", balance="5.00")]
    statement = read_pdf_statement(build_pdf([cover, table]))
    assert statement.skipped_pages == [1] and len(statement.rows) == 1


def test_lines_the_layout_cannot_place_become_bad_rows_not_losses():
    good = row_items(120, "GOOD000001", "2026-10-30", "10:00:00", ["Fine"],
                     paid_in="5.00", balance="5.00")
    no_balance = [i for i in row_items(140, "NOBAL00001", "2026-10-30", "10:00:00", ["Fine"],
                                       paid_in="7.00", balance="7.77") if i[2] != "7.77"]
    no_time = [i for i in row_items(160, "NOTIME0001", "2026-10-30", "99:99:99", ["Fine"],
                                    paid_in="9.00", balance="9.00") if i[2] != "99:99:99"]
    page = [*header_items(60), (177.5, 90, "stray words above any receipt"), *good, *no_balance, *no_time]
    statement = read_pdf_statement(build_pdf([page]))
    assert [r.data["receipt_no"] for r in statement.rows] == ["GOOD000001"]
    assert [(b.number, b.reason) for b in statement.bad_rows] == [
        (1, "text_before_first_receipt"),
        (3, "unexpected_number_count"),
        (4, "missing_timestamp"),
    ]


@pytest.mark.parametrize(
    ("data", "code"),
    [
        (b"not a pdf at all", "not_pdf"),
        (build_pdf([[]]), "pdf_no_text"),
        (build_pdf([[(50, 50, "hello world")]]), "header_not_found"),
    ],
)
def test_unusable_pdfs_raise_coded_errors(data, code):
    with pytest.raises(CsvError) as exc:
        read_pdf_statement(data)
    assert exc.value.code == code


def test_a_path_works_as_well_as_bytes(tmp_path):
    path = tmp_path / "statement.pdf"
    path.write_bytes(statement_pdf())
    assert len(read_pdf_statement(path).rows) == 5
    assert len(read_pdf_statement(str(path)).rows) == 5


def test_missing_pdfplumber_gives_an_actionable_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "pdfplumber", None)
    with pytest.raises(PdfSupportError, match=r"payrecon-ea\[pdf\]"):
        read_pdf_statement(statement_pdf())


def test_password_and_other_open_failures_are_told_apart(monkeypatch):
    import pdfplumber

    class PDFPasswordIncorrect(Exception):  # stands in for pdfminer's class
        pass

    class PdfminerException(Exception):  # pdfplumber wraps the original in this
        pass

    def refuse(*args, **kwargs):
        raise PdfminerException(PDFPasswordIncorrect())

    monkeypatch.setattr(pdfplumber, "open", refuse)
    with pytest.raises(CsvError) as missing:
        read_pdf_statement(statement_pdf())
    with pytest.raises(CsvError) as wrong:
        read_pdf_statement(statement_pdf(), password="guess")
    assert (missing.value.code, wrong.value.code) == (
        "pdf_password_required",
        "pdf_password_incorrect",
    )

    def explode(*args, **kwargs):
        raise ValueError("boom")

    monkeypatch.setattr(pdfplumber, "open", explode)
    with pytest.raises(CsvError) as other:
        read_pdf_statement(statement_pdf())
    assert other.value.code == "pdf_unreadable"
