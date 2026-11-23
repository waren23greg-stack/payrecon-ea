import json
import sqlite3
from hashlib import sha256

import pytest

from payrecon.adapters.base import AdaptedPayments, Issue, Rejected
from payrecon.adapters.mpesa_statement import adapt_statement
from payrecon.csv_utils import Row
from payrecon.db import connect, init_schema, new_run_db
from payrecon.loader import AlreadyLoadedError, FileInfo, file_sha256, load_payments
from payrecon.schema import PAYMENT_COLUMNS, column_names


@pytest.fixture
def conn():
    connection = connect()
    init_schema(connection)
    yield connection
    connection.close()


def digest(text):
    return sha256(text.encode()).hexdigest()


def info(name="statement.pdf", text="one"):
    return FileInfo(name=name, sha256=digest(text), period_text="01 Oct 2026 - 31 Oct 2026")


def row(number, **overrides):
    data = {
        "receipt_no": f"QWE1234AB{number}",
        "completion_time": "2026-10-29 08:00:00",
        "details": "Funds received from - 254700***001 Test Person",
        "transaction_status": "Completed",
        "paid_in": "270.00",
        "withdrawn": "",
        "balance": "270.00",
    }
    data.update(overrides)
    return Row(number, data)


def adapted(*rows):
    return adapt_statement(rows, source_file="statement.pdf")


def count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_a_file_and_its_payments_are_loaded_together(conn):
    result = load_payments(conn, adapted(row(1), row(2, paid_in="", withdrawn="-5.00")), info())
    assert (result.rows_staged, result.rows_rejected, result.issues_recorded) == (2, 0, 0)
    file_row = conn.execute("SELECT * FROM source_files").fetchone()
    assert file_row["file_id"] == result.file_id
    assert (file_row["name"], file_row["kind"], file_row["provider"]) == ("statement.pdf", "payments", "MPESA_KE")
    assert (file_row["rows_read"], file_row["rows_staged"], file_row["rows_rejected"]) == (2, 2, 0)
    assert file_row["period_text"] == "01 Oct 2026 - 31 Oct 2026"
    assert file_row["loaded_at"].endswith("Z")
    assert count(conn, "payments") == 2
    assert conn.execute("SELECT DISTINCT file_id FROM payments").fetchall()[0][0] == result.file_id


def test_every_contract_column_survives_the_round_trip(conn):
    adapt = adapted(row(1, details="Pay Bill to 123456 - Test Co Acc. ab-1234567", paid_in="", withdrawn="-30.00"))
    load_payments(conn, adapt, info())
    stored = dict(conn.execute("SELECT * FROM payments").fetchone())
    for column in column_names(PAYMENT_COLUMNS):
        assert stored[column] == adapt.payments[0][column], column
    assert stored["bill_ref"] == "AB1234567" and stored["amount_minor"] == 3000


def test_issues_and_rejected_rows_are_stored_with_the_file(conn):
    base = adapted(row(1, paid_in="12x"), row(2, paid_in="5.00", withdrawn="-5.00"))
    base.rejected.append(Rejected(9, "wrong_field_count", ["a", "b,c", "é"]))
    result = load_payments(conn, base, info())
    assert (result.rows_staged, result.rows_rejected, result.issues_recorded) == (1, 2, 1)
    issues = [tuple(r) for r in conn.execute("SELECT source_row, field, code FROM load_issues")]
    assert issues == [(1, "paid_in", "not_numeric")]
    rejected = {r["source_row"]: r for r in conn.execute("SELECT * FROM rejected_rows")}
    assert rejected[2]["reason"] == "both_amounts"
    assert json.loads(rejected[9]["cells_json"]) == ["a", "b,c", "é"]
    file_row = conn.execute("SELECT rows_read, rows_staged, rows_rejected FROM source_files").fetchone()
    assert tuple(file_row) == (3, 1, 2)


def test_the_same_content_cannot_be_loaded_twice_even_under_another_name(conn):
    load_payments(conn, adapted(row(1)), info("first.pdf"))
    with pytest.raises(AlreadyLoadedError) as exc:
        load_payments(conn, adapted(row(1)), info("renamed.pdf"))
    assert exc.value.existing_name == "first.pdf"
    assert "renamed.pdf" in str(exc.value)
    assert (count(conn, "source_files"), count(conn, "payments")) == (1, 1)


def test_different_content_loads_alongside_the_first_file(conn):
    load_payments(conn, adapted(row(1)), info("a.pdf", "one"))
    load_payments(conn, adapted(row(2)), info("b.pdf", "two"))
    assert (count(conn, "source_files"), count(conn, "payments")) == (2, 2)


def test_a_failure_part_way_leaves_nothing_behind(conn):
    good, bad = adapted(row(1), row(2)), None
    bad = adapted(row(3))
    bad.payments[0]["provider"] = None  # NOT NULL column: the database refuses it
    mixed = AdaptedPayments(good.payments + bad.payments, [Issue(1, "x", "y")], [])
    with pytest.raises(sqlite3.IntegrityError):
        load_payments(conn, mixed, info())
    for table in ("source_files", "payments", "load_issues", "rejected_rows", "statement_summary"):
        assert count(conn, table) == 0, table
    load_payments(conn, good, info())  # the same hash can still be loaded afterwards
    assert count(conn, "payments") == 2


def test_a_payment_that_breaks_the_contract_is_refused_before_any_write(conn):
    broken = adapted(row(1))
    broken.payments[0]["surprise"] = 1
    with pytest.raises(ValueError):
        load_payments(conn, broken, info())
    assert count(conn, "source_files") == 0


