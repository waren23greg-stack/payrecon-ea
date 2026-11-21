"""Build tiny synthetic statement PDFs for tests: raw PDF syntax, no extra library.

Words are placed at explicit x/y positions, mimicking the layout of a real
M-Pesa statement (header centred over its columns, text left aligned, amounts
right aligned). All names, numbers and amounts here are made up.
"""

from __future__ import annotations

PAGE_WIDTH, PAGE_HEIGHT, FONT_SIZE = 595, 848, 8
_CHAR_WIDTH = {**{str(d): 0.556 for d in range(10)}, ".": 0.278, ",": 0.278, "-": 0.333}


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def text_width(text: str) -> float:
    return sum(_CHAR_WIDTH.get(ch, 0.556) for ch in text) * FONT_SIZE


def build_pdf(pages: list[list[tuple[float, float, str]]]) -> bytes:
    """pages: per page, a list of (x, y_from_top, text). Returns PDF bytes."""
    n = len(pages)
    page_ids = [4 + 2 * i for i in range(n)]
    content_ids = [5 + 2 * i for i in range(n)]
    objs: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: f"<< /Type /Pages /Kids [{' '.join(f'{p} 0 R' for p in page_ids)}] /Count {n} >>".encode(),
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    }
    for i, items in enumerate(pages):
        ops = [
            f"BT /F1 {FONT_SIZE} Tf {x:.2f} {PAGE_HEIGHT - top:.2f} Td ({_escape(text)}) Tj ET"
            for x, top, text in items
        ]
        stream = "\n".join(ops).encode("latin-1")
        objs[content_ids[i]] = b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"
        objs[page_ids[i]] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_WIDTH} {PAGE_HEIGHT}] "
            f"/Contents {content_ids[i]} 0 R /Resources << /Font << /F1 3 0 R >> >> >>"
        ).encode()
    out = bytearray(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    for num in sorted(objs):
        offsets[num] = len(out)
        out += f"{num} 0 obj\n".encode() + objs[num] + b"\nendobj\n"
    xref = len(out)
    size = max(objs) + 1
    out += f"xref\n0 {size}\n".encode() + b"0000000000 65535 f \n"
    for num in range(1, size):
        out += f"{offsets[num]:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


HEADER = [
    (50.7, "Receipt"), (83, "No."), (110.8, "Completion"), (154, "Time"),
    (215.7, "Details"), (282.2, "Transaction"), (326, "Status"),
    (372.9, "Paid"), (393, "In"), (435.5, "Withdrawn"), (510.2, "Balance"),
]
PAID_RIGHT, WITHDRAWN_RIGHT, BALANCE_RIGHT = 418.0, 487.0, 557.0


def header_items(top: float, shift: float = 0.0) -> list[tuple[float, float, str]]:
    return [(x + shift, top, text) for x, text in HEADER]


def row_items(
    top: float,
    receipt: str,
    date: str,
    time: str,
    details: list[str],
    *,
    paid_in: str = "",
    withdrawn: str = "",
    balance: str = "0.00",
    status: str = "Completed",
    shift: float = 0.0,
) -> list[tuple[float, float, str]]:
    """One statement line: receipt line plus wrapped details lines 6pt apart."""
    items = [
        (38 + shift, top, receipt),
        (100 + shift, top, date),
        (143 + shift, top, time),
        (177.5 + shift, top, details[0]),
        (282.1 + shift, top, status),
    ]
    if paid_in:
        items.append((PAID_RIGHT + shift - text_width(paid_in), top, paid_in))
    if withdrawn:
        items.append((WITHDRAWN_RIGHT + shift - text_width(withdrawn), top, withdrawn))
    items.append((BALANCE_RIGHT + shift - text_width(balance), top, balance))
    for k, extra in enumerate(details[1:], start=1):
        items.append((177.5 + shift, top + 6 * k, extra))
    return items


def statement_pdf(shift: float = 0.0) -> bytes:
    """Two pages, five lines, one receipt on two lines, a wrapped three-line cell."""
    page1 = [
        (480, 20, "Page 1 of 2"),
        (250, 60, "M-PESA STATEMENT"),
        (50, 90, "Customer Name:"), (200, 90, "TEST CUSTOMER"),
        (50, 105, "Mobile Number:"), (200, 105, "0700000000"),
        (50, 120, "Statement Period:"), (200, 120, "01 Oct 2026 - 31 Oct 2026"),
        (250, 200, "SUMMARY"),
        (50, 215, "TRANSACTION TYPE"), (380, 215, "PAID IN"), (440, 215, "PAID OUT"),
        (50, 230, "SEND MONEY:"), (400, 230, "0.00"), (470, 230, "100.00"),
        (50, 245, "RECEIVED MONEY:"), (400, 245, "300.00"), (470, 245, "0.00"),
        (50, 260, "TOTAL:"), (400, 260, "300.00"), (470, 260, "100.00"),
        (250, 290, "DETAILED STATEMENT"),
        *header_items(320, shift),
        *row_items(345, "QWE1234ABC", "2026-10-30", "21:01:33",
                   ["Customer Transfer Fuliza", "to - 254700***000 Test Person"],
                   withdrawn="-30.00", balance="0.00", shift=shift),
        *row_items(367, "QWE1234ABC", "2026-10-30", "21:01:33",
                   ["OverDraft of Credit Party"],
                   paid_in="30.00", balance="30.00", shift=shift),
        *row_items(383, "ASD5555FGH", "2026-10-29", "09:15:00",
                   ["Merchant Pay to 123456", "TEST SHOP", "NAIROBI BRANCH"],
                   withdrawn="-70.00", balance="1,030.00", shift=shift),
        (50, 800, "Disclaimer: made-up data for tests."),
        (50, 820, "Statement Verification Code ZZZZ9999"),
    ]
    page2 = [
        (480, 20, "Page 2 of 2"),
        *header_items(60, shift),
        *row_items(85, "ZXC9876POI", "2026-10-29", "08:00:00",
                   ["Funds received from -", "254700***001 Another Person"],
                   paid_in="1,100.00", balance="1,100.00", shift=shift),
        *row_items(107, "MNB4444VCX", "2026-10-28", "07:30:10",
                   ["Pay Bill Charge"], withdrawn="-1.00", balance="1,099.00", shift=shift),
        (50, 800, "Disclaimer: made-up data for tests."),
    ]
    return build_pdf([page1, page2])
