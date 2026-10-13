"""
search_engine.py
================
Smart-Buy: Multiplatform Price Review Comparison System
======================================================
Core Search Engine Orchestrator:
- Parallel fetching across Amazon, Flipkart, and Meesho via ThreadPoolExecutor
- Per-platform status tracking & failure isolation
- Platform-isolated caching with ?fresh=1 bypass
- Processing pipeline: Relevance filtering, Canonical matching, Specification matrix, Best Deal
"""

import time
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional, Dict, Any, List, Tuple

# ── Scrapers ──────────────────────────────────────────────────────────────
from scrapers.amazon_scraper import AmazonScraper
from scrapers.flipkart_scraper import FlipkartScraper
from scrapers.meesho_scraper import MeeshoScraper
from scrapers.gec_scraper import GecScraper

# ── Processing Package ───────────────────────────────────────────────────
from search.normalizer import normalize_query, detect_query_type
from search.matching import (
    calculate_similarity,
    get_adaptive_threshold,
    deduplicate_products,
    is_relevant,
)
from search.ranking import rank_products, annotate_badges, generate_sort_views
from search.filters import extract_filters_from_results, apply_filters
from search.specs_extractor import extract_specs
from ai_compare import get_ai_comparison, get_best_deals
from cache import get_platform_cached_results, set_platform_cached_results

from scrapers.scraper_logger import log_scraper_event

logger = logging.getLogger("smartbuy.search_engine")
logging.basicConfig(level=logging.INFO)

_scrapers_instances = {
    "Amazon": AmazonScraper(),
    "Flipkart": FlipkartScraper(),
    "Meesho": MeeshoScraper(),
    "GEC": GecScraper(),
}


def fetch_all_products_parallel(query: str, bypass_fresh: bool = False) -> tuple[dict, dict]:
    """
    Fetch products from Amazon, Flipkart, and Meesho in parallel.
    Platform failures are completely isolated — if one fails, others continue.
    Uses platform-isolated caching when bypass_fresh is False.
    """
    logger.info(f"[SEARCH] Starting parallel multi-platform search for '{query}' (fresh={bypass_fresh})...")

    raw_results = {name: [] for name in _scrapers_instances}
    platform_status = {
        name: {
            "status": "unavailable",
            "available": False,
            "error": None,
            "count": 0,
            "duration": 0.0,
            "source": "live"
        } for name in _scrapers_instances
    }

    platforms_to_scrape = {}

    # 1. Check Platform-Isolated Cache
    for name in _scrapers_instances:
        if not bypass_fresh:
            cached_entry = get_platform_cached_results(name, query, bypass_fresh=False)
            if cached_entry and cached_entry.get("results"):
                items = cached_entry["results"]
                raw_results[name] = items
                platform_status[name]["available"] = True
                platform_status[name]["count"] = len(items)
                platform_status[name]["status"] = cached_entry.get("scrape_status", "success")
                platform_status[name]["source"] = "cache"
                logger.info(f"[{name.upper()}] Loaded {len(items)} items from platform cache")
                continue

        platforms_to_scrape[name] = _scrapers_instances[name]

    if not platforms_to_scrape:
        return raw_results, platform_status

    def _execute_scraper(name, scraper_obj, q):
        start = time.time()
        try:
            items, status, err_msg = scraper_obj.search_products(q)
            dur = round(time.time() - start, 2)
            stat_val = status.value if hasattr(status, "value") else str(status)
            return name, items, stat_val, err_msg, "live", dur
        except Exception as err:
            dur = round(time.time() - start, 2)
            logger.exception(f"[{name}] Scraper execution exception: {err}")
            return name, [], "scraper_error", str(err), "live", dur

    with ThreadPoolExecutor(max_workers=4) as executor:
        future_to_platform = {
            executor.submit(_execute_scraper, name, obj, query): name
            for name, obj in platforms_to_scrape.items()
        }

        for future in as_completed(future_to_platform):
            platform = future_to_platform[future]
            try:
                p_name, items, status_code, err_msg, src, dur = future.result(timeout=45)
                raw_results[p_name] = items or []
                platform_status[p_name]["count"] = len(items or [])
                platform_status[p_name]["available"] = bool(items)
                platform_status[p_name]["status"] = status_code
                platform_status[p_name]["error"] = err_msg
                platform_status[p_name]["duration"] = dur
                platform_status[p_name]["source"] = src

                # Store in isolated platform cache if items were found
                if items:
                    set_platform_cached_results(p_name, query, items, status_code)

                log_scraper_event(p_name, query, status_code, retry=0, products_count=len(items or []), duration=dur)
                logger.info(f"[{p_name.upper()}] source={src} status={status_code} products={len(items or [])} duration={dur}s")
            except TimeoutError:
                logger.error(f"[{platform.upper()}] Scraper timed out after 45s.")
                raw_results[platform] = []
                platform_status[platform]["status"] = "timeout"
                platform_status[platform]["available"] = False
                platform_status[platform]["error"] = f"{platform} request timed out"
                log_scraper_event(platform, query, "timeout", retry=0, products_count=0, error=f"{platform} request timed out")
            except Exception as e:
                logger.error(f"[{platform.upper()}] Scraper execution exception: {e}")
                raw_results[platform] = []
                platform_status[platform]["status"] = "scraper_error"
                platform_status[platform]["available"] = False
                platform_status[platform]["error"] = str(e)
                log_scraper_event(platform, query, "scraper_error", retry=0, products_count=0, error=str(e))

    return raw_results, platform_status


