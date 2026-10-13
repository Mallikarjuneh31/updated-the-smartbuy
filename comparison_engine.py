"""
comparison_engine.py
====================
SmartBuy URL-Based Product Comparison Engine.

Coordinates:
1. URL detection and validation
2. Source canonical product extraction with 4-tier fallback
3. Concurrent searching of remaining 2 platforms with error isolation
4. Multi-signal weighted product matching
5. Category-prioritized 3-column specification comparison matrix
6. Verified Best Deal computation
7. Step-by-step progress tracking for real-time frontend updates
"""

import time
import datetime
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, Any, Optional, Tuple, Generator, List

from url_detector import validate_and_detect_url
from product_normalizer import normalize_canonical_product
from product_matcher import (
    generate_search_query_from_product,
    compute_weighted_match_score,
    match_canonical_against_platform_results
)
from scrapers.amazon_scraper import AmazonScraper
from scrapers.flipkart_scraper import FlipkartScraper
from scrapers.meesho_scraper import MeeshoScraper
from scrapers.gec_scraper import GecScraper
from scrapers.scraper_result import ScrapeStatus
from search.matching_service import get_category_spec_keys, extract_normalized_specs

logger = logging.getLogger("smartbuy.comparison_engine")

_scrapers = {
    "amazon": AmazonScraper(),
    "flipkart": FlipkartScraper(),
    "meesho": MeeshoScraper(),
    "gec": GecScraper(),
}

# Cache for URL comparisons
_url_comparison_cache: Dict[str, Dict[str, Any]] = {}
URL_CACHE_TTL_SECONDS = 900  # 15 minutes


def get_cached_url_comparison(url: str, fresh: bool = False) -> Optional[Dict[str, Any]]:
    """Retrieve cached comparison for a URL if valid and not expired."""
    if fresh:
        return None
    clean_url = url.strip()
    entry = _url_comparison_cache.get(clean_url)
    if not entry:
        return None
    age = time.time() - entry.get("timestamp", 0)
    if age < URL_CACHE_TTL_SECONDS:
        logger.info(f"[URL_CACHE] Cache HIT for '{clean_url}' (age: {int(age)}s)")
        return entry.get("data")
    return None


def set_cached_url_comparison(url: str, data: Dict[str, Any]):
    """Store comparison data in cache with current timestamp."""
    clean_url = url.strip()
    _url_comparison_cache[clean_url] = {
        "timestamp": time.time(),
        "data": data
    }


def compute_url_best_deal(
    canonical: Dict[str, Any],
    matches: Dict[str, Optional[Dict[str, Any]]],
    source_platform: str
) -> Optional[Dict[str, Any]]:
    """
    Computes verified Best Deal considering only valid matching products
    across available platforms. Explains exact selection criteria.
    """
    candidates = []
    for plat_key, prod in matches.items():
        if not prod:
            continue
        if not prod.get('in_stock', True):
            continue
        price_num = prod.get('price_num') or 0
        if price_num <= 0:
            continue

        match_type = prod.get('match_type') or ('Exact Match' if plat_key == source_platform else 'Similar Product')
        match_score = prod.get('match_score') or (100.0 if plat_key == source_platform else 60.0)

        # Only allow Exact Match, Variant, or Strong Match to compete for Best Verified Deal
        if match_type not in ('Exact Match', 'Variant', 'Strong Match') and match_score < 70:
            continue

        candidates.append({
            "platform": plat_key.capitalize(),
            "platform_key": plat_key,
            "product": prod,
            "price": price_num,
            "match_type": match_type,
            "match_score": match_score,
            "rating": prod.get('rating') or 0.0,
            "reviews": prod.get('review_count') or 0
        })

    if not candidates:
        return None

    # Sort candidates: lowest price primary, rating secondary
    candidates.sort(key=lambda c: (c['price'], -c['rating'], -c['reviews']))
    winner = candidates[0]
    win_prod = winner['product']

    # Compute savings against highest price in the same valid matching group
    all_prices = [c['price'] for c in candidates]
    max_p = max(all_prices)
    savings = max(0, max_p - winner['price'])

    reasons = [
        f"Lowest verified price: ₹{winner['price']:,}",
        f"{winner['match_type']} verified on {winner['platform']}",
        f"{win_prod.get('availability', 'In Stock')}"
    ]
    if win_prod.get('rating'):
        reasons.append(f"Rated {win_prod['rating']} ★ ({win_prod.get('review_count', 0):,} reviews)")
    if savings > 0:
        reasons.append(f"Saves ₹{savings:,} compared to highest offer")

    return {
        "platform": winner['platform'],
        "platform_key": winner['platform_key'],
        "price": winner['price'],
        "formatted_price": f"₹{winner['price']:,}",
        "product": win_prod,
        "title": win_prod.get('title') or win_prod.get('product_name'),
        "product_name": win_prod.get('title') or win_prod.get('product_name'),
        "link": win_prod.get('product_url') or win_prod.get('url') or win_prod.get('link'),
        "image": win_prod.get('image_url') or win_prod.get('image'),
        "rating": win_prod.get('rating'),
        "reviews": win_prod.get('review_count', 0),
        "savings": savings,
        "match_type": winner['match_type'],
        "match_score": winner['match_score'],
        "reasons": reasons,
        "reason_text": " • ".join(reasons),
        "is_exact_verified": winner['match_type'] in ('Exact Match', 'Variant')
    }


