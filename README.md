# Personal Finance & Micro-Expense API

A single-service API that turns messy, item-level spending from food-delivery and
transit apps (Swiggy, Zomato, EatSure, Uber, Rapido, BlaBlaCar…) into structured,
queryable data — then serves personal cash-flow analytics on top of it.

## The problem (the "why")

Generic bank statements collapse a whole order into one opaque line
(`SWIGGY*ORDER1234  ₹480`), hiding what you actually spent on. This API ingests
raw receipts, runs them through a normalization engine that recovers the
merchant, the individual items and a category, and stores them at line-item
granularity — so "how much did I spend on food delivery last month?" has a
precise answer.

## How it works

```
Batch upload (JSON / CSV)
        │
        ▼
 Normalization engine  ──► merchant match ──► alias hit?  ──► known merchant
        │                                     fuzzy hit?  ──► known + flagged
        │                                     no hit      ──► Unknown + flagged
        ▼
 parse items + categorize
        │
        ▼
 Postgres: Merchant / Transaction / TransactionItem
        │
        ▼
 /analytics/*  by category · by merchant · by month · top items
```

### The normalization engine (the centerpiece)

`app/normalization.py` does three things to every row:

1. **Merchant match** — case-insensitive substring lookup against the alias
   table. No hit? Fuzzy match (`rapidfuzz`) above `FUZZY_THRESHOLD`. Still
   nothing? `Unknown`, flagged for review.
2. **Item parse** — when a descriptor bundles merchant + item
   (`SWIGGY*Dominos Farmhouse Pizza`), the item text is split off into
   `TransactionItem` rows.
3. **Categorize** — the merchant's default category, overridden per item by
   keyword rules (a `Paracetamol` line inside a grocery order becomes Health).

It returns a structured object: merchant, category, items, **confidence**, and
`needs_review`.

Three decisions worth knowing:

- **Earliest match wins, then longest.** `SWIGGY*Dominos Farmhouse Pizza`
  contains both `SWIGGY` and `DOMINOS`; the platform prefix comes first, so
  Swiggy wins. At the same position the longer pattern wins, so
  `SWIGGY INSTAMART` (Groceries) beats `SWIGGY` (Food).
- **`*` and `-` separate merchant from item, not item from item.** Splitting on
  `-` would turn the route `TRIP HSR-KORAMANGALA` into two bogus items. Only
  `,`, `+` and `and` split multiple items.
- **Money is never invented.** A single parsed item inherits the receipt total.
  A multi-item receipt with no per-item prices leaves item amounts `NULL` rather
  than dividing evenly — so `/analytics/top-items` reports only real numbers.

The engine is pure: it works off an `AliasIndex` loadable from CSV *or* the
database, so `tests/test_normalization.py` runs with no database at all.

## Tech stack

Python · FastAPI · SQLAlchemy · PostgreSQL · psycopg2 · Pydantic · Docker · uvicorn

## File layout

```
finance-api/
├── app/
│   ├── main.py            # FastAPI app + all routes, delegating to services
│   ├── db.py              # engine, SessionLocal, Base, get_db dependency
│   ├── models.py          # Merchant, MerchantAlias, Category, Transaction, TransactionItem
│   ├── schemas.py         # Pydantic request/response models
│   ├── normalization.py   # the engine: alias lookup + regex + fuzzy + categorize
│   ├── ingestion.py       # parse JSON/CSV batches -> normalize -> persist
│   └── analytics.py       # aggregation queries
├── seed/
│   ├── categories.csv
│   └── merchant_aliases.csv
├── tests/
│   └── test_normalization.py
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .env
└── README.md
```

## Setup

Prerequisites: Docker + Docker Compose.

```bash
docker compose up --build
```

Both containers start together, tables are created and `seed/` is upserted on
startup. Interactive docs: <http://localhost:8000/docs>

The Postgres volume `pgdata` persists across restarts. The DB is published on
host port **5434** (5432 and 5433 are used by other projects on this machine);
the API talks to it as `db:5432` inside the compose network.

Without Docker: point `DATABASE_URL` at `localhost:5434`, then
`pip install -r requirements.txt` and `uvicorn app.main:app --reload`.