def fetch_all_products_with_fallbacks(query_chain: list[str], bypass_fresh: bool = False) -> tuple[dict, dict]:
    """
    Execute scraper search with automatic fallback loop:
    Try query_chain[0] broad query across Amazon, Flipkart, Meesho, GEC.
    """
    combined_raw = {"Amazon": [], "Flipkart": [], "Meesho": [], "GEC": []}
    combined_status = {
        "Amazon": {"available": False, "error": None, "count": 0, "status": "unavailable"},
        "Flipkart": {"available": False, "error": None, "count": 0, "status": "unavailable"},
        "Meesho": {"available": False, "error": None, "count": 0, "status": "unavailable"},
        "GEC": {"available": False, "error": None, "count": 0, "status": "unavailable"},
    }

    if not query_chain:
        query_chain = ['Products']

    for idx, q in enumerate(query_chain):
        logger.info(f"Search Stage {idx+1}/{len(query_chain)} using query: '{q}'")
        raw_res, status = fetch_all_products_parallel(q, bypass_fresh=bypass_fresh)

        total_items = sum(len(items) for items in raw_res.values())

        for platform, items in raw_res.items():
            if items:
                combined_raw[platform].extend(items)
                combined_status[platform]["available"] = True
                combined_status[platform]["count"] = len(combined_raw[platform])
                combined_status[platform]["status"] = status.get(platform, {}).get("status", "success")
            else:
                combined_status[platform]["status"] = status.get(platform, {}).get("status", "unavailable")
                combined_status[platform]["error"] = status.get(platform, {}).get("error")

        if total_items >= 3 or idx == len(query_chain) - 1:
            break

    return combined_raw, combined_status


