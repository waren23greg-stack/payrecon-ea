"""Read a statement PDF (M-Pesa layout) into the Row objects csv_utils produces.

PDF text has no cells, only words at x/y positions, so this module rebuilds the
table from geometry. Column boundaries are derived from the header line on each
page, never hard-coded. The output uses the same normalized column names a CSV
export of the same statement would have (receipt_no, completion_time, details,
transaction_status, paid_in, withdrawn, balance), so one adapter serves both.

Needs the optional ``pdf`` extra (pdfplumber), imported lazily. Values stay text.
The owner's name, number and email in the statement header are never read.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from pathlib import Path

from payrecon.csv_utils import BadRow, CsvError, Row

_RECEIPT = re.compile(r"^[A-Z0-9]{10}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME = re.compile(r"^\d{2}:\d{2}:\d{2}$")
_NUMBER = re.compile(r"^-?[\d,]+\.\d{2}$")
_LINE_TOLERANCE = 2.0  # words whose tops differ by less than this share a line


class PdfSupportError(RuntimeError):
    """pdfplumber is not installed."""


@dataclass(frozen=True)
class PdfStatement:
    rows: list[Row]
    bad_rows: list[BadRow]  # lines the layout rules could not place
    pages: int
    period_text: str | None  # e.g. "01 Jan 2026 - 20 Nov 2026"
    summary: dict[str, tuple[str, str]]  # label -> (paid in, paid out), as printed
    skipped_pages: list[int]  # pages with no table header


@dataclass(frozen=True)
class _Geometry:
    receipt_max_x: float  # receipt ids start left of this
    status_min_x: float  # status text starts here
    number_min_x: float  # amounts and balances start here
    pitch_paid_withdrawn: float  # distance between paid-in and withdrawn columns
    pitch_withdrawn_balance: float


def _import_pdfplumber():
    try:
        import pdfplumber
    except ImportError as exc:
        raise PdfSupportError(
            'Reading PDFs needs the optional extra: pip install "payrecon-ea[pdf]"'
        ) from exc
    return pdfplumber


def _is_password_error(exc: BaseException) -> bool:
    """pdfplumber wraps pdfminer's PDFPasswordIncorrect, so look through the chain."""
    chain = [exc, exc.__cause__, exc.__context__, *exc.args]
    return any("password" in type(e).__name__.lower() for e in chain if e is not None)


def _lines(words: list[dict]) -> list[list[dict]]:
    """Group words into visual lines, each sorted left to right."""
    lines: list[tuple[float, list[dict]]] = []
    for word in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if lines and abs(word["top"] - lines[-1][0]) <= _LINE_TOLERANCE:
            lines[-1][1].append(word)
        else:
            lines.append((word["top"], [word]))
    return [sorted(ws, key=lambda w: w["x0"]) for _, ws in lines]


def _centre(left: float, right: float) -> float:
    return (left + right) / 2


def _geometry(words: list[dict]) -> tuple[_Geometry, float] | None:
    """Column boundaries from the header line, and the y where the header ends."""
    for anchor in (w for w in words if w["text"] == "Receipt"):
        line = sorted(
            (w for w in words if abs(w["top"] - anchor["top"]) <= _LINE_TOLERANCE),
            key=lambda w: w["x0"],
        )
        by_text = {w["text"]: w for w in line}
        needed = ("Completion", "Transaction", "Paid", "Withdrawn", "Balance")
        if not all(name in by_text for name in needed):
            continue
        paid, withdrawn, balance = by_text["Paid"], by_text["Withdrawn"], by_text["Balance"]
        paid_right = by_text["In"]["x1"] if "In" in by_text else paid["x1"]
        c_paid = _centre(paid["x0"], paid_right)
        c_withdrawn = _centre(withdrawn["x0"], withdrawn["x1"])
        c_balance = _centre(balance["x0"], balance["x1"])
        geometry = _Geometry(
            receipt_max_x=by_text["Completion"]["x0"] - 8,
            status_min_x=by_text["Transaction"]["x0"] - 3,
            number_min_x=paid["x0"] - 20,
            pitch_paid_withdrawn=c_withdrawn - c_paid,
            pitch_withdrawn_balance=c_balance - c_withdrawn,
        )
        return geometry, anchor["bottom"]
    return None


def _summary(words: list[dict]) -> dict[str, tuple[str, str]]:
    """The 'SUMMARY' block: label followed by paid-in and paid-out totals."""
    start = next((w["top"] for w in words if w["text"] == "SUMMARY"), None)
    end = next((w["top"] for w in words if w["text"] == "DETAILED"), None)
    if start is None or end is None:
        return {}
    found: dict[str, tuple[str, str]] = {}
    for line in _lines([w for w in words if start < w["top"] < end]):
        numbers = [w["text"] for w in line if _NUMBER.match(w["text"])]
        label = " ".join(w["text"] for w in line if not _NUMBER.match(w["text"]))
        if len(numbers) == 2 and label:
            found[label.rstrip(":").strip()] = (numbers[0], numbers[1])
    return found


