"""CSV reading for payrecon-ea: encoding, delimiter and header detection.

Statement exports and Excel files are messy. This module turns raw bytes into
clean rows without guessing silently: everything it decides (encoding,
delimiter, which line is the header) is reported back in CsvTable, and rows it
cannot trust are returned in ``bad_rows`` instead of being dropped.

Standard library only. Cell values stay text; typing is normalize.py's job.
"""

from __future__ import annotations

import codecs
import csv
import io
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

_DELIMITERS = (",", ";", "\t", "|")
_EXCEL_SIGNATURES = (b"PK\x03\x04", b"\xd0\xcf\x11\xe0")  # .xlsx/.zip, legacy .xls
_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\ufeff"), None)
_FORMULA_WRAP = re.compile(r'^="(.*)"$')  # ="0712345678" keeps a leading zero


class CsvError(ValueError):
    """The file cannot be read as a table at all. ``code`` is stable."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Row:
    number: int  # 1-based record number after the header; blank rows count
    data: dict[str, str]


@dataclass(frozen=True)
class BadRow:
    number: int
    cells: list[str]
    reason: str


@dataclass(frozen=True)
class CsvTable:
    header: list[str]  # normalized, unique column names
    rows: list[Row]
    bad_rows: list[BadRow]  # rows with the wrong number of fields
    encoding: str
    delimiter: str
    header_row: int  # 1-based record number of the header line
    preamble: list[list[str]]  # lines above the header (titles, account info)
    blank_rows: int


# ---------------------------------------------------------------------------
# Cells and headers
# ---------------------------------------------------------------------------


def _clean_cell(value: str) -> str:
    """Trim, drop zero-width chars, NBSP -> space, unwrap ="..." and a leading
    apostrophe before digits (Excel text markers)."""
    s = value.translate(_ZERO_WIDTH).replace("\u00a0", " ").strip()
    wrapped = _FORMULA_WRAP.match(s)
    if wrapped:
        return wrapped.group(1).strip()
    if len(s) > 1 and s[0] == "'" and s[1].isdigit():
        return s[1:]
    return s


def normalize_header(name: str) -> str:
    """'Receipt No.' -> 'receipt_no'. Lower-case, runs of non-alphanumerics -> '_'."""
    return re.sub(r"[^a-z0-9]+", "_", _clean_cell(name).lower()).strip("_")


def _unique(names: Iterable[str]) -> list[str]:
    """Name blank headers 'unnamed_<n>' and suffix repeats: amount, amount_2."""
    seen: dict[str, int] = {}
    out: list[str] = []
    for position, name in enumerate(names, start=1):
        name = name or f"unnamed_{position}"
        seen[name] = seen.get(name, 0) + 1
        out.append(name if seen[name] == 1 else f"{name}_{seen[name]}")
    return out


# ---------------------------------------------------------------------------
# Encoding and delimiter
# ---------------------------------------------------------------------------


def decode_bytes(data: bytes) -> tuple[str, str]:
    """Decode to text. Returns (text, encoding name).

    BOMs first (UTF-8, and UTF-16 as written by Excel 'Unicode Text'), then
    strict UTF-8, then Windows-1252, then Latin-1 which cannot fail. The
    fallbacks are heuristics, so the encoding used is always reported.
    """
    if data.startswith(codecs.BOM_UTF8):
        return data.decode("utf-8-sig"), "utf-8-sig"
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return data.decode("utf-16"), "utf-16"
    for encoding in ("utf-8", "cp1252"):
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1"), "latin-1"


def sniff_delimiter(text: str, max_lines: int = 50) -> str:
    """Pick the delimiter that splits the most lines into the same width.

    Each candidate is scored by (lines sharing the most common field count,
    that field count); a width below 2 means it did not split anything.
    Title lines above the header barely matter because the table dominates.
    Falls back to a comma for single-column files.
    """
    lines = text.splitlines()[:max_lines]
    best, best_score = ",", (0, 0)
    for delimiter in _DELIMITERS:
        try:
            counts = [len(r) for r in csv.reader(lines, delimiter=delimiter) if r]
        except csv.Error:
            continue
        if not counts:
            continue
        width, support = max(Counter(counts).items(), key=lambda kv: (kv[1], kv[0]))
        if width >= 2 and (support, width) > best_score:
            best, best_score = delimiter, (support, width)
    return best


# ---------------------------------------------------------------------------
# Table parsing
# ---------------------------------------------------------------------------


def _find_header(
    records: list[list[str]], wanted: frozenset[str], max_scan: int, min_matches: int
) -> int | None:
    """Index of the header record, or None.

    With no hints, the first non-blank record. With hints, the record (within
    the first ``max_scan``) whose cells match the most hints, provided it
    matches at least ``min_matches``; the earliest wins a tie.
    """
    best_index, best_score = None, 0
    for index, cells in enumerate(records[:max_scan]):
        if not any(_clean_cell(c) for c in cells):
            continue
        if not wanted:
            return index
        score = sum(1 for c in cells if normalize_header(c) in wanted)
        if score > best_score:
            best_index, best_score = index, score
    return best_index if best_score >= min_matches else None


def parse_table(
    data: bytes,
    hints: Iterable[str] = (),
    *,
    delimiter: str | None = None,
    max_scan: int = 50,
    min_matches: int = 2,
) -> CsvTable:
    """Parse raw CSV bytes into a CsvTable.

    ``hints`` are column names the header is expected to contain (any spelling
    that normalize_header maps to the same thing). They locate the header below
    statement title lines. ``delimiter`` overrides sniffing.
    """
    if not data.strip():
        raise CsvError("empty_file", "The file is empty.")
    if data.startswith(_EXCEL_SIGNATURES):
        raise CsvError(
            "not_csv_excel",
            "This looks like an Excel file, not a CSV. Export it as CSV and upload again.",
        )
    text, encoding = decode_bytes(data)
    if "\x00" in text:
        raise CsvError("binary_file", "The file contains binary data and is not a CSV.")
    directive = re.match(r"sep=(.)\r?\n", text)  # Excel's delimiter declaration
    if directive:
        text = text[directive.end():]
        delimiter = delimiter or directive.group(1)
    delimiter = delimiter or sniff_delimiter(text)
    try:
        records = list(csv.reader(io.StringIO(text, newline=""), delimiter=delimiter))
    except csv.Error as exc:
        raise CsvError("malformed_csv", f"The file could not be parsed as CSV: {exc}") from exc

    wanted = frozenset(normalize_header(h) for h in hints)
    start = _find_header(records, wanted, max_scan, min_matches)
    if start is None:
        if wanted:
            raise CsvError(
                "header_not_found",
                f"No header row with at least {min_matches} expected columns "
                f"was found in the first {max_scan} lines.",
            )
        raise CsvError("empty_file", "The file has no data.")

    header = _unique(normalize_header(c) for c in records[start])
    preamble = [[_clean_cell(c) for c in r] for r in records[:start]]
    rows: list[Row] = []
    bad_rows: list[BadRow] = []
    blank_rows = 0
    for number, raw in enumerate(records[start + 1 :], start=1):
        cells = [_clean_cell(c) for c in raw]
        if not any(cells):
            blank_rows += 1
        elif len(cells) != len(header):
            bad_rows.append(BadRow(number, cells, "wrong_field_count"))
        else:
            rows.append(Row(number, dict(zip(header, cells))))
    return CsvTable(header, rows, bad_rows, encoding, delimiter, start + 1, preamble, blank_rows)


def read_table(path: str | Path, hints: Iterable[str] = (), **options: object) -> CsvTable:
    """parse_table on a file's bytes. Options are parse_table's keyword options."""
    return parse_table(Path(path).read_bytes(), hints, **options)  # type: ignore[arg-type]
