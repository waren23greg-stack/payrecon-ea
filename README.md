# payrecon-ea

[![CI](https://github.com/waren23greg-stack/payrecon-ea/actions/workflows/ci.yml/badge.svg)](https://github.com/waren23greg-stack/payrecon-ea/actions/workflows/ci.yml)

Reconciliation and data-quality checks for East African payment data: mobile-money
statements (M-Pesa first) matched against order exports, with every anomaly traceable
to a source row.

> **Status: work in progress.** The ingestion layer is built and tested. Loading,
> reconciliation, the SQL checks, reporting and the web UI are next (see the roadmap).

## Why it exists

Mobile-money data is messy in ways generic ETL examples ignore. This project is built
around what real statements contain:

- One receipt number can appear on several lines (a payment, its fee and an overdraft
  draw), so a receipt is **not** a unique key. Lines are keyed by
  `(provider, receipt_no, kind, direction)`.
- Overdraft draws (for example Fuliza) are credit, not customer money, and must never
  be counted as income.
- Phone numbers arrive masked (`254798***048`) and amounts in several number formats.
- Statements are often password-protected PDFs rather than CSV files.
- Failed or odd rows are reported, never silently dropped.

## What works today

| Area | Module | What it does |
|---|---|---|
| Contract | `schema.py` | Currencies as integer minor units, per-country UTC offsets, column contract |
| Cleaning | `normalize.py` | Money, phone, timestamp and reference parsing with coded errors |
| CSV input | `csv_utils.py` | Encoding, delimiter and header detection below statement title lines |
| PDF input | `pdf_table.py` | Rebuilds statement tables from word positions (optional `pdf` extra) |
| Adapter | `adapters/mpesa_statement.py` | Classifies lines into payment, charge, overdraft draw and so on |
| Storage | `db.py`, `sql/` | Generated staging DDL for SQLite and Postgres, one database per run |

## Quick start

```bash
git clone https://github.com/waren23greg-stack/payrecon-ea.git
cd payrecon-ea
python -m venv .venv
source .venv/bin/activate          # Git Bash on Windows: source .venv/Scripts/activate
python -m pip install -e ".[dev]"
python -m pytest
```

Python 3.11 or newer. The core uses only the standard library; reading PDFs needs the
`pdf` extra, which the `dev` extra includes.

## Privacy

- Only synthetic data is committed. Real statements belong in `data/private/`, which is
  git-ignored.
- Phone numbers are never stored raw: only a keyed hash and a masked hint.
- Names in free-text statement details are discarded, not stored.
- Run folders (`runs/`) can hold raw text from rejected rows and are git-ignored.

## Roadmap

- [x] Schema, normalizers, CSV and PDF readers, M-Pesa statement adapter, database layer
- [ ] Loader (one transaction per file, idempotent by file content)
- [ ] Orders adapter and synthetic dirty/clean datasets with ground truth
- [ ] Tiered reconciliation (reference, then phone hint, amount and time window)
- [ ] SQL data-quality checks, reports and CLI
- [ ] FastAPI backend and React front end
- [ ] Postgres in Docker Compose, deployment docs

## Limitations

- Only one real M-Pesa personal statement layout has been checked so far; merchant
  (Paybill and Till) exports and other providers still need real samples.
- The Postgres DDL is generated but not yet executed against a live server.

## Licence

MIT, see [LICENSE](LICENSE).
