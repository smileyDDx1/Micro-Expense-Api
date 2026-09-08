"""The normalization engine: messy vendor string -> structured data.

Deliberately pure: everything works off an AliasIndex that can be built from a
CSV or from the database, so the engine is unit-testable with no DB at all.

    "SWIGGY*Dominos Farmhouse Pizza"
      -> merchant "Swiggy", category "Food",
         items [Farmhouse Pizza -> Food], confidence 1.0
"""
from __future__ import annotations

import csv
import os
import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from rapidfuzz import fuzz

SEED_DIR = Path(__file__).resolve().parent.parent / "seed"
FUZZY_THRESHOLD = int(os.getenv("FUZZY_THRESHOLD", "82"))
UNKNOWN_MERCHANT = "Unknown"
DEFAULT_CATEGORY = "Uncategorized"

# Junk that shows up around the useful text in payment descriptors.
NOISE_PATTERNS = [
    r"\bORDER\s*#?\d+\b", r"\bTXN\s*#?\w*\d+\w*\b", r"\bREF\s*#?\w*\d+\w*\b",
    r"\bINV\s*#?\d+\b", r"\bID\s*[:#]?\s*\d+\b", r"\b\d{3,}\b",
    r"\bPVT\b", r"\bLTD\b", r"\bLIMITED\b", r"\bPOS\b", r"\bUPI\b",
    r"\bPAYU\b", r"\bRAZORPAY\b", r"\bPAYTM\b", r"\bINDIA\b", r"\bIN\b",
    r"\b(?:BANGALORE|BENGALURU|MUMBAI|DELHI|GURGAON|NOIDA|PUNE|HYDERABAD|CHENNAI)\b",
    r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b",
    r"\bRS\.?\b", r"\bINR\b",
]
_NOISE_RE = re.compile("|".join(NOISE_PATTERNS), re.IGNORECASE)

# The remainder is split into several items only on these; "*" and "-" are
# merchant/item separators, not item/item ones ("TRIP HSR-KORAMANGALA" is one thing).
_ITEM_SPLIT_RE = re.compile(r"\s*(?:,|\+|\band\b|&)\s*", re.IGNORECASE)
_LEADING_SEP_RE = re.compile(r"^[\s*\-:|/#.]+")
_TRAILING_SEP_RE = re.compile(r"[\s*\-:|/#.]+$")

# Per-item category overrides, applied over the merchant's default.
ITEM_KEYWORDS: dict[str, tuple[str, ...]] = {
    "Food": (
        "pizza", "burger", "biryani", "coffee", "latte", "tea", "chai", "cake",
        "roll", "dosa", "idli", "thali", "sandwich", "momo", "noodle", "pasta",
        "paneer", "chicken", "meal", "combo", "fries", "shake", "dessert", "curry",
    ),
    "Groceries": (
        "milk", "atta", "rice", "dal", "oil", "eggs", "bread", "onion", "tomato",
        "potato", "detergent", "shampoo", "soap", "sugar", "salt", "curd", "butter",
    ),
    "Transit": ("trip", "ride", "cab", "auto", "fare", "metro", "bus", "train", "toll"),
    "Health": ("pharmacy", "medicine", "tablet", "syrup", "capsule", "vitamin"),
}


@dataclass(frozen=True)
class AliasEntry:
    pattern: str            # stored uppercase for matching
    merchant: str
    default_category: str | None


@dataclass
class NormalizedItem:
    name: str
    quantity: int = 1
    unit_price: Decimal | None = None
    amount: Decimal | None = None
    category: str | None = None


@dataclass
class NormalizedTxn:
    raw_string: str
    merchant: str
    category: str | None
    items: list[NormalizedItem] = field(default_factory=list)
    confidence: float = 0.0
    match_method: str = "none"      # alias | fuzzy | none
    needs_review: bool = False


