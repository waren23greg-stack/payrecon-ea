"""Result types shared by every provider adapter.

An adapter turns parsed rows into canonical dicts and reports what it could
not trust in two separate ways, so nothing is silently dropped:

* ``issues``: the row was staged, but one field was blank or unusable. The
  loader writes these to a load_issues table.
* ``rejected``: the row could not be staged at all (a NOT NULL field is
  missing) and goes to quarantine. ``cells`` may hold raw text from the file,
  so quarantine output must be treated as sensitive.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Issue:
    source_row: int
    field: str
    code: str  # a NormalizationError code or an adapter code; never the value itself


@dataclass(frozen=True)
class Rejected:
    source_row: int
    reason: str
    cells: list[str]


@dataclass
class AdaptedPayments:
    payments: list[dict[str, object]] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    rejected: list[Rejected] = field(default_factory=list)
