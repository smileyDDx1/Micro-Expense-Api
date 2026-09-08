"""FastAPI app + all routes, delegating to services."""
import logging
from contextlib import asynccontextmanager
from datetime import date

from fastapi import Depends, FastAPI, File, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app import analytics
from app.db import SessionLocal, create_tables, get_db
from app.ingestion import ingest_receipts, load_seed, parse_csv
from app.schemas import (
    CategorySpend,
    IngestSummary,
    ItemSpend,
    MerchantSpend,
    MonthSpend,
    RawReceiptIn,
    SummaryResponse,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("finance-api")

MAX_UPLOAD_BYTES = 5 * 1024 * 1024


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_tables()
    db = SessionLocal()
    try:
        load_seed(db)
        log.info("seed data loaded")
    finally:
        db.close()
    yield


app = FastAPI(
    title="Personal Finance & Micro-Expense API",
    description="Turns messy item-level spending into structured, queryable data.",
    version="1.0.0",
    lifespan=lifespan,
)

# Shared by every /analytics route.
FromDate = Query(None, alias="from", description="Inclusive start date (YYYY-MM-DD)")
ToDate = Query(None, alias="to", description="Inclusive end date (YYYY-MM-DD)")


# ---------- consistent error envelopes ----------

@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={
            "error": "validation_error",
            "message": "Request body or query parameters failed validation.",
            "detail": [
                {"field": ".".join(str(p) for p in e["loc"][1:]) or "body",
                 "problem": e["msg"]}
                for e in exc.errors()
            ],
        },
    )


@app.exception_handler(ValueError)
async def value_error(request: Request, exc: ValueError):
    return JSONResponse(
        status_code=400,
        content={"error": "bad_request", "message": str(exc)},
    )


# ---------- health ----------

@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok"}


# ---------- ingestion ----------

@app.post("/ingest/json", response_model=IngestSummary, tags=["ingestion"])
def ingest_json(receipts: list[RawReceiptIn], db: Session = Depends(get_db)):
    """Batch-ingest raw receipts. Each row runs through the normalization engine."""
    return ingest_receipts(db, receipts)


@app.post("/ingest/csv", response_model=IngestSummary, tags=["ingestion"])
async def ingest_csv(file: UploadFile = File(...), db: Session = Depends(get_db)):
    """Batch-ingest from a CSV upload.

    Required columns: raw_string, amount, occurred_at.
    Optional: currency, source.
    """
    content = await file.read()
    if not content:
        raise ValueError("uploaded file is empty")
    if len(content) > MAX_UPLOAD_BYTES:
        raise ValueError(f"file exceeds {MAX_UPLOAD_BYTES // 1024 // 1024}MB limit")

    receipts, parse_errors = parse_csv(content)   # ValueError -> 400
    summary = ingest_receipts(db, receipts)
    summary.errors = parse_errors + summary.errors
    return summary


# ---------- analytics ----------

@app.get("/analytics/summary", response_model=SummaryResponse, tags=["analytics"])
def analytics_summary(date_from: date | None = FromDate, date_to: date | None = ToDate,
                      db: Session = Depends(get_db)):
    """Total spend, transaction count, and the split by category."""
    return analytics.summary(db, date_from, date_to)


@app.get("/analytics/by-merchant", response_model=list[MerchantSpend], tags=["analytics"])
def analytics_by_merchant(date_from: date | None = FromDate, date_to: date | None = ToDate,
                          limit: int = Query(50, ge=1, le=500),
                          db: Session = Depends(get_db)):
    return analytics.by_merchant(db, date_from, date_to, limit)


@app.get("/analytics/by-category", response_model=list[CategorySpend], tags=["analytics"])
def analytics_by_category(date_from: date | None = FromDate, date_to: date | None = ToDate,
                          db: Session = Depends(get_db)):
    return analytics.by_category(db, date_from, date_to)


@app.get("/analytics/by-month", response_model=list[MonthSpend], tags=["analytics"])
def analytics_by_month(date_from: date | None = FromDate, date_to: date | None = ToDate,
                       db: Session = Depends(get_db)):
    """Monthly spend trend, oldest first."""
    return analytics.by_month(db, date_from, date_to)


@app.get("/analytics/top-items", response_model=list[ItemSpend], tags=["analytics"])
def analytics_top_items(date_from: date | None = FromDate, date_to: date | None = ToDate,
                        limit: int = Query(20, ge=1, le=200),
                        db: Session = Depends(get_db)):
    """Highest-spend line items. Only priced items can be ranked."""
    return analytics.top_items(db, date_from, date_to, limit)
