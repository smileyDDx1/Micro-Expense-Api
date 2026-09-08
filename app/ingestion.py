"""Parse JSON/CSV batches -> normalize -> persist."""
from __future__ import annotations

import csv
import hashlib
import io
from decimal import Decimal, InvalidOperation

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.models import Category, Merchant, MerchantAlias, Transaction, TransactionItem
from app.normalization import (
    DEFAULT_CATEGORY,
    SEED_DIR,
    UNKNOWN_MERCHANT,
    AliasIndex,
    normalize,
)
from app.schemas import IngestError, IngestSummary, RawReceiptIn

CSV_COLUMNS = {"raw_string", "amount", "occurred_at"}


# ---------- seed loading ----------

def load_seed(db: Session) -> None:
    """Upsert categories and merchant aliases from seed/. Safe to re-run."""
    with open(SEED_DIR / "categories.csv", newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            get_or_create_category(db, row["name"].strip())
    db.flush()

    with open(SEED_DIR / "merchant_aliases.csv", newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            pattern = row["pattern"].strip().upper()
            category = get_or_create_category(db, (row.get("default_category") or "").strip()
                                              or DEFAULT_CATEGORY)
            merchant = get_or_create_merchant(db, row["canonical_merchant"].strip(), category)
            db.flush()
            exists = (
                db.query(MerchantAlias).filter(MerchantAlias.raw_pattern == pattern).first()
            )
            if not exists:
                db.add(MerchantAlias(raw_pattern=pattern, merchant_id=merchant.id))
    get_or_create_merchant(db, UNKNOWN_MERCHANT, get_or_create_category(db, DEFAULT_CATEGORY))
    db.commit()


def get_or_create_category(db: Session, name: str) -> Category:
    name = name or DEFAULT_CATEGORY
    obj = db.query(Category).filter(Category.name == name).first()
    if obj is None:
        obj = Category(name=name)
        db.add(obj)
        db.flush()
    return obj


def get_or_create_merchant(db: Session, name: str, category: Category | None) -> Merchant:
    obj = db.query(Merchant).filter(Merchant.canonical_name == name).first()
    if obj is None:
        obj = Merchant(
            canonical_name=name,
            default_category_id=category.id if category else None,
        )
        db.add(obj)
        db.flush()
    return obj


# ---------- ingestion ----------

def fingerprint(receipt: RawReceiptIn) -> str:
    """Stable identity for a receipt, so the same row can't land twice.

    An explicit external_id from the provider wins outright. Otherwise the
    content itself is the key -- note this means two genuinely separate but
    identical purchases on the same day look like one; send an external_id
    when that matters.
    """
    if receipt.external_id:
        basis = f"ext:{receipt.external_id}"
    else:
        basis = "|".join([
            receipt.raw_string.strip().upper(),
            f"{receipt.amount:.2f}",
            receipt.occurred_at.isoformat(),
            receipt.source,
        ])
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def ingest_receipts(db: Session, receipts: list[RawReceiptIn]) -> IngestSummary:
    """Run each receipt through the engine and persist the result.

    Each row is written inside its own SAVEPOINT, so a row that fails at the
    database rolls back only itself -- the rest of the batch still lands.
    """
    index = AliasIndex.from_db(db)
    created = matched = unknown = skipped = 0
    errors: list[IngestError] = []
    seen: set[str] = set()

    for i, receipt in enumerate(receipts):
        try:
            fp = fingerprint(receipt)
            already = fp in seen or db.query(Transaction.id).filter(
                Transaction.fingerprint == fp
            ).first() is not None
            if already:
                skipped += 1
                seen.add(fp)
                continue

            result = normalize(
                receipt.raw_string,
                receipt.amount,
                index,
                provided_items=[item.model_dump() for item in receipt.items]
                if receipt.items
                else None,
            )

            # SAVEPOINT: everything inside is flushed here, so a DB error is
            # raised against the row that actually caused it and undoes only it.
            with db.begin_nested():
                category = get_or_create_category(db, result.category or DEFAULT_CATEGORY)
                merchant = get_or_create_merchant(db, result.merchant, category)
                db.flush()

                txn = Transaction(
                    merchant_id=merchant.id,
                    fingerprint=fp,
                    raw_string=receipt.raw_string,
                    amount=receipt.amount,
                    currency=receipt.currency,
                    occurred_at=receipt.occurred_at,
                    source=receipt.source,
                    category_id=category.id,
                    confidence=result.confidence,
                    needs_review=result.needs_review,
                )
                db.add(txn)
                db.flush()

                for item in result.items:
                    item_category = get_or_create_category(db, item.category or DEFAULT_CATEGORY)
                    db.add(
                        TransactionItem(
                            transaction_id=txn.id,
                            name=item.name,
                            quantity=item.quantity,
                            unit_price=item.unit_price,
                            amount=item.amount,
                            category_id=item_category.id,
                        )
                    )

            # Counted only once the savepoint has actually committed.
            seen.add(fp)
            created += 1
            if result.merchant == UNKNOWN_MERCHANT:
                unknown += 1
            else:
                matched += 1

        except Exception as exc:
            errors.append(IngestError(row=i, raw_string=receipt.raw_string, error=str(exc)))

    db.commit()
    return IngestSummary(
        created=created,
        matched_merchants=matched,
        unknown_merchants=unknown,
        skipped_duplicates=skipped,
        errors=errors,
    )


def parse_csv(content: bytes) -> tuple[list[RawReceiptIn], list[IngestError]]:
    """Parse an uploaded CSV with the stdlib csv module.

    Row-level problems become IngestErrors; a structurally broken file raises
    ValueError, which the route turns into a 400.
    """
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(f"file is not valid UTF-8: {exc}") from exc

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("CSV has no header row")

    header = {(f or "").strip().lower() for f in reader.fieldnames}
    missing = CSV_COLUMNS - header
    if missing:
        raise ValueError(f"CSV missing required column(s): {sorted(missing)}")

    receipts: list[RawReceiptIn] = []
    errors: list[IngestError] = []

    for i, row in enumerate(reader):
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        try:
            raw_amount = row.get("amount", "")
            try:
                amount = Decimal(raw_amount)
            except InvalidOperation:
                # Decimal's own error is unreadable ("[<class ConversionSyntax>]").
                raise ValueError(f"amount {raw_amount!r} is not a valid number") from None

            receipts.append(
                RawReceiptIn(
                    raw_string=row["raw_string"],
                    amount=amount,
                    currency=row.get("currency") or "INR",
                    occurred_at=row["occurred_at"],
                    source=row.get("source") or "other",
                )
            )
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
            )
            errors.append(IngestError(row=i, raw_string=row.get("raw_string"), error=problems))
        except (KeyError, ValueError) as exc:
            errors.append(
                IngestError(row=i, raw_string=row.get("raw_string"), error=str(exc))
            )

    return receipts, errors