def process_results(query: str, raw_results: dict,
                    filter_params: Optional[dict] = None,
                    platform_status: Optional[dict] = None) -> dict:
    """
    Full product comparison pipeline:
        raw items → similarity filter → spec extraction → deduplication → match scoring → ranking → badges
    """
    if platform_status is None:
        platform_status = {
            p: {"available": bool(items), "error": None, "count": len(items), "status": "success" if items else "no_products_found"}
            for p, items in raw_results.items()
        }

    from search.category_detector import detect_category
    from search.pipeline import run_comparison_pipeline

    detected_cat = detect_category(query=query)
    logger.info(f"[PIPELINE] Executing Single Source of Truth pipeline for query: '{query}' | Category: '{detected_cat}'")

    pipeline_data = run_comparison_pipeline(
        query=query,
        raw_platform_results=raw_results,
        platform_status=platform_status
    )

    platform_results = pipeline_data["validated_by_platform"]
    all_matched = pipeline_data["validated_products"]

    # Upsert validated products to database
    try:
        from database import upsert_normalized_product
        for it in all_matched:
            upsert_normalized_product(it, query=query)
    except Exception:
        pass

    # Record actual prices from Amazon, Flipkart, Meesho, and GEC with timestamps in MongoDB
    try:
        from services.price_prediction_service import record_search_prices_to_history
        record_search_prices_to_history(query, pipeline_data.get("best_match_per_platform") or platform_results)
    except Exception as e:
        logger.debug(f"[PricePrediction] Could not record search prices to history: {e}")

    # Apply Match Scoring & Ranking if filters provided
    if filter_params:
        from search.filters import score_and_rank_products
        for platform in platform_results:
            platform_results[platform] = score_and_rank_products(
                platform_results[platform], filter_params, raw_q=query
            )
        all_matched = score_and_rank_products(all_matched, filter_params, raw_q=query)
    else:
        from search.ranking import rank_products
        for platform in platform_results:
            platform_results[platform] = rank_products(platform_results[platform])
        all_matched = rank_products(all_matched)

    # Badges & Views
    annotate_badges(platform_results)
    from search.ranking import get_summary_badges
    summary_badges = get_summary_badges(all_matched)
    sort_views = generate_sort_views(all_matched)

    all_raw_for_filters = [item for items in raw_results.values() for item in items]
    dynamic_filters = extract_filters_from_results(all_raw_for_filters)

    top_verified_offers = pipeline_data["top_verified_offers"]
    specifications_matrix = pipeline_data["specifications_matrix"]
    quality_comparison_table = pipeline_data.get("quality_comparison_table", [])
    comparison_summary = pipeline_data.get("comparison_summary", {})
    best_deal = pipeline_data["best_deal"]
    overall_best = best_deal
    savings_info = pipeline_data["savings_info"]
    match_pairs = pipeline_data["match_pairs"]
    marketplace_status = pipeline_data.get("marketplace_status", {})

    best_match_per_platform = pipeline_data["best_match_per_platform"]
    best_per_platform = {
        "Amazon": best_match_per_platform.get("Amazon"),
        "Flipkart": best_match_per_platform.get("Flipkart"),
        "Meesho": best_match_per_platform.get("Meesho"),
        "GEC": best_match_per_platform.get("GEC"),
    }

    ai_summary = get_ai_comparison(query, platform_results)

    platforms_payload = {
        "amazon": {
            "status": platform_status.get("Amazon", {}).get("status", "success"),
            "products": platform_results.get("Amazon", [])
        },
        "flipkart": {
            "status": platform_status.get("Flipkart", {}).get("status", "success"),
            "products": platform_results.get("Flipkart", [])
        },
        "meesho": {
            "status": platform_status.get("Meesho", {}).get("status", "success"),
            "products": platform_results.get("Meesho", [])
        },
        "gec": {
            "status": platform_status.get("GEC", {}).get("status", "success"),
            "products": platform_results.get("GEC", [])
        }
    }

    amz_prod = best_match_per_platform.get("Amazon") or (platform_results.get("Amazon", [None])[0] if platform_results.get("Amazon") else None)
    fk_prod = best_match_per_platform.get("Flipkart") or (platform_results.get("Flipkart", [None])[0] if platform_results.get("Flipkart") else None)
    mee_prod = best_match_per_platform.get("Meesho") or (platform_results.get("Meesho", [None])[0] if platform_results.get("Meesho") else None)
    gec_prod = best_match_per_platform.get("GEC") or (platform_results.get("GEC", [None])[0] if platform_results.get("GEC") else None)

    comparison = {
        "query": query,
        "matched_products": [p for p in best_match_per_platform.values() if p],
        "specifications": specifications_matrix,
        "best_deal": best_deal,
        "amazon": amz_prod,
        "flipkart": fk_prod,
        "meesho": mee_prod,
        "gec": gec_prod,
        "Amazon": amz_prod,
        "Flipkart": fk_prod,
        "Meesho": mee_prod,
        "GEC": gec_prod,
        "specification_table": {
            "has_match": pipeline_data["has_cross_platform_match"],
            "rows": specifications_matrix,
            "columns": ["specification", "amazon", "flipkart", "meesho", "gec"]
        },
        "quality_comparison_table": quality_comparison_table,
        "comparison_summary": comparison_summary,
        "match_pairs": match_pairs,
        "products": {
            "amazon": platform_results.get("Amazon", []),
            "flipkart": platform_results.get("Flipkart", []),
            "meesho": platform_results.get("Meesho", []),
            "gec": platform_results.get("GEC", [])
        }
    }

    return {
        "query":                 query,
        "platforms":             platforms_payload,
        "comparison":            comparison,
        "platform_results":      platform_results,
        "platform_status":       platform_status,
        "all_results":           all_matched,
        "validated_products":    all_matched,
        "sort_views":            sort_views,
        "summary_badges":        summary_badges,
        "dynamic_filters":       dynamic_filters,
        "best_per_platform":     best_per_platform,
        "overall_best":          overall_best,
        "ai_summary":            ai_summary,
        "top_verified_offers":   top_verified_offers,
        "top_prices_data":       top_verified_offers,
        "top_prices":            top_verified_offers,
        "best_overall_deal":     overall_best,
        "savings_info":          savings_info,
        "comparison_data":       comparison,
        "specification_table":   comparison.get("specification_table", {}),
        "specifications_matrix": specifications_matrix,
        "quality_comparison_table": quality_comparison_table,
        "comparison_summary":    comparison_summary,
        "marketplace_status":    marketplace_status,
        "best_deal":             best_deal,
        "match_pairs":           match_pairs,
        "no_deal_message":       pipeline_data.get("no_deal_message"),
        "has_cross_platform_match": pipeline_data.get("has_cross_platform_match", False)
    }