class AliasIndex:
    """The merchant lookup table, loadable from CSV or DB."""

    def __init__(self, entries: list[AliasEntry]):
        self.entries = entries

    @classmethod
    def from_csv(cls, path: Path | str = SEED_DIR / "merchant_aliases.csv") -> "AliasIndex":
        entries = []
        with open(path, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                entries.append(
                    AliasEntry(
                        pattern=row["pattern"].strip().upper(),
                        merchant=row["canonical_merchant"].strip(),
                        default_category=(row.get("default_category") or "").strip() or None,
                    )
                )
        return cls(entries)

    @classmethod
    def from_db(cls, session) -> "AliasIndex":
        from app.models import MerchantAlias

        entries = []
        for alias in session.query(MerchantAlias).all():
            merchant = alias.merchant
            entries.append(
                AliasEntry(
                    pattern=alias.raw_pattern.upper(),
                    merchant=merchant.canonical_name,
                    default_category=(
                        merchant.default_category.name if merchant.default_category else None
                    ),
                )
            )
        return cls(entries)

    def match(self, raw: str) -> tuple[AliasEntry | None, float, str, int]:
        """Return (entry, confidence, method, end_index_of_match_in_raw)."""
        upper = raw.upper()

        # 1. Substring hit. Earliest position wins -- the platform prefix comes
        #    first in "SWIGGY*Dominos ...", so Swiggy beats Dominos. Ties go to
        #    the longer pattern, so "SWIGGY INSTAMART" beats "SWIGGY".
        hits = []
        for entry in self.entries:
            pos = upper.find(entry.pattern)
            if pos != -1:
                hits.append((pos, -len(entry.pattern), entry))
        if hits:
            hits.sort(key=lambda h: (h[0], h[1]))
            pos, neg_len, entry = hits[0]
            return entry, 1.0, "alias", pos + len(entry.pattern)

        # 2. Fuzzy, for typos like "SWIGY". Compared per-token so a short
        #    pattern can't partial-match its way into a long string.
        tokens = [t for t in re.split(r"[^A-Z0-9]+", upper) if len(t) > 2]
        best_entry, best_score, best_token = None, 0.0, None
        for entry in self.entries:
            score = fuzz.token_set_ratio(upper, entry.pattern)
            token_hit = None
            for token in tokens:
                token_score = fuzz.ratio(token, entry.pattern)
                if token_score > score:
                    score, token_hit = token_score, token
            if score > best_score:
                best_entry, best_score, best_token = entry, score, token_hit

        if best_entry and best_score >= FUZZY_THRESHOLD:
            # Cut after the token that actually matched, so "SWIGY*Paneer Roll"
            # yields the item "Paneer Roll" rather than the whole descriptor.
            cut = 0
            if best_token:
                pos = upper.find(best_token)
                if pos != -1:
                    cut = pos + len(best_token)
            return best_entry, round(best_score / 100, 3), "fuzzy", cut

        return None, 0.0, "none", 0


def _strip_noise(text: str) -> str:
    text = _NOISE_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text)
    text = _LEADING_SEP_RE.sub("", text)
    return _TRAILING_SEP_RE.sub("", text).strip()


def _looks_meaningful(fragment: str) -> bool:
    """Keep fragments with real words, drop leftovers like '1234' or 'X'."""
    letters = re.sub(r"[^A-Za-z]", "", fragment)
    return len(letters) >= 3


def categorize_item(name: str, default: str | None) -> str | None:
    lowered = name.lower()
    for category, keywords in ITEM_KEYWORDS.items():
        if any(kw in lowered for kw in keywords):
            return category
    return default


def parse_items(remainder: str, default_category: str | None) -> list[NormalizedItem]:
    """Split the non-merchant part of a descriptor into line items."""
    cleaned = _strip_noise(remainder)
    if not cleaned or not _looks_meaningful(cleaned):
        return []

    items = []
    for fragment in _ITEM_SPLIT_RE.split(cleaned):
        fragment = _strip_noise(fragment)
        if not fragment or not _looks_meaningful(fragment):
            continue
        name = fragment.title() if fragment.isupper() or fragment.islower() else fragment
        items.append(NormalizedItem(name=name, category=categorize_item(name, default_category)))
    return items


def normalize(
    raw_string: str,
    amount: Decimal | float | None = None,
    index: AliasIndex | None = None,
    provided_items: list[dict] | None = None,
) -> NormalizedTxn:
    """Turn a raw descriptor into merchant + category + items + confidence."""
    index = index or AliasIndex.from_csv()
    raw = (raw_string or "").strip()

    entry, confidence, method, cut = index.match(raw)

    if entry is None:
        merchant, category = UNKNOWN_MERCHANT, DEFAULT_CATEGORY
        # Nothing was recognised, so the descriptor is a merchant name we can't
        # place -- inventing a line item out of it would just pollute the data.
        remainder = ""
        needs_review = True
    else:
        merchant = entry.merchant
        category = entry.default_category or DEFAULT_CATEGORY
        remainder = raw[cut:]
        needs_review = method == "fuzzy"

    if provided_items:
        items = [
            NormalizedItem(
                name=i["name"],
                quantity=i.get("quantity", 1) or 1,
                unit_price=i.get("unit_price"),
                amount=i.get("amount"),
                category=categorize_item(i["name"], category),
            )
            for i in provided_items
        ]
    else:
        items = parse_items(remainder, category)

    # Only attribute money to an item when it's unambiguous. Splitting an
    # unpriced multi-item receipt evenly would be inventing data.
    if amount is not None and len(items) == 1 and items[0].amount is None:
        items[0].amount = Decimal(str(amount))

    return NormalizedTxn(
        raw_string=raw,
        merchant=merchant,
        category=category,
        items=items,
        confidence=confidence,
        match_method=method,
        needs_review=needs_review,
    )
