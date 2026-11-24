import json
import sqlite3
from hashlib import sha256

import pytest

from payrecon.adapters.base import Rejected
from payrecon.adapters.mpesa_statement import adapt_statement
from payrecon.adapters.orders import ORDER_HEADER_HINTS, AdaptedOrders, adapt_orders, template_csv
from payrecon.csv_utils import Row, parse_table
from payrecon.db import connect, init_schema
from payrecon.loader import AlreadyLoadedError, FileInfo, load_orders, load_payments
from payrecon.schema import ORDER_COLUMNS, column_names


@pytest.fixture
def conn():
    connection = connect()
    init_schema(connection)
    yield connection
    connection.close()


def digest(text):
    return sha256(text.encode()).hexdigest()


def info(name="orders_web.csv", text="orders-one"):
    return FileInfo(name=name, sha256=digest(text))


def order_row(number, **overrides):
    data = {"order_id": f"ORD-100{number}", "channel": "WEB", "phone": "0712345678",
            "order_time": "2026-10-31 14:05:09", "amount": "1500.00", "status": "COMPLETED",
            "bill_ref": f"ORD100{number}"}
    data.update(overrides)
    return Row(number, data)


def adapted(*rows, **options):
    options.setdefault("salt", "s")
    return adapt_orders(rows, source_file="orders_web.csv", **options)


def count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_an_orders_file_is_loaded_with_its_file_record(conn):
    result = load_orders(conn, adapted(order_row(1), order_row(2)), info())
    assert (result.rows_staged, result.rows_rejected, result.issues_recorded) == (2, 0, 0)
    file_row = conn.execute("SELECT * FROM source_files").fetchone()
    assert (file_row["kind"], file_row["provider"], file_row["name"]) == ("orders", None, "orders_web.csv")
    assert (file_row["rows_read"], file_row["rows_staged"], file_row["rows_rejected"]) == (2, 2, 0)
    assert file_row["declared_paid_in_minor"] is None
    assert count(conn, "orders") == 2 and count(conn, "statement_summary") == 0


def test_every_order_column_survives_the_round_trip(conn):
    adapt = adapted(order_row(1, customer_id="C-9"))
    load_orders(conn, adapt, info())
    stored = dict(conn.execute("SELECT * FROM orders").fetchone())
    for column in column_names(ORDER_COLUMNS):
        assert stored[column] == adapt.orders[0][column], column
    assert stored["phone_hint"] == "254712***678" and len(stored["phone_hash"]) == 64


def test_issues_file_level_issues_and_rejects_are_stored(conn):
    base = adapted(order_row(1, amount="12x"), order_row(2, order_id=""), salt=None)
    base.rejected.append(Rejected(9, "wrong_field_count", ["a", "é"]))
    result = load_orders(conn, base, info())
    assert (result.rows_staged, result.rows_rejected, result.issues_recorded) == (1, 2, 2)
    issues = sorted(tuple(r) for r in conn.execute("SELECT source_row, field, code FROM load_issues"))
    assert issues == [(0, "phone_hash", "no_phone_salt"), (1, "amount", "not_numeric")]
    reasons = {r["source_row"]: r["reason"] for r in conn.execute("SELECT * FROM rejected_rows")}
    assert reasons == {2: "missing_order_id", 9: "wrong_field_count"}
    cells = conn.execute("SELECT cells_json FROM rejected_rows WHERE source_row = 9").fetchone()[0]
    assert json.loads(cells) == ["a", "é"]


def test_the_same_orders_content_cannot_be_loaded_twice(conn):
    load_orders(conn, adapted(order_row(1)), info("first.csv"))
    with pytest.raises(AlreadyLoadedError) as exc:
        load_orders(conn, adapted(order_row(1)), info("renamed.csv"))
    assert exc.value.existing_name == "first.csv"
    assert (count(conn, "source_files"), count(conn, "orders")) == (1, 1)


