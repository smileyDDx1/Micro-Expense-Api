"""Aggregation queries behind the /analytics/* endpoints.

All of these are plain SQL GROUP BYs over transactions, with an optional
[date_from, date_to] window applied the same way everywhere.
"""
from __future__ import annotations

from datetime import date, datetime, time, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Category, Merchant, Transaction, TransactionItem
from app.schemas import (
    CategorySpend,
    ItemSpend,
    MerchantSpend,
    MonthSpend,
    SummaryResponse,
)


def _window(stmt, date_from: date | None, date_to: date | None):
    """Inclusive on both ends; `to` covers the whole day."""
    if date_from:
        stmt = stmt.where(
            Transaction.occurred_at >= datetime.combine(date_from, time.min, timezone.utc)
        )
    if date_to:
        stmt = stmt.where(
            Transaction.occurred_at <= datetime.combine(date_to, time.max, timezone.utc)
        )
    return stmt


def summary(db: Session, date_from: date | None = None, date_to: date | None = None
            ) -> SummaryResponse:
    totals = db.execute(
        _window(
            select(func.coalesce(func.sum(Transaction.amount), 0), func.count(Transaction.id)),
            date_from, date_to,
        )
    ).one()

    return SummaryResponse(
        total_spend=Decimal(totals[0]),
        txn_count=totals[1],
        by_category=by_category(db, date_from, date_to),
    )


def by_category(db: Session, date_from: date | None = None, date_to: date | None = None
                ) -> list[CategorySpend]:
    stmt = (
        select(
            func.coalesce(Category.name, "Uncategorized"),
            func.sum(Transaction.amount),
            func.count(Transaction.id),
        )
        .select_from(Transaction)
        .outerjoin(Category, Transaction.category_id == Category.id)
        .group_by(Category.name)
        .order_by(func.sum(Transaction.amount).desc())
    )
    return [
        CategorySpend(category=name, total=Decimal(total), txn_count=count)
        for name, total, count in db.execute(_window(stmt, date_from, date_to)).all()
    ]


def by_merchant(db: Session, date_from: date | None = None, date_to: date | None = None,
                limit: int = 50) -> list[MerchantSpend]:
    stmt = (
        select(
            Merchant.canonical_name,
            func.coalesce(Category.name, "Uncategorized"),
            func.sum(Transaction.amount),
            func.count(Transaction.id),
        )
        .select_from(Transaction)
        .join(Merchant, Transaction.merchant_id == Merchant.id)
        .outerjoin(Category, Transaction.category_id == Category.id)
        .group_by(Merchant.canonical_name, Category.name)
        .order_by(func.sum(Transaction.amount).desc())
        .limit(limit)
    )
    return [
        MerchantSpend(merchant=m, category=c, total=Decimal(total), txn_count=count)
        for m, c, total, count in db.execute(_window(stmt, date_from, date_to)).all()
    ]


def by_month(db: Session, date_from: date | None = None, date_to: date | None = None
             ) -> list[MonthSpend]:
    month = func.to_char(Transaction.occurred_at, "YYYY-MM")
    stmt = (
        select(month, func.sum(Transaction.amount), func.count(Transaction.id))
        .select_from(Transaction)
        .group_by(month)
        .order_by(month)
    )
    return [
        MonthSpend(month=m, total=Decimal(total), txn_count=count)
        for m, total, count in db.execute(_window(stmt, date_from, date_to)).all()
    ]


def top_items(db: Session, date_from: date | None = None, date_to: date | None = None,
              limit: int = 20) -> list[ItemSpend]:
    """Only priced items can rank by spend, so unpriced ones are excluded."""
    stmt = (
        select(
            TransactionItem.name,
            Merchant.canonical_name,
            func.coalesce(Category.name, "Uncategorized"),
            func.sum(TransactionItem.amount),
            func.count(TransactionItem.id),
        )
        .select_from(TransactionItem)
        .join(Transaction, TransactionItem.transaction_id == Transaction.id)
        .join(Merchant, Transaction.merchant_id == Merchant.id)
        .outerjoin(Category, TransactionItem.category_id == Category.id)
        .where(TransactionItem.amount.isnot(None))
        .group_by(TransactionItem.name, Merchant.canonical_name, Category.name)
        .order_by(func.sum(TransactionItem.amount).desc())
        .limit(limit)
    )
    return [
        ItemSpend(item=name, merchant=m, category=c, total=Decimal(total), times_bought=count)
        for name, m, c, total, count in db.execute(_window(stmt, date_from, date_to)).all()
    ]