Tests: `pytest tests/ -v` (no database required).

## Build order

- [x] 1. **Infrastructure** — Dockerfile + docker-compose (api + postgres, healthcheck, named volume); `GET /health`.
- [x] 2. **Database modeling** — SQLAlchemy models + Pydantic schemas; tables created on startup.
- [x] 3. **Normalization engine** — alias lookup + fuzzy match + item parse + categorize, seeded from `seed/`, with unit tests. *(centerpiece)*
- [x] 4. **Ingestion endpoints** — `POST /ingest/json` and `POST /ingest/csv`, returning a summary.
- [x] 5. **Analytics endpoints** — spend by category / merchant / month + top items. *(the payoff)*
- [x] 6. **Refinement** — consistent JSON errors, input validation, DB constraints.

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness check |
| POST | `/ingest/json` | Batch-ingest raw receipts (JSON body) |
| POST | `/ingest/csv` | Batch-ingest raw receipts (CSV upload) |
| GET | `/analytics/summary` | Total spend, txn count, spend by category |
| GET | `/analytics/by-merchant` | Spend grouped by merchant |
| GET | `/analytics/by-category` | Spend grouped by category |
| GET | `/analytics/by-month` | Monthly spend trend |
| GET | `/analytics/top-items` | Highest-spend line items |

All `/analytics/*` endpoints accept optional `from` / `to` date filters
(`YYYY-MM-DD`, inclusive on both ends).

CSV upload requires columns `raw_string, amount, occurred_at`; `currency` and
`source` are optional.

## Demo / acceptance test

```bash
curl -X POST localhost:8000/ingest/json -H "content-type: application/json" -d '[
  {"raw_string":"SWIGGY*Dominos Farmhouse Pizza","amount":480,"occurred_at":"2026-01-04","source":"swiggy"},
  {"raw_string":"UBER *TRIP HSR-KORAMANGALA","amount":190,"occurred_at":"2026-01-04","source":"uber"}
]'
# -> {"created":2,"matched_merchants":2,"unknown_merchants":0,"skipped_duplicates":0,"errors":[]}

curl "localhost:8000/analytics/summary?from=2026-01-01&to=2026-01-31"
# -> total 670, split Food 480 / Transit 190
```

## Validation & error handling

- `422` with a field-by-field `detail` list for schema violations (amount must be
  `> 0`, `source` must be a known value, dates must parse).
- `400` for a malformed CSV — not UTF-8, no header, or missing required columns.
- Row-level failures inside a batch are collected into `errors[]` and the rest of
  the batch still lands. Each row is written inside its own **SAVEPOINT**
  (`db.begin_nested()`), so a row that fails at the database rolls back only
  itself, and the error is attributed to the row that actually caused it.
  Without the savepoint, SQLAlchemy defers the INSERT: a bad row blows up during
  the *next* row's flush, `Session.rollback()` discards every uncommitted row in
  the batch, and the summary still reports them as created.
- **Ingestion is idempotent.** Every transaction carries a unique `fingerprint`
  (sha256 of `raw_string | amount | occurred_at | source`, or of `external_id`
  when the provider supplies one). Re-uploading the same batch returns
  `skipped_duplicates` instead of doubling your spend. Note the trade-off: two
  genuinely separate but identical purchases on the same day read as one
  duplicate — pass an `external_id` when that matters.
- DB constraints: unique `merchant_aliases.raw_pattern`, unique category and
  merchant names, non-null FKs, `CHECK (amount > 0)` on transactions.

## Out of scope (v1, on purpose)

| Excluded | Why / when to add |
|---|---|
| Email-receipt parsing | Best real data source — add once the JSON/CSV path is solid. |
| OCR of screenshots | Heavy; only for photo receipts. |
| Multi-user auth | It's "personal"; single-user keeps v1 lean. |
| ML categorization | Rules + alias table are enough; ML is over-engineering here. |

## Roadmap

1. Email-receipt ingestion (Swiggy/Zomato/Uber send itemized emails).
2. A review queue for `Unknown` merchants with one-click alias creation.
3. Budgets + per-category threshold alerts.
4. Auth + multi-user.
5. A small dashboard UI over the analytics endpoints.
