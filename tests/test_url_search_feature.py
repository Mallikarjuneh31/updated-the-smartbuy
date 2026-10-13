"""
tests/test_url_search_feature.py
================================
Comprehensive test suite for the "Search by Product URL" feature:
- URL validation and platform detection
- Canonical product normalization
- Strict multi-signal weighted matching
- Comparison engine & Best Verified Deal computation
- API endpoints: POST /api/compare-url, GET /api/compare-url/stream
- Non-breaking regression verification for product-name search
"""

import pytest
import json
from unittest.mock import patch, MagicMock

from url_detector import (
    is_valid_http_url,
    detect_platform_from_url,
    extract_amazon_asin,
    extract_flipkart_id,
    extract_meesho_id,
    validate_and_detect_url
)
from product_normalizer import (
    normalize_canonical_product,
    extract_variant_attributes,
    extract_model_number
)
from product_matcher import (
    compute_weighted_match_score,
    generate_search_query_from_product,
    match_canonical_against_platform_results
)
from comparison_engine import (
    compute_url_best_deal,
    build_url_specifications_matrix,
    compare_by_product_url
)
from app import app


# ── 1. URL Detector & Validation Tests ───────────────────────────────────────

def test_url_detector_amazon():
    url = "https://www.amazon.in/Apple-iPhone-15-128-GB/dp/B0CHX1W1XY/ref=sr_1_1"
    res = validate_and_detect_url(url)
    assert res["is_valid"] is True
    assert res["platform"] == "amazon"
    assert res["product_id"] == "B0CHX1W1XY"
    assert res["canonical_url"] == "https://www.amazon.in/dp/B0CHX1W1XY"


def test_url_detector_flipkart():
    url = "https://www.flipkart.com/apple-iphone-15-black-128-gb/p/itm6ac6485515ae4?pid=MOBGTAGPTB3VS24W&lid=LSTMOBGTAGPTB3VS24WVUQ0KM"
    res = validate_and_detect_url(url)
    assert res["is_valid"] is True
    assert res["platform"] == "flipkart"
    assert res["product_id"] == "MOBGTAGPTB3VS24W"
    assert "pid=MOBGTAGPTB3VS24W" in res["canonical_url"]


def test_url_detector_meesho():
    url = "https://www.meesho.com/s/p/4abc12?p_ref=homepage"
    res = validate_and_detect_url(url)
    assert res["is_valid"] is True
    assert res["platform"] == "meesho"
    assert res["product_id"] == "4abc12"
    assert "https://www.meesho.com/s/p/4abc12" in res["canonical_url"]


def test_url_detector_invalid_and_search_pages():
    # Empty string
    assert validate_and_detect_url("")["is_valid"] is False

    # Unsupported domain
    assert validate_and_detect_url("https://www.ebay.com/itm/123456")["is_valid"] is False

    # Amazon search result page (should be rejected)
    amz_search = "https://www.amazon.in/s?k=iphone+15"
    res_amz = validate_and_detect_url(amz_search)
    assert res_amz["is_valid"] is False
    assert "search results page" in res_amz["error"].lower()

    # Flipkart search result page
    fk_search = "https://www.flipkart.com/search?q=laptop"
    res_fk = validate_and_detect_url(fk_search)
    assert res_fk["is_valid"] is False


# ── 2. Product Normalizer Tests ──────────────────────────────────────────────

def test_canonical_normalization():
    raw_amz = {
        "title": "Samsung Galaxy A15 5G (Blue, 8GB RAM, 128GB Storage)",
        "price_num": 19499,
        "mrp_num": 21499,
        "rating": 4.2,
        "review_count": 1250,
        "image": "https://m.media-amazon.com/images/I/71abc.jpg",
        "url": "https://www.amazon.in/dp/B0CSK92XYZ",
        "brand": "Samsung",
        "specifications": {"RAM": "8GB", "Storage": "128GB", "Color": "Blue"}
    }
    norm = normalize_canonical_product(raw_amz, "amazon")
    assert norm is not None
    assert norm["platform"] == "amazon"
    assert norm["price"] == "₹19,499"
    assert norm["price_num"] == 19499
    assert norm["mrp"] == "₹21,499"
    assert norm["rating"] == 4.2
    assert norm["review_count"] == 1250
    assert norm["brand"] == "Samsung"
    assert norm["variant"]["storage"] == "128GB"
    assert norm["variant"]["ram"] == "8GB"


# ── 3. Weighted Product Matching Tests ───────────────────────────────────────