@pytest.mark.parametrize("bad", ["", "xyz", "A" * 64, "a" * 63])
def test_a_malformed_hash_is_refused(conn, bad):
    with pytest.raises(ValueError):
        load_payments(conn, adapted(row(1)), FileInfo(name="x", sha256=bad))
    assert count(conn, "source_files") == 0


def test_a_file_with_only_rejects_is_still_recorded(conn):
    only_rejects = AdaptedPayments([], [], [Rejected(1, "no_amount", ["x"])])
    result = load_payments(conn, only_rejects, info())
    assert (result.rows_staged, result.rows_rejected) == (0, 1)
    assert tuple(conn.execute("SELECT provider, rows_staged, rows_rejected FROM source_files").fetchone()) == (None, 0, 1)


def test_the_printed_summary_and_declared_totals_are_stored(conn):
    summary = {
        "SEND MONEY": ("0.00", "100.00"),
        "RECEIVED MONEY": ("300.00", "0.00"),
        "TOTAL": ("300.00", "100.00"),
    }
    result = load_payments(conn, adapted(row(1)), info(), summary)
    stored = {r["label"]: (r["paid_in_minor"], r["paid_out_minor"]) for r in conn.execute("SELECT * FROM statement_summary")}
    assert stored == {"SEND MONEY": (0, 10000), "RECEIVED MONEY": (30000, 0), "TOTAL": (30000, 10000)}
    declared = conn.execute("SELECT declared_paid_in_minor, declared_paid_out_minor FROM source_files").fetchone()
    assert tuple(declared) == (30000, 10000)
    assert result.issues_recorded == 0


def test_a_summary_that_does_not_foot_is_visible_to_plain_sql(conn):
    summary = {"A:": ("100.00", "0.00"), "B": ("100.00", "0.00"), "TOTAL": ("204.28", "0.00")}
    load_payments(conn, adapted(row(1)), info(), summary)
    gap = conn.execute(
        "SELECT f.declared_paid_in_minor - SUM(s.paid_in_minor) FROM source_files f"
        " JOIN statement_summary s ON s.file_id = f.file_id WHERE s.label <> 'TOTAL'"
    ).fetchone()[0]
    assert gap == 428


def test_detail_lines_can_be_compared_with_the_declared_total_in_plain_sql(conn):
    rows = [row(1, paid_in="100.00"), row(2, paid_in="200.00"), row(3, paid_in="", withdrawn="-50.00")]
    load_payments(conn, adapted(*rows), info(), {"TOTAL": ("300.00", "50.00")})
    sums = conn.execute(
        "SELECT SUM(CASE WHEN p.direction = 'IN' THEN p.amount_minor END),"
        " SUM(CASE WHEN p.direction = 'OUT' THEN p.amount_minor END),"
        " MAX(f.declared_paid_in_minor), MAX(f.declared_paid_out_minor)"
        " FROM payments p JOIN source_files f ON f.file_id = p.file_id"
    ).fetchone()
    assert tuple(sums) == (30000, 5000, 30000, 5000)


def test_an_unreadable_summary_figure_becomes_a_file_level_issue(conn):
    summary = {"GOOD": ("1.00", "2.00"), "BAD": ("x", "2.00"), "TOTAL": ("1.00", "2.00")}
    result = load_payments(conn, adapted(row(1)), info(), summary)
    assert result.issues_recorded == 1
    assert [tuple(r) for r in conn.execute("SELECT source_row, field, code FROM load_issues")] == [
        (0, "summary:BAD", "not_numeric")
    ]
    assert {r["label"] for r in conn.execute("SELECT label FROM statement_summary")} == {"GOOD", "TOTAL"}


def test_summary_currency_follows_the_argument(conn):
    load_payments(conn, adapted(row(1)), info(), {"TOTAL": ("25,000", "0")}, currency="UGX")
    assert conn.execute("SELECT declared_paid_in_minor FROM source_files").fetchone()[0] == 25000


def test_file_sha256_accepts_bytes_and_paths(tmp_path):
    assert file_sha256(b"abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    path = tmp_path / "x.bin"
    path.write_bytes(b"abc")
    assert file_sha256(path) == file_sha256(str(path)) == file_sha256(b"abc")


def test_loaded_data_survives_reopening_the_run_database(tmp_path):
    run = new_run_db(tmp_path)
    load_payments(run.conn, adapted(row(1), row(2)), info())
    run.conn.close()
    reopened = connect(run.path)
    try:
        assert count(reopened, "payments") == 2
    finally:
        reopened.close()


def test_a_synthetic_pdf_statement_loads_end_to_end(conn):
    pytest.importorskip("pdfplumber")
    from pdf_fixture import statement_pdf

    from payrecon.pdf_table import read_pdf_statement

    data = statement_pdf()
    statement = read_pdf_statement(data)
    result = adapt_statement(statement.rows, statement.bad_rows, source_file="fixture.pdf")
    file_info = FileInfo("fixture.pdf", file_sha256(data), provider="MPESA_KE", period_text=statement.period_text)
    loaded = load_payments(conn, result, file_info, statement.summary)
    assert (loaded.rows_staged, loaded.rows_rejected, loaded.issues_recorded) == (5, 0, 0)
    declared = conn.execute("SELECT declared_paid_in_minor, declared_paid_out_minor, period_text FROM source_files").fetchone()
    assert tuple(declared) == (30000, 10000, "01 Oct 2026 - 31 Oct 2026")
    kinds = [r[0] for r in conn.execute("SELECT kind FROM payments ORDER BY id")]
    assert kinds == ["PAYMENT", "OVERDRAFT_DRAW", "OTHER", "PAYMENT", "CHARGE"]
    with pytest.raises(AlreadyLoadedError):
        load_payments(conn, result, file_info, statement.summary)
