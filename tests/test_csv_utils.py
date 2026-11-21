import pytest

from payrecon.csv_utils import (
    CsvError,
    decode_bytes,
    normalize_header,
    parse_table,
    read_table,
    sniff_delimiter,
)

HINTS = ["Receipt No.", "Completion Time", "Paid In", "Details"]

STATEMENT = (
    "Statement of Account\n"
    "Customer: TEST SHOP\n"
    "\n"
    "Receipt No.,Completion Time,Details,Paid In,Balance\n"
    "AAA1111111,31-10-2026 14:05:09,Pay Bill from 2547***123 - ORD1001,\"1,500.00\",9000.00\n"
    "AAA2222222,31-10-2026 15:00:00,Pay Bill from 2547***456 - ORD1002,200.00,9200.00\n"
).encode()


@pytest.mark.parametrize(
    ("data", "text", "encoding"),
    [
        (b"a,b\n1,2\n", "a,b\n1,2\n", "utf-8"),
        (b"\xef\xbb\xbfa,b\n", "a,b\n", "utf-8-sig"),
        ("a,b\n1,2\n".encode("utf-16"), "a,b\n1,2\n", "utf-16"),
        (b"caf\xe9", "caf\u00e9", "cp1252"),
        ("Wanjir\u00fb".encode("utf-8"), "Wanjir\u00fb", "utf-8"),
        (b"caf\x81", "caf\x81", "latin-1"),
    ],
)
def test_decode_bytes(data, text, encoding):
    assert decode_bytes(data) == (text, encoding)


@pytest.mark.parametrize(
    ("text", "delimiter"),
    [
        ("a,b,c\n1,2,3\n4,5,6\n", ","),
        ("a;b;c\n1;2;3\n4;5;6\n", ";"),
        ("a\tb\tc\n1\t2\t3\n", "\t"),
        ("a|b|c\n1|2|3\n", "|"),
        ('a;b;c\n1;"x, y";3\n4;"p, q";6\n', ";"),
        ("Title line\nOwner: X\na;b;c\n1;2;3\n4;5;6\n", ";"),
        ("ref;amount\nA1;1500,50\nA2;20,00\n", ";"),
        ("single\ncolumn\nonly\n", ","),
    ],
)
def test_sniff_delimiter(text, delimiter):
    assert sniff_delimiter(text) == delimiter


def test_normalize_header():
    assert normalize_header("Receipt No.") == "receipt_no"
    assert normalize_header("  Paid In ") == "paid_in"
    assert normalize_header("\ufeffTransaction ID") == "transaction_id"
    assert normalize_header("Bill Ref Number") == "bill_ref_number"
    assert normalize_header("***") == ""


def test_statement_with_title_lines_above_the_header():
    table = parse_table(STATEMENT, HINTS)
    assert table.encoding == "utf-8"
    assert table.delimiter == ","
    assert table.header == ["receipt_no", "completion_time", "details", "paid_in", "balance"]
    assert table.header_row == 4
    assert table.preamble == [["Statement of Account"], ["Customer: TEST SHOP"], []]
    assert len(table.rows) == 2 and not table.bad_rows
    first = table.rows[0]
    assert first.number == 1
    assert first.data["receipt_no"] == "AAA1111111"
    assert first.data["paid_in"] == "1,500.00"  # still text: normalize.py types it
    assert table.rows[1].number == 2


def test_without_hints_the_first_non_blank_line_is_the_header():
    table = parse_table(b"\n\nid,amount\n1,10\n")
    assert table.header == ["id", "amount"]
    assert table.header_row == 3
    assert table.rows[0].data == {"id": "1", "amount": "10"}


def test_blank_rows_are_counted_and_still_advance_row_numbers():
    table = parse_table(b"id,amount\n1,10\n\n2,20\n,\n")
    assert [r.number for r in table.rows] == [1, 3]
    assert table.blank_rows == 2


def test_wrong_field_count_goes_to_bad_rows_not_the_bin():
    table = parse_table(b"id,amount\n1,10\n2,20,extra\n3\n4,40\n")
    assert [r.number for r in table.rows] == [1, 4]
    assert [(b.number, b.reason) for b in table.bad_rows] == [
        (2, "wrong_field_count"),
        (3, "wrong_field_count"),
    ]
    assert table.bad_rows[0].cells == ["2", "20", "extra"]


def test_duplicate_and_blank_headers_get_unique_names():
    table = parse_table(b"amount,amount,,amount\n1,2,3,4\n")
    assert table.header == ["amount", "amount_2", "unnamed_3", "amount_3"]


def test_excel_text_markers_are_unwrapped():
    table = parse_table(b'phone,ref\n"=""0712345678""",\'0042\n')
    assert table.rows[0].data == {"phone": "0712345678", "ref": "0042"}


def test_quoted_newline_stays_in_one_field_and_crlf_is_handled():
    table = parse_table(b'id,note\r\n1,"line one\nline two"\r\n2,ok\r\n')
    assert table.rows[0].data["note"] == "line one\nline two"
    assert table.rows[1].data["note"] == "ok"
    assert not table.bad_rows


def test_semicolon_file_with_decimal_commas():
    table = parse_table(b"ref;amount\nA1;1500,50\nA2;20,00\n")
    assert table.delimiter == ";"
    assert table.rows[0].data["amount"] == "1500,50"


def test_excel_sep_directive_wins_and_is_not_data():
    table = parse_table(b"sep=;\nid;note\n1;a,b\n")
    assert table.delimiter == ";"
    assert table.header == ["id", "note"]
    assert table.header_row == 1
    assert table.rows[0].data["note"] == "a,b"


def test_explicit_delimiter_overrides_sniffing():
    table = parse_table(b"a;b,c\n1;2,3\n", delimiter=";")
    assert table.header == ["a", "b_c"]


def test_utf16_tab_export_from_excel():
    raw = "Name\tAmount\nAsha\t100\n".encode("utf-16")
    table = parse_table(raw)
    assert (table.encoding, table.delimiter) == ("utf-16", "\t")
    assert table.rows[0].data == {"name": "Asha", "amount": "100"}


@pytest.mark.parametrize(
    ("data", "hints", "code"),
    [
        (b"", (), "empty_file"),
        (b"  \r\n \n", (), "empty_file"),
        (b"\xef\xbb\xbf", (), "empty_file"),
        (b",,\n,,\n", (), "empty_file"),
        (b"PK\x03\x04rest-of-xlsx", (), "not_csv_excel"),
        (b"\xd0\xcf\x11\xe0\xa1\xb1", (), "not_csv_excel"),
        (b"a,b\n1,\x002\n", (), "binary_file"),
        (b"x,y\n1,2\n", ("Receipt No.", "Paid In"), "header_not_found"),
    ],
)
def test_unreadable_files_raise_coded_errors(data, hints, code):
    with pytest.raises(CsvError) as exc:
        parse_table(data, hints)
    assert exc.value.code == code


def test_header_hint_search_stops_at_max_scan():
    data = b"\n" * 5 + b"Receipt No.,Paid In\nA,1\n"
    assert parse_table(data, HINTS, max_scan=10).header_row == 6
    with pytest.raises(CsvError):
        parse_table(data, HINTS, max_scan=3)


def test_read_table_reads_a_file(tmp_path):
    path = tmp_path / "statement.csv"
    path.write_bytes(STATEMENT)
    table = read_table(path, HINTS)
    assert len(table.rows) == 2 and table.header_row == 4
