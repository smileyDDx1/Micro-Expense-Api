"""Pydantic request/response models."""
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ALLOWED_SOURCES = {
    "swiggy", "zomato", "eatsure", "uber", "rapido",
    "blablacar", "bank", "upi", "card", "other",
}


# ---------- ingestion input ----------

class RawItemIn(BaseModel):
    """Optional pre-split item; the engine parses items when this is absent."""

    name: str = Field(min_length=1, max_length=256)
    quantity: int = Field(default=1, ge=1)
    unit_price: Decimal | None = Field(default=None, ge=0)
    amount: Decimal | None = Field(default=None, ge=0)


class RawReceiptIn(BaseModel):
    raw_string: str = Field(min_length=1, max_length=512)
    amount: Decimal = Field(gt=0, description="Total for the receipt; must be positive")
    currency: str = Field(default="INR", min_length=3, max_length=3)
    occurred_at: datetime
    source: str = Field(default="other")
    external_id: str | None = Field(
        default=None, max_length=128,
        description="Provider's own id. When given, it alone decides duplicate identity.",
    )
    items: list[RawItemIn] | None = None

    @field_validator("source")
    @classmethod
    def known_source(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in ALLOWED_SOURCES:
            raise ValueError(f"source must be one of {sorted(ALLOWED_SOURCES)}")
        return v

    @field_validator("currency")
    @classmethod
    def upper_currency(cls, v: str) -> str:
        return v.strip().upper()


# ---------- ingestion output ----------

class IngestError(BaseModel):
    row: int
    raw_string: str | None = None
    error: str


class IngestSummary(BaseModel):
    created: int
    matched_merchants: int
    unknown_merchants: int
    skipped_duplicates: int = 0
    errors: list[IngestError] = []


# ---------- analytics output ----------

class CategorySpend(BaseModel):
    category: str
    total: Decimal
    txn_count: int


class SummaryResponse(BaseModel):
    total_spend: Decimal
    txn_count: int
    currency: str = "INR"
    by_category: list[CategorySpend]


class MerchantSpend(BaseModel):
    merchant: str
    category: str | None = None
    total: Decimal
    txn_count: int


class MonthSpend(BaseModel):
    month: str  # YYYY-MM
    total: Decimal
    txn_count: int


class ItemSpend(BaseModel):
    item: str
    merchant: str
    category: str | None = None
    total: Decimal
    times_bought: int


# ---------- read models ----------

class TransactionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    raw_string: str
    amount: Decimal
    currency: str
    occurred_at: datetime
    source: str
    merchant: str
    category: str | None
    confidence: float | None
    needs_review: bool
    items: list[str] = []


class DateRange(BaseModel):
    from_: date | None = None
    to: date | None = None
