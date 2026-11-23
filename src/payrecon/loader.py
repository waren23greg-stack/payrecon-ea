"""Load adapter output into the staging database, one transaction per file.

A file is registered in source_files and everything it produced (payment rows,
load issues, quarantined rows, the statement's printed summary) is written in a
single transaction: either the whole file is loaded or nothing is. The same file
content is never loaded twice into one database; that check uses the SHA-256 of
the bytes, so renaming a file does not fool it.

source_row 0 in load_issues means "the file itself" (for example a summary
figure that could not be read), as opposed to a numbered data row.

rejected_rows.cells_json can contain raw text from the source file, so run
databases must be treated as sensitive.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from payrecon.adapters.base import AdaptedPayments
from payrecon.normalize import NormalizationError, parse_money
from payrecon.schema import PAYMENT_COLUMNS, column_names

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class AlreadyLoadedError(Exception):
    """A file with identical content was already loaded into this database."""

    def __init__(self, name: str, existing_name: str, file_id: int) -> None:
        super().__init__(
            f"{name} has the same content as {existing_name} (file {file_id}), which is already loaded"
        )
        self.existing_name = existing_name
        self.file_id = file_id


@dataclass(frozen=True)
class FileInfo:
    name: str
    sha256: str
    provider: str | None = None
    encoding: str | None = None
    delimiter: str | None = None
    period_text: str | None = None


@dataclass(frozen=True)
class LoadResult:
    file_id: int
    rows_staged: int
    rows_rejected: int
    issues_recorded: int


def file_sha256(source: bytes | str | Path) -> str:
    """SHA-256 of file bytes, or of a file on disk, as lower-case hex."""
    data = source if isinstance(source, bytes) else Path(source).read_bytes()
    return hashlib.sha256(data).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _summary_rows(
    summary: Mapping[str, tuple[str, str]], currency: str
) -> tuple[list[tuple[str, int, int]], list[tuple[str, str]]]:
    """Parse printed summary figures. Returns (rows, [(field, code)] for unreadable ones)."""
    rows: list[tuple[str, int, int]] = []
    problems: list[tuple[str, str]] = []
    for label, (raw_in, raw_out) in summary.items():
        try:
            paid_in = parse_money(raw_in, currency)
            paid_out = parse_money(raw_out, currency)
        except NormalizationError as exc:
            problems.append((f"summary:{label}", exc.code))
            continue
        if paid_in is None or paid_out is None:
            problems.append((f"summary:{label}", "missing_amount"))
            continue
        rows.append((label, abs(paid_in), abs(paid_out)))
    return rows, problems


def load_payments(
    conn: sqlite3.Connection,
    adapted: AdaptedPayments,
    info: FileInfo,
    summary: Mapping[str, tuple[str, str]] | None = None,
    *,
    currency: str = "KES",
) -> LoadResult:
    """Write one payments file and everything it produced, atomically.

    ``summary`` is the statement's printed summary as text, label -> (paid in,
    paid out). A row labelled TOTAL also fills the declared totals on the file.
    Raises AlreadyLoadedError for repeated content and ValueError for a malformed
    payment dict or hash; in every failure case nothing is written.
    """
    if not _SHA256.match(info.sha256):
        raise ValueError("sha256 must be 64 lower-case hex characters")
    columns = column_names(PAYMENT_COLUMNS)
    for payment in adapted.payments:
        if set(payment) != set(columns):
            raise ValueError("a payment does not match the PAYMENT_COLUMNS contract")

    existing = conn.execute(
        "SELECT file_id, name FROM source_files WHERE kind = 'payments' AND sha256 = ?",
        (info.sha256,),
    ).fetchone()
    if existing is not None:
        raise AlreadyLoadedError(info.name, existing["name"], existing["file_id"])

    summary_rows, problems = _summary_rows(summary or {}, currency)
    declared = {label.upper(): (i, o) for label, i, o in summary_rows}.get("TOTAL")
    provider = info.provider or (adapted.payments[0]["provider"] if adapted.payments else None)
    file_issues = [(0, field, code) for field, code in problems]
    row_issues = [(i.source_row, i.field, i.code) for i in adapted.issues]

    with conn:  # one transaction: commit on success, roll back on any error
        cursor = conn.execute(
            "INSERT INTO source_files (name, kind, provider, sha256, encoding, delimiter,"
            " rows_read, rows_staged, rows_rejected, period_text,"
            " declared_paid_in_minor, declared_paid_out_minor, loaded_at)"
            " VALUES (?, 'payments', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                info.name, provider, info.sha256, info.encoding, info.delimiter,
                len(adapted.payments) + len(adapted.rejected), len(adapted.payments),
                len(adapted.rejected), info.period_text,
                declared[0] if declared else None, declared[1] if declared else None,
                _utc_now(),
            ),
        )
        file_id = cursor.lastrowid
        marks = ", ".join("?" for _ in columns)
        conn.executemany(
            f"INSERT INTO payments (file_id, {', '.join(columns)}) VALUES (?, {marks})",
            [(file_id, *(p[c] for c in columns)) for p in adapted.payments],
        )
        conn.executemany(
            "INSERT INTO load_issues (file_id, source_row, field, code) VALUES (?, ?, ?, ?)",
            [(file_id, *issue) for issue in (*file_issues, *row_issues)],
        )
        conn.executemany(
            "INSERT INTO rejected_rows (file_id, source_row, reason, cells_json) VALUES (?, ?, ?, ?)",
            [(file_id, r.source_row, r.reason, json.dumps(r.cells)) for r in adapted.rejected],
        )
        conn.executemany(
            "INSERT INTO statement_summary (file_id, label, paid_in_minor, paid_out_minor)"
            " VALUES (?, ?, ?, ?)",
            [(file_id, *row) for row in summary_rows],
        )
    return LoadResult(file_id, len(adapted.payments), len(adapted.rejected), len(file_issues) + len(row_issues))
