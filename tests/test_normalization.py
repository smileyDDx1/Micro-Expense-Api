"""Unit tests for the normalization engine (no database needed)."""
from decimal import Decimal

import pytest

from app.normalization import AliasIndex, categorize_item, normalize, parse_items


@pytest.fixture(scope="module")
def index():
    return AliasIndex.from_csv()


def test_clean_string_matches_merchant_and_category(index):
    """A plain descriptor with only order noise around the merchant."""
    txn = normalize("ZOMATO ORDER 8891", Decimal("320"), index)

    assert txn.merchant == "Zomato"
    assert txn.category == "Food"
    assert txn.confidence == 1.0
    assert txn.match_method == "alias"
    assert txn.needs_review is False
    assert txn.items == []          # "ORDER 8891" is noise, not an item


def test_bundled_string_splits_item_off(index):
    """The headline case: merchant and item share one descriptor."""
    txn = normalize("SWIGGY*Dominos Farmhouse Pizza", Decimal("480"), index)

    assert txn.merchant == "Swiggy"     # platform prefix wins over "DOMINOS"
    assert txn.category == "Food"
    assert len(txn.items) == 1
    assert txn.items[0].name == "Dominos Farmhouse Pizza"
    assert txn.items[0].category == "Food"
    assert txn.items[0].amount == Decimal("480")


def test_aliased_merchant_resolves_to_canonical_name(index):
    """FAASOS is an alias for EatSure, and BEHROUZ resolves to the same."""
    assert normalize("FAASOS BANGALORE", Decimal("250"), index).merchant == "EatSure"
    assert normalize("BEHROUZ BIRYANI", Decimal("400"), index).merchant == "EatSure"


def test_unknown_merchant_is_flagged_for_review(index):
    txn = normalize("QWXZ RANDOM SHOP", Decimal("99"), index)

    assert txn.merchant == "Unknown"
    assert txn.category == "Uncategorized"
    assert txn.needs_review is True
    assert txn.confidence == 0.0
    assert txn.match_method == "none"
    assert txn.items == []      # a merchant name must not become a line item


def test_fuzzy_match_survives_a_typo(index):
    txn = normalize("SWIGY*Paneer Roll", Decimal("210"), index)

    assert txn.merchant == "Swiggy"
    assert txn.match_method == "fuzzy"
    assert 0 < txn.confidence < 1.0
    assert txn.needs_review is True     # fuzzy hits are worth a human glance
    assert [i.name for i in txn.items] == ["Paneer Roll"]   # prefix stripped


def test_longer_alias_wins_at_the_same_position(index):
    """SWIGGY INSTAMART is groceries, not food."""
    txn = normalize("SWIGGY INSTAMART Milk 1L", Decimal("66"), index)

    assert txn.merchant == "Swiggy Instamart"
    assert txn.category == "Groceries"


def test_transit_descriptor_keeps_route_as_one_item(index):
    """A hyphenated route must not be split into two items."""
    txn = normalize("UBER *TRIP HSR-KORAMANGALA", Decimal("190"), index)

    assert txn.merchant == "Uber"
    assert txn.category == "Transit"
    assert len(txn.items) == 1
    assert "HSR-KORAMANGALA" in txn.items[0].name.upper()


def test_multiple_items_split_on_commas(index):
    txn = normalize("ZOMATO*Paneer Tikka, Butter Naan and Gulab Jamun", Decimal("560"), index)

    names = [i.name for i in txn.items]
    assert len(names) == 3
    assert "Paneer Tikka" in names
    # unpriced multi-item receipts must not have money invented per item
    assert all(i.amount is None for i in txn.items)


def test_item_keyword_overrides_merchant_default(index):
    """A pharmacy line inside a grocery order is Health, not Groceries."""
    assert categorize_item("Paracetamol Tablet", "Groceries") == "Health"
    assert categorize_item("Farmhouse Pizza", "Groceries") == "Food"
    assert categorize_item("Unrecognised Thing", "Groceries") == "Groceries"


def test_provided_items_are_used_verbatim(index):
    txn = normalize(
        "BIGBASKET ORDER 12",
        Decimal("500"),
        index,
        provided_items=[
            {"name": "Milk 1L", "quantity": 2, "unit_price": Decimal("33"),
             "amount": Decimal("66")},
            {"name": "Paracetamol Tablet", "quantity": 1, "amount": Decimal("434")},
        ],
    )

    assert txn.merchant == "BigBasket"
    assert [i.name for i in txn.items] == ["Milk 1L", "Paracetamol Tablet"]
    assert txn.items[0].category == "Groceries"
    # keyword rules only fire on the words they know; a bare brand name
    # ("Paracetamol" alone) correctly falls back to the merchant default.
    assert txn.items[1].category == "Health"


def test_noise_only_remainder_yields_no_items():
    assert parse_items("ORDER 123456 PVT LTD BANGALORE", "Food") == []