def _period(words: list[dict]) -> str | None:
    for line in _lines(words):
        texts = [w["text"] for w in line]
        if "Period:" in texts:
            return " ".join(texts[texts.index("Period:") + 1 :]) or None
    return None


class _Record:
    """A statement line being assembled from its receipt line and continuations."""

    def __init__(self, number: int, receipt: str) -> None:
        self.number = number
        self.receipt = receipt
        self.date = ""
        self.time = ""
        self.status: list[str] = []
        self.details: list[str] = []
        self.numbers: list[tuple[float, str]] = []  # (right edge, text)
        self.stray: list[str] = []

    def add(self, word: dict, geometry: _Geometry) -> None:
        text = word["text"]
        if _DATE.match(text) and not self.date:
            self.date = text
        elif _TIME.match(text) and not self.time:
            self.time = text
        elif word["x0"] >= geometry.number_min_x:
            if _NUMBER.match(text):
                self.numbers.append((word["x1"], text))
            else:
                self.stray.append(text)
        elif word["x0"] >= geometry.status_min_x:
            self.status.append(text)
        else:
            self.details.append(text)

    def finish(self, geometry: _Geometry) -> Row | BadRow:
        cells = [self.receipt, f"{self.date} {self.time}".strip(), *self.details]
        if not (self.date and self.time):
            return BadRow(self.number, cells, "missing_timestamp")
        if len(self.numbers) != 2 or self.stray:
            return BadRow(self.number, cells, "unexpected_number_count")
        (amount_edge, amount), (balance_edge, balance) = sorted(self.numbers)
        withdrawn_edge = balance_edge - geometry.pitch_withdrawn_balance
        paid_edge = withdrawn_edge - geometry.pitch_paid_withdrawn
        is_withdrawn = abs(amount_edge - withdrawn_edge) < abs(amount_edge - paid_edge)
        return Row(
            self.number,
            {
                "receipt_no": self.receipt,
                "completion_time": f"{self.date} {self.time}",
                "details": " ".join(self.details),
                "transaction_status": " ".join(self.status),
                "paid_in": "" if is_withdrawn else amount,
                "withdrawn": amount if is_withdrawn else "",
                "balance": balance,
            },
        )


def read_pdf_statement(source: bytes | str | Path, *, password: str | None = None) -> PdfStatement:
    """Parse an M-Pesa style statement PDF. Raises CsvError for unusable files."""
    pdfplumber = _import_pdfplumber()
    data = source if isinstance(source, bytes) else Path(source).read_bytes()
    if not data.startswith(b"%PDF"):
        raise CsvError("not_pdf", "The file is not a PDF.")
    try:
        pdf = pdfplumber.open(io.BytesIO(data), password=password)
    except Exception as exc:  # pdfminer raises many unrelated types
        if _is_password_error(exc):
            code = "pdf_password_incorrect" if password else "pdf_password_required"
            raise CsvError(code, "The PDF is password protected.") from exc
        raise CsvError("pdf_unreadable", f"The PDF could not be opened: {exc}") from exc

    rows: list[Row] = []
    bad_rows: list[BadRow] = []
    skipped: list[int] = []
    summary: dict[str, tuple[str, str]] = {}
    period: str | None = None
    geometry: _Geometry | None = None
    record: _Record | None = None
    count = 0
    any_text = False

    def close(rec: _Record | None) -> None:
        if rec is not None and geometry is not None:
            done = rec.finish(geometry)
            (rows if isinstance(done, Row) else bad_rows).append(done)

    with pdf:
        pages = len(pdf.pages)
        for page_number, page in enumerate(pdf.pages, start=1):
            words = page.extract_words(x_tolerance=1.5)
            any_text = any_text or bool(words)
            summary = summary or _summary(words)
            period = period or _period(words)
            found = _geometry(words)
            if found is not None:
                geometry, header_bottom = found
            elif geometry is None:
                skipped.append(page_number)
                continue
            else:
                header_bottom = 0.0
            stop = min((w["top"] for w in words if w["text"].startswith("Disclaimer")), default=1e9)
            body = [w for w in words if header_bottom < w["top"] < stop]
            has_header = found is not None
            seen_receipt = False
            for line in _lines(body):
                first = line[0]
                if _RECEIPT.match(first["text"]) and first["x0"] < geometry.receipt_max_x:
                    close(record)
                    count += 1
                    record = _Record(count, first["text"])
                    line = line[1:]
                    seen_receipt = True
                if record is None or (not has_header and not seen_receipt):
                    # Never attach loose text to a good row: on a page without its own
                    # header it is probably page furniture, so report it instead.
                    count += 1
                    reason = "text_before_first_receipt" if record is None else "unattached_text"
                    bad_rows.append(BadRow(count, [w["text"] for w in line], reason))
                    continue
                for word in line:
                    record.add(word, geometry)
    close(record)

    if not any_text:
        raise CsvError("pdf_no_text", "The PDF has no text layer; it may be a scan.")
    if geometry is None:
        raise CsvError("header_not_found", "No statement table header was found in the PDF.")
    return PdfStatement(rows, bad_rows, pages, period, summary, skipped)