def test_orders_and_payments_with_identical_bytes_are_different_kinds(conn):
    same = info("x.csv", "same-bytes")
    load_orders(conn, adapted(order_row(1)), same)
    payments = adapt_statement(
        [Row(1, {"receipt_no": "QWE1234ABC", "completion_time": "2026-10-31 14:00:00", "details": "Funds received from - 254700***001 X",
                 "transaction_status": "Completed", "paid_in": "10.00", "withdrawn": "", "balance": "10.00"})],
        source_file="x.csv",
    )
    load_payments(conn, payments, same)
    assert sorted(r[0] for r in conn.execute("SELECT kind FROM source_files")) == ["orders", "payments"]
    with pytest.raises(AlreadyLoadedError):
        load_orders(conn, adapted(order_row(1)), FileInfo("again.csv", same.sha256))


def test_a_failure_leaves_no_partial_orders_file(conn):
    good = adapted(order_row(1), order_row(2))
    bad = adapted(order_row(3))
    bad.orders[0]["channel"] = None  # NOT NULL column
    with pytest.raises(sqlite3.IntegrityError):
        load_orders(conn, AdaptedOrders(good.orders + bad.orders, [], [], {}), info())
    for table in ("source_files", "orders", "load_issues", "rejected_rows"):
        assert count(conn, table) == 0, table
    load_orders(conn, good, info())
    assert count(conn, "orders") == 2


def test_an_order_that_breaks_the_contract_is_refused_before_any_write(conn):
    broken = adapted(order_row(1))
    broken.orders[0]["customer_name"] = "x"
    with pytest.raises(ValueError):
        load_orders(conn, broken, info())
    assert count(conn, "source_files") == 0


def test_the_template_loads_and_duplicates_stay_visible_to_sql(conn):
    table = parse_table(template_csv().encode(), ORDER_HEADER_HINTS)
    result = adapt_orders(table.rows, table.bad_rows, source_file="template.csv", salt="s")
    load_orders(conn, result, info("template.csv", "template"))
    assert count(conn, "orders") == 2
    twice = adapted(order_row(1), order_row(2, order_id="ord-1001"))
    load_orders(conn, twice, info("dupes.csv", "dupes"))
    dupes = conn.execute("SELECT order_id, COUNT(*) FROM orders GROUP BY order_id HAVING COUNT(*) > 1").fetchall()
    assert [tuple(r) for r in dupes] == [("ORD-1001", 3)]


def test_orders_and_payments_join_on_bill_reference_in_plain_sql(conn):
    load_orders(conn, adapted(order_row(1), order_row(2, amount="750.00")), info())
    payments = adapt_statement(
        [
            Row(1, {"receipt_no": "AAA1111111", "completion_time": "2026-10-31 14:10:00",
                    "details": "Pay Bill from 254700***001 - Test Acc. ORD-1001", "transaction_status": "Completed",
                    "paid_in": "1,500.00", "withdrawn": "", "balance": "1,500.00"}),
            Row(2, {"receipt_no": "BBB2222222", "completion_time": "2026-10-31 14:20:00",
                    "details": "Pay Bill from 254700***002 - Test Acc. ORD-1002", "transaction_status": "Completed",
                    "paid_in": "700.00", "withdrawn": "", "balance": "2,200.00"}),
        ],
        source_file="statement.csv",
    )
    assert [p["bill_ref"] for p in payments.payments] == ["ORD1001", "ORD1002"]
    load_payments(conn, payments, info("statement.csv", "statement"))
    rows = conn.execute(
        "SELECT o.order_id, p.receipt_no, o.amount_minor - p.amount_minor AS gap"
        " FROM orders o JOIN payments p ON p.bill_ref = o.bill_ref"
        " AND p.direction = 'IN' AND p.kind = 'PAYMENT' ORDER BY o.order_id"
    ).fetchall()
    assert [tuple(r) for r in rows] == [("ORD-1001", "AAA1111111", 0), ("ORD-1002", "BBB2222222", 5000)]