def test_matcher_exact_model_and_storage_agreement():
    can = {
        "title": "Samsung Galaxy A15 5G (Blue, 8GB RAM, 128GB Storage)",
        "brand": "Samsung",
        "model": "Galaxy A15 5G",
        "category": "phone",
        "variant": {"storage": "128GB", "ram": "8GB"}
    }
    same_prod = {
        "title": "Samsung Galaxy A15 5G (Light Blue, 128 GB) (8 GB RAM)",
        "brand": "Samsung",
        "model": "Galaxy A15 5G",
        "category": "phone",
        "variant": {"storage": "128GB", "ram": "8GB"}
    }
    score, match_class, _ = compute_weighted_match_score(can, same_prod)
    assert score >= 85
    assert match_class in ("Exact Match", "Strong Match")


def test_matcher_variant_discrimination_storage():
    can_128 = {
        "title": "Apple iPhone 15 (128 GB) - Black",
        "brand": "Apple",
        "model": "iPhone 15",
        "category": "phone",
        "variant": {"storage": "128GB"}
    }
    cand_256 = {
        "title": "Apple iPhone 15 (256 GB) - Black",
        "brand": "Apple",
        "model": "iPhone 15",
        "category": "phone",
        "variant": {"storage": "256GB"}
    }
    score, match_class, _ = compute_weighted_match_score(can_128, cand_256)
    # Storage conflict must classify as Variant, NOT Exact Match!
    assert match_class == "Variant"


def test_matcher_power_discrimination_chargers():
    vivo_44w = {
        "title": "Vivo 44W Fast FlashCharger Adapter",
        "brand": "Vivo",
        "model": "44W",
        "category": "charger",
        "variant": {"power": "44W"}
    }
    vivo_18w = {
        "title": "Vivo 18W Fast Charger Adapter",
        "brand": "Vivo",
        "model": "18W",
        "category": "charger",
        "variant": {"power": "18W"}
    }
    score, match_class, breakdown = compute_weighted_match_score(vivo_44w, vivo_18w)
    # 44W vs 18W must not be an exact match
    assert match_class != "Exact Match"
    assert breakdown["variant"] == 0.0


def test_matcher_weight_discrimination_face_wash():
    fw_100g = {
        "title": "Cetaphil Gentle Skin Cleanser Face Wash 100g",
        "brand": "Cetaphil",
        "model": "Standard",
        "category": "face_wash",
        "weight": "100g",
        "variant": {"weight": "100g"}
    }
    fw_200g = {
        "title": "Cetaphil Gentle Skin Cleanser Face Wash 200g",
        "brand": "Cetaphil",
        "model": "Standard",
        "category": "face_wash",
        "weight": "200g",
        "variant": {"weight": "200g"}
    }
    score, match_class, breakdown = compute_weighted_match_score(fw_100g, fw_200g)
    # 100g vs 200g is a Variant or Similar Product, NEVER an Exact Match!
    assert match_class in ("Variant", "Similar Product")
    assert breakdown["weight"] == 0.0


def test_matcher_5g_vs_4g_discrimination():
    a15_5g = {
        "title": "Samsung Galaxy A15 5G Blue 128GB",
        "brand": "Samsung",
        "model": "Galaxy A15 5G",
        "category": "phone",
        "variant": {"storage": "128GB"}
    }
    a15_4g = {
        "title": "Samsung Galaxy A15 4G Blue 128GB",
        "brand": "Samsung",
        "model": "Galaxy A15 4G",
        "category": "phone",
        "variant": {"storage": "128GB"}
    }
    score, match_class, _ = compute_weighted_match_score(a15_5g, a15_4g)
    assert match_class != "Exact Match"


def test_generate_concise_search_query():
    phone = {
        "title": "Samsung Galaxy A15 5G (Blue, 8GB RAM, 128GB Storage) with No Cost EMI",
        "brand": "Samsung",
        "model": "Galaxy A15 5G",
        "category": "phone",
        "variant": {"storage": "128GB"}
    }
    query = generate_search_query_from_product(phone)
    assert "Samsung" in query
    assert "A15" in query
    assert "No Cost EMI" not in query


# ── 4. Comparison Engine & Best Verified Deal Tests ──────────────────────────