def build_url_specifications_matrix(
    canonical: Dict[str, Any],
    matches: Dict[str, Optional[Dict[str, Any]]],
    platform_status: Dict[str, Dict[str, Any]],
    best_deal: Optional[Dict[str, Any]] = None
) -> List[Dict[str, Any]]:
    """
    Builds the 3-column dynamic specification comparison matrix (Specification | Amazon | Flipkart | Meesho).
    Displays ONLY real extracted values; hides rows where all marketplaces have no data;
    Missing values display '—'.
    """
    from search.specs_extractor import build_dynamic_specifications
    from search.category_detector import detect_category

    category = canonical.get('category') or detect_category(title=canonical.get('title', ''), query=canonical.get('title', '')) or 'default'
    matrix = build_dynamic_specifications(matches, category)
    if platform_status:
        for row in matrix:
            for p_key, p_stat in platform_status.items():
                p_lower = p_key.lower()
                if p_lower in row and (row[p_lower] in ('—', '-', '', None)):
                    if not p_stat.get('available', True) or p_stat.get('status') in ('unavailable', 'error', 'failed'):
                        row[p_lower] = "Temporarily unavailable"
    return matrix


def compare_by_product_url_stream(
    url: str,
    fresh: bool = False
) -> Generator[Dict[str, Any], None, Dict[str, Any]]:
    """
    Generator yielding step-by-step progress events, and finally returning the full comparison payload.
    Emits actual progress as each operation completes.
    """
    # ── Step 1: Detect URL ──────────────────────────────────────────────────
    validation = validate_and_detect_url(url)
    if not validation["is_valid"]:
        yield {
            "step": 1,
            "step_id": "url_detected",
            "name": "URL detected",
            "status": "error",
            "message": validation.get("error", "Invalid URL")
        }
        return {
            "success": False,
            "error": validation.get("error", "Invalid product URL")
        }

    source_platform = validation["platform"]
    source_platform_name = validation["platform_name"]
    canonical_url = validation["canonical_url"]

    yield {
        "step": 1,
        "step_id": "url_detected",
        "name": "URL detected",
        "status": "done",
        "message": f"Detected {source_platform_name} product URL"
    }

    # Check cache
    cached = get_cached_url_comparison(canonical_url, fresh=fresh)
    if cached:
        logger.info(f"[COMPARISON] Returning cached URL comparison for {canonical_url}")
        for s in [2, 3, 4, 5, 6, 7]:
            step_names = {
                2: "Source product extracted",
                3: "Amazon checked",
                4: "Flipkart checked",
                5: "Meesho checked",
                6: "Products matched",
                7: "Best deal calculated"
            }
            yield {
                "step": s,
                "step_id": f"step_{s}",
                "name": step_names[s],
                "status": "done",
                "message": "Loaded from cache"
            }
        return cached

    # ── Step 2: Extract Source Product ──────────────────────────────────────
    source_scraper = _scrapers[source_platform]
    source_product, src_status, src_err = source_scraper.scrape_product_url(canonical_url)

    if not source_product:
        yield {
            "step": 2,
            "step_id": "source_extracted",
            "name": "Source product extracted",
            "status": "error",
            "message": f"Unable to retrieve product from {source_platform_name}: {src_err or 'Scraper error'}"
        }
        return {
            "success": False,
            "error": f"Unable to extract product from {source_platform_name}. {src_err or ''}".strip()
        }

    source_product['is_source'] = True
    source_product['match_type'] = "EXACT MATCH"
    source_product['match_score'] = 100.0

    yield {
        "step": 2,
        "step_id": "source_extracted",
        "name": "Source product extracted",
        "status": "done",
        "message": f"Extracted '{source_product['title'][:40]}' ({source_product['price']})"
    }

    # ── Steps 3-5: Search other platforms concurrently ──────────────────────
    SUPPORTED_PLATFORMS = ["amazon", "flipkart", "meesho", "gec"]
    target_platforms = [p for p in SUPPORTED_PLATFORMS if p != source_platform]
    search_query = generate_search_query_from_product(source_product)
    logger.info(f"[URL_SEARCH] Searching targets {target_platforms} with generated query: '{search_query}'")

    raw_candidates: Dict[str, List[Dict[str, Any]]] = {p: [] for p in SUPPORTED_PLATFORMS}
    raw_candidates[source_platform] = [source_product]

    platform_status: Dict[str, Dict[str, Any]] = {
        (p.upper() if p == "gec" else p.capitalize()): {
            "status": "success" if p == source_platform else "pending",
            "available": True if p == source_platform else False,
            "error": None,
            "count": 1 if p == source_platform else 0
        }
        for p in SUPPORTED_PLATFORMS
    }

    def _scrape_target(plat):
        s_obj = _scrapers[plat]
        items, stat, err = s_obj.search_products(search_query)
        stat_val = stat.value if hasattr(stat, "value") else str(stat)
        return plat, items, stat_val, err

    with ThreadPoolExecutor(max_workers=3) as executor:
        future_map = {executor.submit(_scrape_target, p): p for p in target_platforms}
        for future in as_completed(future_map):
            plat = future_map[future]
            p_cap = plat.upper() if plat == "gec" else plat.capitalize()
            try:
                p_name, items, stat_val, err_msg = future.result(timeout=40)
                raw_candidates[p_name] = items or []
                platform_status[p_cap]["count"] = len(items or [])
                platform_status[p_cap]["available"] = bool(items)
                platform_status[p_cap]["status"] = stat_val
                platform_status[p_cap]["error"] = err_msg

                step_num = 3 if plat == "amazon" else (4 if plat == "flipkart" else (5 if plat == "meesho" else 6))
                yield {
                    "step": step_num,
                    "step_id": f"{plat}_checked",
                    "name": f"{p_cap} checked",
                    "status": "done" if items else ("warning" if stat_val in ('blocked', 'timeout', 'scraper_error') else "done"),
                    "message": f"{len(items or [])} offers found" if items else ("Temporarily unavailable" if stat_val in ('blocked', 'timeout', 'scraper_error') else "No matching offers")
                }
            except Exception as exc:
                logger.error(f"[URL_SEARCH] Error searching {plat}: {exc}")
                platform_status[p_cap]["status"] = "scraper_error"
                platform_status[p_cap]["available"] = False
                platform_status[p_cap]["error"] = str(exc)

                step_num = 3 if plat == "amazon" else (4 if plat == "flipkart" else (5 if plat == "meesho" else 6))
                yield {
                    "step": step_num,
                    "step_id": f"{plat}_checked",
                    "name": f"{p_cap} checked",
                    "status": "warning",
                    "message": f"Temporarily unavailable ({exc})"
                }

    # Also yield the source platform step if not yet yielded
    src_step_num = 3 if source_platform == "amazon" else (4 if source_platform == "flipkart" else (5 if source_platform == "meesho" else 6))
    yield {
        "step": src_step_num,
        "step_id": f"{source_platform}_checked",
        "name": f"{source_platform_name} checked",
        "status": "done",
        "message": "Source product active"
    }

    # ── Step 6: Product Matching via Unified Pipeline ───────────────────────
    from search.pipeline import run_comparison_pipeline

    raw_by_plat = {
        "Amazon": raw_candidates.get("amazon", []),
        "Flipkart": raw_candidates.get("flipkart", []),
        "Meesho": raw_candidates.get("meesho", []),
        "GEC": raw_candidates.get("gec", [])
    }

    pipeline_data = run_comparison_pipeline(
        query=search_query,
        raw_platform_results=raw_by_plat,
        platform_status=platform_status,
        source_product=source_product,
        is_url_search=True
    )

    best_match_per_platform = pipeline_data.get("best_match_per_platform", {})
    matched_results = {
        "amazon": best_match_per_platform.get("Amazon"),
        "flipkart": best_match_per_platform.get("Flipkart"),
        "meesho": best_match_per_platform.get("Meesho"),
        "gec": best_match_per_platform.get("GEC"),
    }
    # Ensure source platform match is anchored to source product
    matched_results[source_platform] = source_product

    similar_by_platform: Dict[str, List[Dict[str, Any]]] = {
        p: [item for item in raw_candidates.get(p, []) if item != matched_results.get(p)]
        for p in SUPPORTED_PLATFORMS
    }

    matching_breakdown: Dict[str, Any] = {}
    for plat in SUPPORTED_PLATFORMS:
        m_item = matched_results.get(plat)
        matching_breakdown[plat] = {
            "has_exact_match": bool(m_item and m_item.get("match_status") == "EXACT_MATCH"),
            "has_variant_match": bool(m_item and m_item.get("match_status") == "VARIANT_MATCH"),
            "match_score": m_item.get("match_score", 0) if m_item else 0,
            "match_type": m_item.get("match_status", "Exact product not found") if m_item else "Exact product not found",
            "similar_count": len(similar_by_platform.get(plat, []))
        }

    yield {
        "step": 6,
        "step_id": "products_matched",
        "name": "Products matched",
        "status": "done",
        "message": "Quality and identity matching complete"
    }

    # ── Step 7: Best Deal & Comparison Matrix ───────────────────────────────
    best_deal = pipeline_data.get("best_deal")
    spec_matrix = pipeline_data.get("specifications_matrix") or build_url_specifications_matrix(source_product, matched_results, platform_status, best_deal)
    top_verified_offers = pipeline_data.get("top_verified_offers", {})
    validated_products = pipeline_data.get("validated_products", [])

    yield {
        "step": 7,
        "step_id": "best_deal_calculated",
        "name": "Best deal calculated",
        "status": "done",
        "message": f"Best verified deal: {best_deal['platform']} @ {best_deal['formatted_price']}" if best_deal else "Comparison compiled"
    }

    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    final_payload = {
        "success": True,
        "source_platform": source_platform,
        "source_platform_name": source_platform_name,
        "source_url": canonical_url,
        "source_product": source_product,
        "search_query_used": search_query,
        "matches": matched_results,
        "similar_products": similar_by_platform,
        "matching": matching_breakdown,
        "platform_status": platform_status,
        "specifications_matrix": spec_matrix,
        "best_deal": best_deal,
        "overall_best": best_deal,
        "top_verified_offers": top_verified_offers,
        "top_prices_data": top_verified_offers,
        "top_prices": top_verified_offers,
        "validated_products": validated_products,
        "validated_by_platform": pipeline_data.get("validated_by_platform", {}),
        "match_pairs": pipeline_data.get("match_pairs", {}),
        "no_deal_message": pipeline_data.get("no_deal_message"),
        "has_cross_platform_match": pipeline_data.get("has_cross_platform_match", False),
        "savings_info": pipeline_data.get("savings_info"),
        "scraped_at": now_iso
    }

    set_cached_url_comparison(canonical_url, final_payload)
    return final_payload


def compare_by_product_url(url: str, fresh: bool = False) -> Dict[str, Any]:
    """Synchronous execution of the URL comparison pipeline."""
    gen = compare_by_product_url_stream(url, fresh=fresh)
    result = None
    try:
        while True:
            next(gen)
    except StopIteration as e:
        result = e.value

    return result or {"success": False, "error": "Unable to complete comparison"}
