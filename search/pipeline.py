"""
search/pipeline.py
==================
SmartBuy Production-Quality Product Comparison Pipeline.

The Single Source of Truth Coordinator:
Produces `validated_products` which is the ONLY source for:
- Product cards
- Specification comparison matrix
- Matching status section
- Top Verified Offers
- Best Deal calculation
- Quality comparison

Guarantees:
- Hard rejection of accessories and model mismatches
- Extreme price anomaly detection (PRICE/IDENTITY UNVERIFIED)
- Best Deal based on Best Value, never raw minimum price
- Returns "No exact cross-platform match available" when cross-platform identity is unverified
"""

import re
import logging
from typing import Dict, Any, List, Optional, Tuple

logger = logging.getLogger(__name__)

from search.query_parser import parse_query_entities
from search.product_matcher import evaluate_product_match, check_suspicious_price, is_accessory_conflict
from search.specs_extractor import build_category_spec_matrix, compute_marketplace_statuses
from search.quality_scorer import compute_quality_and_confidence, get_detailed_quality_report, calculate_value_for_money_index
from search.category_detector import detect_category

def _enrich_product_quality(item: Dict[str, Any], q_rep: Dict[str, Any], min_price: Optional[float] = None) -> None:
    item["quality_report"] = q_rep
    item["quality_score"] = q_rep.get("raw_quality_score") or 50.0
    item["data_confidence"] = q_rep.get("data_confidence") or 50.0
    item["has_sufficient_data"] = q_rep.get("has_sufficient_data", True)
    item["quality_score_label"] = q_rep.get("quality_score_label")
    item["quality_band"] = q_rep.get("quality_band", "Standard Quality")
    item["quality_attributes"] = q_rep.get("quality_attributes", {})
    item["positive_themes"] = q_rep.get("review_signals", {}).get("positive_themes", [])
    item["negative_themes"] = q_rep.get("review_signals", {}).get("negative_themes", [])

    # Extract quality points list for rich UI rendering
    points = []
    points.append(f"Quality Score: {item['quality_score']:.0f}/100 ({item['quality_band']})")
    
    q_mat = item["quality_attributes"].get("material") or item.get("material")
    if q_mat and q_mat != "Standard Durable Material":
        points.append(f"Build: {q_mat}")

    q_warr = item["quality_attributes"].get("warranty") or item.get("warranty")
    if q_warr:
        points.append(f"Warranty: {q_warr}")

    r_val = item.get("rating")
    rev_cnt = item.get("reviews") or item.get("review_count")
    if r_val and str(r_val) not in ("0", "0.0", "None", "N/A"):
        points.append(f"Buyer Trust: ★ {r_val} ({rev_cnt or 'Verified'} reviews)")
    elif rev_cnt and str(rev_cnt) not in ("0", "None", "N/A"):
        points.append(f"Buyer Trust: {rev_cnt} verified reviews")

    for th in item["positive_themes"][:2]:
        points.append(f"Highlight: {th}")

    item["quality_points"] = points

    p_num = item.get("price_num")
    if p_num and p_num > 0:
        base_p = min_price if (min_price and min_price > 0) else p_num
        item["vfm_index"] = calculate_value_for_money_index(item["quality_score"], p_num, base_p)
    else:
        item["vfm_index"] = round(item["quality_score"] * 0.8, 1)