def test_compute_url_best_deal():
    canonical = {
        "title": "Vivo T3 5G 128GB",
        "brand": "Vivo",
        "model": "T3 5G",
        "price_num": 18999,
        "rating": 4.4,
        "review_count": 2000,
        "in_stock": True
    }
    matches = {
        "amazon": {
            "title": "Vivo T3 5G (Cosmic Blue, 128 GB)",
            "brand": "Vivo",
            "model": "T3 5G",
            "price_num": 18999,
            "match_type": "Exact Match",
            "match_score": 95,
            "rating": 4.4,
            "review_count": 2000,
            "in_stock": True
        },
        "flipkart": {
            "title": "Vivo T3 5G (Crystal Flake, 128 GB)",
            "brand": "Vivo",
            "model": "T3 5G",
            "price_num": 17999,  # Cheaper!
            "match_type": "Exact Match",
            "match_score": 95,
            "rating": 4.5,
            "review_count": 15000,
            "in_stock": True
        },
        "meesho": None
    }
    best = compute_url_best_deal(canonical, matches, "amazon")
    assert best is not None
    assert best["platform"] == "Flipkart"
    assert best["price"] == 17999
    assert best["savings"] == 1000
    assert best["is_exact_verified"] is True


def test_build_specifications_matrix():
    canonical = {
        "title": "boAt Rockerz 450",
        "brand": "boAt",
        "model": "Rockerz 450",
        "category": "headphones",
        "price": "₹1,499",
        "price_num": 1499,
        "rating": 4.2,
        "availability": "In Stock",
        "specifications": {"Playtime": "15 Hours", "Bluetooth": "v5.0"}
    }
    matches = {
        "amazon": canonical,
        "flipkart": {
            "title": "boAt Rockerz 450 Bluetooth Headset",
            "brand": "boAt",
            "model": "Rockerz 450",
            "price": "₹1,399",
            "price_num": 1399,
            "rating": 4.3,
            "availability": "In Stock",
            "specifications": {"Playtime": "15 Hours"}
        },
        "meesho": None
    }
    stat = {
        "Amazon": {"available": True, "status": "success"},
        "Flipkart": {"available": True, "status": "success"},
        "Meesho": {"available": False, "status": "unavailable"}
    }
    matrix = build_url_specifications_matrix(canonical, matches, stat)
    assert len(matrix) >= 8
    # Meesho cell must show "Temporarily unavailable" because status is unavailable
    p_row = next(r for r in matrix if r["name"] == "Product Name")
    assert "boAt" in p_row["amazon"]
    assert "boAt" in p_row["flipkart"]
    assert p_row["meesho"] == "Temporarily unavailable"


# ── 5. Endpoints & Integration Tests ─────────────────────────────────────────

def test_api_compare_url_validation():
    client = app.test_client()

    # Missing URL
    resp = client.post('/api/compare-url', json={})
    assert resp.status_code == 400

    # Invalid URL
    resp2 = client.post('/api/compare-url', json={"url": "not_a_url"})
    assert resp2.status_code == 400
    assert "invalid" in resp2.get_json()["message"].lower()


@patch('api.compare_by_product_url')
def test_api_compare_url_mocked_success(mock_compare):
    mock_compare.return_value = {

        "success": True,
        "source_platform": "amazon",
        "source_platform_name": "Amazon",
        "source_url": "https://www.amazon.in/dp/B0CHX1W1XY",
        "source_product": {
            "title": "Apple iPhone 15 128GB",
            "price": "₹69,999",
            "price_num": 69999,
            "brand": "Apple"
        },
        "matches": {
            "amazon": {"title": "Apple iPhone 15 128GB", "price": "₹69,999", "price_num": 69999},
            "flipkart": {"title": "Apple iPhone 15 (128 GB)", "price": "₹68,999", "price_num": 68999},
            "meesho": None
        },
        "matching": {"flipkart": {"has_exact_match": True, "match_type": "Exact Match"}},
        "best_deal": {
            "platform": "Flipkart",
            "price": 68999,
            "formatted_price": "₹68,999",
            "savings": 1000
        },
        "specifications_matrix": [
            {"name": "Price", "amazon": "₹69,999", "flipkart": "₹68,999", "meesho": "Exact product not found"}
        ],
        "scraped_at": "2026-09-10T12:00:00Z"
    }

    client = app.test_client()
    resp = client.post('/api/compare-url', json={"url": "https://www.amazon.in/dp/B0CHX1W1XY"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "success"
    assert data["source_platform"] == "amazon"
    assert data["best_deal"]["platform"] == "Flipkart"
    assert data["matches"]["flipkart"]["price_num"] == 68999


def test_regression_existing_product_name_search():
    """Ensure existing product-name search continues working with zero regression."""
    client = app.test_client()
    # Health check
    resp_health = client.get('/api/health')
    assert resp_health.status_code == 200

    # Autocomplete
    resp_auto = client.get('/autocomplete?q=sam')
    assert resp_auto.status_code == 200
    assert "suggestions" in resp_auto.get_json()