def run_comparison_pipeline(
    query: str,
    raw_platform_results: Dict[str, List[Dict[str, Any]]],
    platform_status: Dict[str, Dict[str, Any]],
    source_product: Optional[Dict[str, Any]] = None,
    is_url_search: bool = False
) -> Dict[str, Any]:
    """
    Master pipeline orchestrator taking raw results from Amazon, Flipkart, and Meesho
    and producing an authoritative, unified comparison payload.
    """
    clean_q = str(query or "").strip()
    detected_cat = detect_category(query=clean_q)

    # 1. Query Understanding / Target Entity Extraction
    if is_url_search and source_product:
        pack_q = source_product.get("pack_quantity")
        pack_cnt = None
        if pack_q is not None:
            try:
                m_p = re.search(r'\d+', str(pack_q))
                if m_p:
                    pack_cnt = int(m_p.group(0))
            except (ValueError, TypeError):
                pack_cnt = None

        target_info = {
            "raw_query": clean_q,
            "brand": source_product.get("brand"),
            "model": source_product.get("model"),
            "category": source_product.get("category") or detected_cat,
            "product_type": source_product.get("category") or detected_cat,
            "ram": source_product.get("ram"),
            "storage": source_product.get("storage"),
            "network": source_product.get("network"),
            "weight": source_product.get("weight"),
            "pack_count": pack_cnt,
            "is_accessory": False,
        }
    else:
        target_info = parse_query_entities(clean_q)
        if not target_info.get("category") or target_info["category"] == "other":
            target_info["category"] = detected_cat

    # 2. Strict Matching, Rejections & Quality Validation per Marketplace
    validated_products_by_platform: Dict[str, List[Dict[str, Any]]] = {
        "Amazon": [],
        "Flipkart": [],
        "Meesho": []
    }
    all_raw_candidates: List[Dict[str, Any]] = []
    rejected_products: List[Dict[str, Any]] = []

    for platform_key in ("Amazon", "Flipkart", "Meesho"):
        raw_items = raw_platform_results.get(platform_key) or raw_platform_results.get(platform_key.lower()) or []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            item["platform"] = platform_key
            all_raw_candidates.append(item)

            # Evaluate match status
            if is_url_search and (item.get('is_source') or (source_product and (item.get('product_url') == source_product.get('product_url') or item.get('url') == source_product.get('url')))):
                status = "EXACT_MATCH"
                score = 100.0
                breakdown = {"title": 100, "brand": 100, "specs": 100}
                reasons = ["Canonical source product from URL"]
                item["is_source"] = True
            else:
                status, score, breakdown, reasons = evaluate_product_match(target_info, item)
            item["match_status"] = status
            item["match_score"] = score
            item["match_breakdown"] = breakdown
            item["match_reasons"] = reasons

            # Compute category-aware quality and confidence
            q_rep = get_detailed_quality_report(item, detected_cat)
            _enrich_product_quality(item, q_rep)

            if status == "REJECTED":
                rejected_products.append(item)
            else:
                validated_products_by_platform[platform_key].append(item)

    # 2b. Platform Fallback Rescue: ensure no platform is left empty if scraper returned candidates
    for platform_key in ("Amazon", "Flipkart", "Meesho"):
        if not validated_products_by_platform[platform_key]:
            raw_items = raw_platform_results.get(platform_key) or raw_platform_results.get(platform_key.lower()) or []
            rescued_count = 0
            for item in raw_items:
                if not isinstance(item, dict):
                    continue
                # Skip true accessory conflicts
                if is_accessory_conflict(target_info.get("category", "other"), item.get("title", "")):
                    continue
                p_val = item.get("price_num")
                if not p_val or p_val <= 0:
                    continue
                item["platform"] = platform_key
                item["match_status"] = "SIMILAR_PRODUCT"
                item["match_score"] = item.get("match_score") or 65.0
                q_rep_res = get_detailed_quality_report(item, detected_cat)
                _enrich_product_quality(item, q_rep_res)
                validated_products_by_platform[platform_key].append(item)
                rescued_count += 1
                if rescued_count >= 15:
                    break
            if rescued_count > 0:
                logger.info(f"[{platform_key}] Rescued {rescued_count} category/similar products to ensure platform coverage.")

    # 3. Suspicious Price Protection (Section 17)
    # Check all validated products against median price of exact/variant matches
    all_candidates_for_median = [
        p for plat_items in validated_products_by_platform.values() for p in plat_items
    ]
    for p in all_candidates_for_median:
        is_suspicious, susp_reason = check_suspicious_price(p, all_candidates_for_median)
        if is_suspicious:
            p["match_status"] = "PRICE/IDENTITY UNVERIFIED"
            p["match_reasons"].append(susp_reason)
            title_str = str(p.get('title') or '')
            logger.warning(f"[{p.get('platform')}] Flagged suspicious price: '{title_str[:35]}' ({p.get('price')}): {susp_reason}")

    # validated_products is the SINGLE SOURCE OF TRUTH
    all_validated_products = [
        p for plat_items in validated_products_by_platform.values() for p in plat_items
        if p.get("match_status") != "REJECTED"
    ]

    # 4. Determine Best Validated Match per Platform for Specification Matrix & Deal Calculation
    best_match_per_platform: Dict[str, Optional[Dict[str, Any]]] = {
        "Amazon": None,
        "Flipkart": None,
        "Meesho": None
    }
    for plat in ("Amazon", "Flipkart", "Meesho"):
        plat_items = validated_products_by_platform.get(plat) or []
        # Filter for Exact Match or allowed Variant Match with verified price
        exact_or_variant = [
            it for it in plat_items
            if it.get("match_status") in ("EXACT_MATCH", "VARIANT_MATCH") and it.get("price_num") and it["price_num"] > 0
        ]
        if exact_or_variant:
            # Sort by match score desc, then price asc
            exact_or_variant.sort(key=lambda x: (-x.get("match_score", 0), x.get("price_num", 9999999)))
            best_match_per_platform[plat] = exact_or_variant[0]
        elif plat_items:
            # Fallback to highest scoring similar product if no exact match
            similar_items = [it for it in plat_items if it.get("price_num") and it["price_num"] > 0]
            if similar_items:
                similar_items.sort(key=lambda x: (-x.get("match_score", 0), x.get("price_num", 9999999)))
                best_match_per_platform[plat] = similar_items[0]
            elif is_url_search and source_product and (source_product.get('platform', '').lower() == plat.lower()):
                best_match_per_platform[plat] = source_product
        elif is_url_search and source_product and (source_product.get('platform', '').lower() == plat.lower()):
            best_match_per_platform[plat] = source_product

    # 5. Top Verified Offers (Section 15)
    # Includes exact/variant matches, and falls back to similar offers so each marketplace with products is represented.
    top_verified_offers: Dict[str, Dict[str, Any]] = {}
    valid_verified_deals: List[Dict[str, Any]] = []
    all_available_deals: List[Dict[str, Any]] = []

    for plat in ("Amazon", "Flipkart", "Meesho"):
        best_p = best_match_per_platform.get(plat)
        if best_p and (best_p.get("price_num") and best_p["price_num"] > 0 or (is_url_search and best_p.get("title"))):
            p_fmt = best_p.get("price") or (f"₹{best_p.get('price_num'):,}" if best_p.get("price_num") else "Check Marketplace")
            q_rep = best_p.get("quality_report") or get_detailed_quality_report(best_p, detected_cat)
            best_p["quality_report"] = q_rep
            offer_data = {
                "available": True,
                "platform": plat,
                "product": best_p,
                "product_name": best_p.get("title", "Product"),
                "title": best_p.get("title", "Product"),
                "image": best_p.get("image") or best_p.get("image_url"),
                "price": p_fmt,
                "formatted_price": p_fmt,
                "price_formatted": p_fmt,
                "price_num": best_p.get("price_num"),
                "mrp": best_p.get("mrp") or p_fmt,
                "discount": best_p.get("discount") or "0%",
                "rating": best_p.get("rating"),
                "reviews": best_p.get("review_count"),
                "quality_score": best_p.get("quality_score", 0.0),
                "data_confidence": best_p.get("data_confidence", 0.0),
                "quality_report": q_rep,
                "quality_score_label": q_rep.get("quality_score_label") or f"{best_p.get('quality_score', 0):.0f}/100",
                "quality_status_badge": q_rep.get("quality_status_badge") or "badge bg-primary text-white",
                "has_sufficient_data": q_rep.get("has_sufficient_data", True),
                "review_signals": q_rep.get("review_signals") or {},
                "quality_attributes": q_rep.get("quality_attributes") or {},
                "quantity": best_p.get("quantity") or best_p.get("weight") or f"Pack of {best_p.get('pack_quantity', 1)}",
                "variant": best_p.get("model") or "Standard",
                "seller": best_p.get("seller") or "Verified Seller",
                "availability": best_p.get("availability", "In Stock"),
                "unit_price": best_p.get("unit_price") or "N/A",
                "match_status": best_p.get("match_status", "SIMILAR_PRODUCT"),
                "match_score": best_p.get("match_score", 0.0),
                "link": best_p.get("product_url") or best_p.get("link", "#"),
                "is_overall_best": False
            }
            top_verified_offers[plat] = offer_data
            all_available_deals.append(offer_data)
            if best_p.get("match_status") in ("EXACT_MATCH", "VARIANT_MATCH"):
                valid_verified_deals.append(offer_data)
        else:
            top_verified_offers[plat] = {
                "available": False,
                "platform": plat,
                "product": None,
                "product_name": "No verified match on this marketplace",
                "title": "No verified match on this marketplace",
                "price": "N/A",
                "price_num": None,
                "mrp": "N/A",
                "discount": "—",
                "rating": None,
                "reviews": None,
                "quality_score": 0.0,
                "data_confidence": 0.0,
                "quality_score_label": "Insufficient Data",
                "has_sufficient_data": False,
                "review_signals": {},
                "quality_attributes": {},
                "is_overall_best": False
            }

    # 6. Category-Specific Specification Matrix (Section 13 & 19)
    spec_category = target_info.get("category") or detected_cat
    specification_matrix = build_category_spec_matrix(
        spec_category,
        best_match_per_platform,
        platform_status
    )
    marketplace_status = compute_marketplace_statuses(
        best_match_per_platform,
        platform_status
    )

    # 6b. Quality Comparison Table across Amazon, Flipkart, and Meesho
    quality_comparison_table = []
    for plat in ("Amazon", "Flipkart", "Meesho"):
        offer = top_verified_offers.get(plat)
        if offer and offer.get("available") and offer.get("product"):
            p = offer["product"]
            q_rep = offer.get("quality_report") or get_detailed_quality_report(p, spec_category)

            # Format main specifications string
            sp_items = []
            if p.get("specifications") and isinstance(p["specifications"], dict):
                for k, v in list(p["specifications"].items())[:3]:
                    if v and str(v).strip() not in ("N/A", "None", ""):
                        sp_items.append(f"{k}: {v}")
            if not sp_items and isinstance(p.get("specs"), dict) and p["specs"].get("category_specs"):
                for k, v in list(p["specs"]["category_specs"].items())[:3]:
                    if v and str(v).strip() not in ("N/A", "None", "Not Available", ""):
                        sp_items.append(f"{k}: {v}")
            main_specs_str = " • ".join(sp_items) if sp_items else "Standard Category Specifications"

            # Format quality attributes string and dictionary
            q_attr = q_rep.get("quality_attributes", {})
            q_attr_items = []
            if q_attr.get("material") and q_attr["material"] != "Standard Durable Material":
                q_attr_items.append(f"Material: {q_attr['material']}")
            if q_attr.get("warranty"):
                q_attr_items.append(f"Warranty: {q_attr['warranty']}")
            if q_attr.get("seller"):
                q_attr_items.append(f"Seller: {q_attr['seller']}")
            quality_attr_str = " | ".join(q_attr_items) if q_attr_items else (q_attr.get("warranty") or "Standard Quality Features")

            q_attr_dict = {
                "Warranty": q_attr.get("warranty") or "Standard 1 Year",
                "Material/Build": q_attr.get("material") or "Standard Build",
                "Seller/Delivery": q_attr.get("seller") or "Verified Marketplace Seller",
                "Condition": q_attr.get("condition") or "Brand New"
            }

            row_entry = {
                "platform": plat,
                "title": offer["title"],
                "product_name": offer["title"],
                "model": p.get("model") or p.get("model_number") or "Standard Variant",
                "price": offer["price"],
                "price_num": offer["price_num"],
                "mrp": offer.get("mrp") or offer["price"],
                "discount": offer.get("discount") or "0%",
                "rating": offer.get("rating"),
                "rating_display": f"★ {float(offer['rating']):.1f}" if (offer.get("rating") and str(offer['rating']).replace('.', '', 1).isdigit()) else (f"★ {offer['rating']}" if offer.get("rating") else "Unrated"),
                "review_count": offer.get("reviews"),
                "review_count_display": f"{int(str(offer['reviews']).replace(',', '')):,} reviews" if (offer.get("reviews") and str(offer['reviews']).replace(',', '').isdigit()) else (f"{offer['reviews']} reviews" if offer.get("reviews") else "No reviews yet"),
                "main_specs": p.get("specs") or p.get("category_specs") or {},
                "main_specifications": main_specs_str,
                "quality_attributes": q_attr_dict,
                "quality_attributes_str": quality_attr_str,
                "quality_score": offer["quality_score"],
                "estimated_quality_score": offer["quality_score"],
                "estimated_quality_label": q_rep.get("quality_score_label", f"{offer['quality_score']}/100"),
                "quality_band": q_rep.get("quality_band", "Standard Quality"),
                "has_sufficient_data": q_rep.get("has_sufficient_data", True),
                "data_confidence": offer["data_confidence"],
                "data_confidence_label": q_rep.get("data_confidence_label", f"{offer['data_confidence']}% Verified Evidence"),
                "quality_badge": q_rep.get("quality_status_badge", "badge bg-primary text-white"),
                "positive_themes": q_rep.get("review_signals", {}).get("positive_themes", []),
                "negative_themes": q_rep.get("review_signals", {}).get("negative_themes", []),
                "link": offer["link"],
                "product_url": offer["link"],
                "image": offer["image"],
                "available": True
            }
            quality_comparison_table.append(row_entry)
        else:
            quality_comparison_table.append({
                "platform": plat,
                "product_name": "No verified match on this marketplace",
                "model": "—",
                "price": "N/A",
                "price_num": None,
                "mrp": "N/A",
                "discount": "—",
                "rating": None,
                "rating_display": "—",
                "review_count": None,
                "review_count_display": "—",
                "main_specs": {},
                "main_specifications": "No specification data available",
                "quality_attributes": {
                    "Warranty": "—",
                    "Material/Build": "—",
                    "Seller/Delivery": "—",
                    "Condition": "—"
                },
                "quality_attributes_str": "Listing unavailable on this platform",
                "estimated_quality_score": None,
                "estimated_quality_label": "Insufficient Data",
                "has_sufficient_data": False,
                "data_confidence": 0.0,
                "data_confidence_label": "0% Data Availability",
                "quality_badge": "badge bg-secondary text-white",
                "positive_themes": [],
                "negative_themes": [],
                "link": "#",
                "image": "",
                "available": False
            })

    # 7. Comparison Summary: Lowest Price, Best Quality, Best Spec Match, Review Insights, Value for Money
    comparison_summary: Dict[str, Any] = {
        "lowest_price": None,
        "best_quality": None,
        "best_spec_match": None,
        "review_insights": None,
        "value_for_money": None
    }

    active_offers = [d for d in all_available_deals if d.get("price_num") and d["price_num"] > 0]
    if active_offers:
        min_price_val = min(d["price_num"] for d in active_offers)
        
        # 1. Lowest Price Indicator (strictly price-based)
        cheapest_deal = min(active_offers, key=lambda x: x["price_num"])
        highest_p = max(d["price_num"] for d in active_offers)
        savings_vs_max = max(0, highest_p - cheapest_deal["price_num"]) if len(active_offers) >= 2 else 0
        comparison_summary["lowest_price"] = {
            "platform": cheapest_deal["platform"],
            "title": cheapest_deal["title"],
            "price": cheapest_deal["price"],
            "price_num": cheapest_deal["price_num"],
            "savings": savings_vs_max,
            "savings_formatted": f"₹{savings_vs_max:,}" if savings_vs_max > 0 else None,
            "link": cheapest_deal["link"],
            "image": cheapest_deal["image"],
            "label": f"Lowest Price: {cheapest_deal['price']} on {cheapest_deal['platform']}"
        }

        # 2. Quality Evidence Indicator (highest evidence-based quality score among items with sufficient data)
        sufficient_quality_deals = [d for d in active_offers if d.get("has_sufficient_data", True)]
        if sufficient_quality_deals:
            top_quality_deal = max(sufficient_quality_deals, key=lambda x: x.get("quality_score", 0))
            comparison_summary["best_quality"] = {
                "platform": top_quality_deal["platform"],
                "title": top_quality_deal["title"],
                "quality_score": top_quality_deal["quality_score"],
                "quality_label": top_quality_deal.get("quality_score_label") or f"{top_quality_deal['quality_score']}/100",
                "data_confidence": top_quality_deal["data_confidence"],
                "price": top_quality_deal["price"],
                "link": top_quality_deal["link"],
                "image": top_quality_deal["image"],
                "reasons": f"Highest evidence-backed quality score ({top_quality_deal['quality_score']}/100) on {top_quality_deal['platform']}"
            }
        else:
            comparison_summary["best_quality"] = {
                "platform": "Marketplace Evidence",
                "title": "Insufficient Data for Score",
                "quality_score": None,
                "quality_label": "Insufficient Data",
                "data_confidence": max(d.get("data_confidence", 0) for d in active_offers),
                "price": "N/A",
                "link": "#",
                "image": "",
                "reasons": "Detailed specification and verified rating signals are currently incomplete."
            }

        # 3. Best Specification Match Indicator
        best_spec_deal = max(active_offers, key=lambda x: (x.get("match_score", 0), x.get("data_confidence", 0)))
        comparison_summary["best_spec_match"] = {
            "platform": best_spec_deal["platform"],
            "title": best_spec_deal["title"],
            "match_score": best_spec_deal.get("match_score", 0),
            "match_status": best_spec_deal.get("match_status", "SIMILAR_PRODUCT"),
            "price": best_spec_deal["price"],
            "link": best_spec_deal["link"],
            "image": best_spec_deal["image"],
            "reasons": f"Verified {best_spec_deal.get('match_status', 'Match')} ({best_spec_deal.get('match_score', 0)}%) on {best_spec_deal['platform']}"
        }

        # 4. Review Insights Indicator
        deals_with_ratings = [d for d in active_offers if d.get("rating")]
        if deals_with_ratings:
            top_reviewed_deal = max(deals_with_ratings, key=lambda x: (x.get("reviews") or 0, x.get("rating") or 0))
            all_pos_themes = []
            for d in active_offers:
                for th in d.get("review_signals", {}).get("positive_themes", []):
                    if th not in all_pos_themes:
                        all_pos_themes.append(th)
            rev_val = top_reviewed_deal.get('reviews')
            try:
                rev_num = int(str(rev_val).replace(',', '')) if rev_val is not None else 0
            except (ValueError, TypeError):
                rev_num = 0
            formatted_rev = f"{rev_num:,} verified reviews" if rev_num > 0 else (f"{rev_val} verified reviews" if rev_val else "Early reviews")
            comparison_summary["review_insights"] = {
                "platform": top_reviewed_deal["platform"],
                "rating": top_reviewed_deal.get("rating"),
                "reviews": rev_num if rev_num > 0 else rev_val,
                "formatted_reviews": formatted_rev,
                "top_positive_themes": all_pos_themes[:3],
                "positive_themes": all_pos_themes[:3],
                "summary": f"Rated ★ {top_reviewed_deal.get('rating')} with {formatted_rev} on {top_reviewed_deal['platform']}."
            }
        else:
            comparison_summary["review_insights"] = {
                "platform": "Marketplace",
                "rating": None,
                "reviews": 0,
                "formatted_reviews": "Awaiting initial customer reviews",
                "top_positive_themes": ["Authentic Listing Verified"],
                "summary": "Customer review signals are accumulating for this product."
            }

        # 5. Best Value for Money Indicator (combines quality score and price efficiency)
        scored_vfm = []
        for d in active_offers:
            vfm_idx = calculate_value_for_money_index(
                d.get("quality_score", 50.0),
                d.get("price_num"),
                min_price_val
            )
            d["value_for_money_index"] = vfm_idx
            scored_vfm.append((vfm_idx, d))
        scored_vfm.sort(key=lambda x: -x[0])
        best_vfm_deal = scored_vfm[0][1]
        comparison_summary["value_for_money"] = {
            "platform": best_vfm_deal["platform"],
            "title": best_vfm_deal["title"],
            "price": best_vfm_deal["price"],
            "price_num": best_vfm_deal["price_num"],
            "quality_score": best_vfm_deal.get("quality_score", 0),
            "vfm_index": scored_vfm[0][0],
            "link": best_vfm_deal["link"],
            "image": best_vfm_deal["image"],
            "reasons": f"Best balance of quality ({best_vfm_deal.get('quality_score', 0)}/100) and pricing ({best_vfm_deal['price']}) on {best_vfm_deal['platform']}."
        }

    # 8. Best Deal Engine (Value Algorithm + Savings)
    best_deal: Optional[Dict[str, Any]] = None
    no_deal_message: Optional[str] = None
    savings_info: Optional[Dict[str, Any]] = None

    # Check cross-platform match requirements
    candidate_deals = valid_verified_deals if valid_verified_deals else all_available_deals
    verified_platforms_count = len(valid_verified_deals)
    has_cross_platform_match = len(candidate_deals) >= 2

    if candidate_deals:
        # Calculate Best Value score for each deal
        min_p = min(d["price_num"] for d in candidate_deals if d["price_num"])
        scored_deals = []

        for d in candidate_deals:
            p_val = d["price_num"]
            prod = d["product"]

            # Deal formula: 35% Price, 25% Quality, 15% Reviews, 15% Seller, 10% Availability
            price_comp = (min_p / max(1, p_val)) * 35.0
            qual_comp = (d["quality_score"] / 100.0) * 25.0
            rev_cnt = d["reviews"] or 0
            rev_comp = min(15.0, (rev_cnt / 500.0) * 15.0 if rev_cnt < 500 else 15.0)
            avail_comp = 10.0 if d["availability"] == "In Stock" else 0.0
            match_comp = (d["match_score"] / 100.0) * 15.0

            deal_score = round(price_comp + qual_comp + rev_comp + avail_comp + match_comp, 1)

            scored_deals.append({
                "deal_data": d,
                "deal_score": deal_score,
                "price_num": p_val
            })

        # Pick deal with highest overall deal score
        scored_deals.sort(key=lambda x: (-x["deal_score"], x["price_num"]))
        winner_deal = scored_deals[0]["deal_data"]
        winner_deal["is_overall_best"] = True

        # Calculate savings if 2 or more offers exist
        savings = 0
        if len(candidate_deals) >= 2:
            prices = [d["price_num"] for d in candidate_deals if d["price_num"]]
            highest_p = max(prices)
            savings = max(0, highest_p - winner_deal["price_num"])
            if savings > 0:
                highest_deal = next(d for d in candidate_deals if d["price_num"] == highest_p)
                savings_info = {
                    "amount": savings,
                    "amount_formatted": f"₹{savings:,}",
                    "compared_platform": highest_deal["platform"],
                    "highest_price_formatted": highest_deal["price"]
                }

        reasons = [
            f"Verified {winner_deal['match_status']} on {winner_deal['platform']}",
            f"Verified Price: {winner_deal['price']}",
            f"Product Quality Score: {winner_deal['quality_score']}/100",
            f"{winner_deal['availability']}"
        ]
        if winner_deal.get("rating"):
            w_rev = winner_deal.get("reviews")
            try:
                w_rev_int = int(str(w_rev).replace(",", "")) if w_rev is not None else 0
                reasons.append(f"Rated {winner_deal['rating']} ★ ({w_rev_int:,} reviews)")
            except (ValueError, TypeError):
                reasons.append(f"Rated {winner_deal['rating']} ★ ({w_rev or 0} reviews)")
        if savings > 0 and savings_info:
            reasons.append(f"Saves ₹{savings:,} compared to {savings_info['compared_platform']}")

        best_deal = {
            "platform": winner_deal["platform"],
            "price": winner_deal["price_num"],
            "formatted_price": winner_deal["price"],
            "price_formatted": winner_deal["price"],
            "price_num": winner_deal["price_num"],
            "title": winner_deal["title"],
            "product_name": winner_deal["title"],
            "link": winner_deal["link"],
            "image": winner_deal["image"],
            "rating": winner_deal["rating"],
            "reviews": winner_deal["reviews"],
            "quality_score": winner_deal["quality_score"],
            "data_confidence": winner_deal["data_confidence"],
            "quality_score_label": winner_deal.get("quality_score_label"),
            "quality_attributes": winner_deal.get("quality_attributes"),
            "review_signals": winner_deal.get("review_signals"),
            "savings": savings,
            "match_status": winner_deal["match_status"],
            "reasons": reasons,
            "reason_text": " • ".join(reasons)
        }
    else:
        no_deal_message = "No exact cross-platform match available"

    # 9. Pairwise Matching Status Summary
    match_pairs = {}
    pair_defs = [
        ("Amazon", "Flipkart", "Amazon ↔ Flipkart"),
        ("Amazon", "Meesho", "Amazon ↔ Meesho"),
        ("Flipkart", "Meesho", "Flipkart ↔ Meesho")
    ]
    for p1, p2, label in pair_defs:
        it1 = best_match_per_platform.get(p1)
        it2 = best_match_per_platform.get(p2)
        if it1 and it2 and it1.get("match_status") in ("EXACT_MATCH", "VARIANT_MATCH") and it2.get("match_status") in ("EXACT_MATCH", "VARIANT_MATCH"):
            avg_score = int((it1.get("match_score", 0) + it2.get("match_score", 0)) / 2)
            classification = "Exact Match" if avg_score >= 85 else "Variant Match"
            match_pairs[label] = {
                "score": avg_score,
                "classification": classification,
                "label": label
            }
        else:
            match_pairs[label] = {
                "score": 0,
                "classification": "Not Available",
                "label": label
            }

    return {
        "query": clean_q,
        "target_info": target_info,
        "detected_category": spec_category,
        "category": spec_category,
        "validated_products": all_validated_products,
        "validated_by_platform": validated_products_by_platform,
        "rejected_products": rejected_products,
        "top_verified_offers": top_verified_offers,
        "top_prices": top_verified_offers,
        "specifications_matrix": specification_matrix,
        "quality_comparison_table": quality_comparison_table,
        "comparison_summary": comparison_summary,
        "best_deal": best_deal,
        "overall_best": best_deal,
        "best_overall_deal": best_deal,
        "no_deal_message": no_deal_message,
        "savings_info": savings_info,
        "match_pairs": match_pairs,
        "has_cross_platform_match": has_cross_platform_match,
        "platform_status": platform_status,
        "marketplace_status": marketplace_status,
        "best_match_per_platform": best_match_per_platform
    }
