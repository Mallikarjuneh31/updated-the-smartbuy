"""
services/price_prediction_service.py
====================================
7-Day AI Price Prediction System using Linear Regression:
- Captures and persists actual prices from Amazon, Flipkart, Meesho, and GEC E-Commerce
  with timestamps into MongoDB collection `price_history`.
- Trains a separate Linear Regression model per marketplace using real historical price observations.
- Forecasts predicted prices for the next 7 days for each marketplace.
- Computes current price, 7-day forecasted price, net price change, and trend classification.
- Strictly adheres to ground truth: displays a clear, informative message when historical data
  is insufficient (< 2 distinct points) without fabricating dummy predictions.
- Eliminates duplicate entries and ensures strict chronological date formatting and gap-spanning on charts.
"""

import os
import re
import logging
import concurrent.futures
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List, Tuple

import numpy as np
from sklearn.linear_model import LinearRegression

import database
from database import (
    init_db,
    record_price_history,
    _parse_numeric_price,
)
from search.normalizer import normalize_brand, normalize_title
from product_normalizer import KNOWN_BRANDS, extract_model_number, extract_variant_attributes

logger = logging.getLogger("smartbuy.price_prediction")

SUPPORTED_MARKETPLACES = ["amazon", "flipkart", "meesho", "gec"]

MARKETPLACE_DISPLAY_NAMES = {
    "amazon": "Amazon",
    "flipkart": "Flipkart",
    "meesho": "Meesho",
    "gec": "GEC E-Commerce"
}

MARKETPLACE_COLORS = {
    "amazon": {"primary": "#ff9900", "bg": "rgba(255, 153, 0, 0.15)"},
    "flipkart": {"primary": "#2874f0", "bg": "rgba(40, 116, 240, 0.15)"},
    "meesho": {"primary": "#f43397", "bg": "rgba(244, 51, 151, 0.15)"},
    "gec": {"primary": "#0284c7", "bg": "rgba(2, 132, 199, 0.15)"}
}

STOP_WORDS = {
    "for", "and", "with", "the", "buy", "in", "online", "of", "to", "a", "an", "at",
    "by", "from", "on", "or", "is", "all", "new", "latest", "best", "pro", "plus",
    "gb", "ram", "storage", "black", "blue", "white", "pack", "men", "women"
}


def _normalize_dt(dt: Any) -> Optional[datetime]:
    """Ensure datetime is offset-aware UTC to prevent naive/aware subtraction TypeErrors."""
    if dt is None:
        return None
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    if isinstance(dt, str):
        try:
            parsed = datetime.fromisoformat(dt.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
        except Exception:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                try:
                    parsed = datetime.strptime(dt[:19], fmt).replace(tzinfo=timezone.utc)
                    return parsed
                except Exception:
                    pass
    return None


def _get_platform_mongo_pattern(platform: str) -> re.Pattern:
    """Return regex pattern to match platform variants stored in MongoDB."""
    plat = (platform or "").strip().lower()
    if plat in ("gec", "gececom", "gec ecom", "gec e-commerce"):
        return re.compile(r"^(gec|gececom|gec ecom|gec e-commerce)", re.IGNORECASE)
    return re.compile(rf"^{re.escape(plat)}", re.IGNORECASE)


def _extract_significant_tokens(text: str) -> List[str]:
    """Extract key alphanumeric tokens for flexible, order-independent search matching."""
    if not text:
        return []
    raw_tokens = re.findall(r'[a-zA-Z0-9]+', text)
    tokens = [t for t in raw_tokens if len(t) > 1 and t.lower() not in STOP_WORDS]
    return tokens


def compute_canonical_key(
    title: str,
    brand: Optional[str] = None,
    model: Optional[str] = None,
    variant: Optional[str] = None
) -> str:
    """
    Generate a normalized, deterministic canonical product key so different products
    never share or contaminate each other's price history.
    Example:
      'Apple iPhone 15 (128 GB) - Black' -> 'apple_iphone_15_128gb'
      'boAt Airdopes 141 True Wireless Earbuds' -> 'boat_airdopes_141'
      'Ant Esports GM320 RGB Optical Wired Gaming Mouse' -> 'ant_esports_gm320'
    """
    clean_title = (title or "").strip()
    if not clean_title:
        return ""
    norm_t = normalize_title(clean_title)
    t_low = clean_title.lower()

    # 1. Brand extraction
    b = brand
    if not b:
        for kb in KNOWN_BRANDS:
            if re.search(r'\b' + re.escape(kb.lower()) + r'\b', t_low):
                b = kb
                break
    if not b:
        toks = clean_title.split()
        b = toks[0] if toks else "generic"
    b_slug = re.sub(r'[^a-z0-9]', '', str(b).lower())

    # 2. Model extraction
    m = model or extract_model_number(clean_title, b)
    if not m:
        words = [
            w for w in re.findall(r'[a-z0-9]+', norm_t)
            if w != b_slug and w not in (
                'the', 'for', 'and', 'with', 'gb', 'ram', 'pro', 'plus',
                'true', 'wireless', 'earbuds', 'mouse', 'bag', 'shoes',
                'phone', 'laptop', 'men', 'women', 'black', 'blue', 'white'
            )
        ]
        m_slug = '_'.join(words[:2]) if words else "standard"
    else:
        m_slug = re.sub(r'[^a-z0-9]', '_', str(m).lower()).strip('_')
        m_slug = re.sub(r'_+', '_', m_slug)

    # 3. Variant attributes (storage, weight, pack quantity)
    v_attrs = extract_variant_attributes(clean_title)
    v_parts = []
    if v_attrs.get('storage'):
        v_parts.append(str(v_attrs['storage']).lower())
    if v_attrs.get('weight'):
        v_parts.append(re.sub(r'\s+', '', str(v_attrs['weight']).lower()))
    pq = v_attrs.get('pack_quantity')
    if pq and int(pq) > 1:
        v_parts.append('pack' + str(pq))

    v_str = '_'.join(v_parts)
    key_parts = [b_slug, m_slug]
    if v_str and v_str not in m_slug:
        key_parts.append(v_str)

    raw_key = '_'.join(p for p in key_parts if p)
    raw_key = re.sub(r'[^a-z0-9_]', '', raw_key)
    raw_key = re.sub(r'_+', '_', raw_key).strip('_')
    return raw_key


_backfill_done = False

def migrate_backfill_canonical_keys() -> int:
    """
    Backfill canonical_key for existing price_history records missing it.
    Ensures every product's price history is queryable by its isolated canonical_key.
    Runs once per process lifecycle or on demand.
    """
    global _backfill_done
    if _backfill_done:
        return 0
    if database.db is None:
        init_db()
    if database.db is None:
        return 0

    try:
        cursor = database.db.price_history.find({
            "$or": [
                {"canonical_key": {"$exists": False}},
                {"canonical_key": ""},
                {"canonical_key": None}
            ]
        })
        updated = 0
        for doc in cursor:
            pname = doc.get("product_name") or ""
            if not pname:
                continue
            ck = compute_canonical_key(pname)
            if ck:
                database.db.price_history.update_one(
                    {"_id": doc["_id"]},
                    {"$set": {"canonical_key": ck}}
                )
                updated += 1
        _backfill_done = True
        if updated > 0:
            logger.info(f"[PricePrediction] Backfilled canonical_key for {updated} price records.")
        return updated
    except Exception as e:
        logger.error(f"[PricePrediction] Error backfilling canonical keys: {e}")
        return 0


def harvest_real_price_history_for_product(
    product_name: str,
    canonical_key: str = "",
    platform: Optional[str] = None
) -> int:
    """
    If a product has insufficient historical data (< 2 points), harvest real past price records
    from existing MongoDB collections (gececom_products, products, search_history) without
    fabricating fake or dummy prices.
    Saves them into MongoDB `price_history` using the product's unique canonical_key.
    """
    if database.db is None:
        init_db()
    if database.db is None:
        return 0

    clean_pname = str(product_name or "").strip()
    ck = str(canonical_key or compute_canonical_key(clean_pname)).strip().lower()
    if not ck and not clean_pname:
        return 0

    harvested_count = 0
    plat_filter = platform.strip().lower() if platform else None

    # 1. Harvest from gececom_products
    if not plat_filter or plat_filter in ("gec", "gececom", "gec ecom", "gec e-commerce"):
        try:
            tokens = _extract_significant_tokens(clean_pname)
            gec_query = {}
            if len(tokens) >= 2:
                top_2 = tokens[:2]
                pat = "".join(f"(?=.*{re.escape(t)})" for t in top_2)
                gec_query = {"product_name": re.compile(pat, re.I)}
            elif clean_pname:
                gec_query = {"product_name": re.compile(re.escape(clean_pname), re.I)}

            matched_gec = list(database.db.gececom_products.find(gec_query)) if gec_query else []
            for g_doc in matched_gec:
                g_title = g_doc.get("product_name") or clean_pname
                g_ck = compute_canonical_key(g_title)
                if ck and g_ck and ck != g_ck and not (ck in g_ck or g_ck in ck):
                    continue

                g_id = str(g_doc.get("_id"))
                for entry in g_doc.get("price_history", []):
                    e_price = _parse_numeric_price(entry.get("price"))
                    e_dt = _normalize_dt(entry.get("changed_at") or entry.get("date") or entry.get("timestamp"))
                    if e_price and e_price > 0 and e_dt:
                        exists = database.db.price_history.find_one({
                            "platform": _get_platform_mongo_pattern("gec"),
                            "canonical_key": ck,
                            "date": {"$gte": e_dt - timedelta(hours=1), "$lte": e_dt + timedelta(hours=1)}
                        })
                        if not exists:
                            database.db.price_history.insert_one({
                                "product_name": g_title,
                                "platform": "gec",
                                "price": float(e_price),
                                "formatted_price": f"₹{e_price:,.0f}",
                                "product_url": f"/gececom#product-{g_id}",
                                "product_id": g_id,
                                "canonical_key": ck,
                                "recorded_at": e_dt,
                                "date": e_dt,
                                "created_at": e_dt,
                                "timestamp": e_dt.strftime("%Y-%m-%d %H:%M:%S"),
                                "source": "gececom_harvest"
                            })
                            harvested_count += 1

                orig_p = _parse_numeric_price(g_doc.get("original_price"))
                c_at = _normalize_dt(g_doc.get("created_at"))
                if orig_p and orig_p > 0 and c_at:
                    exists = database.db.price_history.find_one({
                        "platform": _get_platform_mongo_pattern("gec"),
                        "canonical_key": ck,
                        "date": {"$gte": c_at - timedelta(hours=1), "$lte": c_at + timedelta(hours=1)}
                    })
                    if not exists:
                        database.db.price_history.insert_one({
                            "product_name": g_title,
                            "platform": "gec",
                            "price": float(orig_p),
                            "formatted_price": f"₹{orig_p:,.0f}",
                            "product_url": f"/gececom#product-{g_id}",
                            "product_id": g_id,
                            "canonical_key": ck,
                            "recorded_at": c_at,
                            "date": c_at,
                            "created_at": c_at,
                            "timestamp": c_at.strftime("%Y-%m-%d %H:%M:%S"),
                            "source": "gececom_original_price"
                        })
                        harvested_count += 1
        except Exception as e:
            logger.debug(f"[PricePrediction] GEC harvest error: {e}")

    # 2. Harvest from products collection across Amazon, Flipkart, Meesho, GEC
    try:
        tokens = _extract_significant_tokens(clean_pname)
        if len(tokens) >= 2:
            top_2 = tokens[:2]
            pat = "".join(f"(?=.*{re.escape(t)})" for t in top_2)
            p_q = {"product_name": re.compile(pat, re.I)}
            if plat_filter:
                p_q["platform"] = _get_platform_mongo_pattern(plat_filter)

            matched_prods = list(database.db.products.find(p_q).limit(25))
            for p_doc in matched_prods:
                p_title = p_doc.get("product_name") or ""
                p_ck = compute_canonical_key(p_title)
                if ck and p_ck and ck != p_ck and not (ck in p_ck or p_ck in ck):
                    continue

                p_plat = str(p_doc.get("platform") or "amazon").strip().lower()
                p_price = _parse_numeric_price(p_doc.get("price") or p_doc.get("price_num"))
                p_dt = _normalize_dt(p_doc.get("scraped_at") or p_doc.get("created_at"))
                if p_price and p_price > 0 and p_dt:
                    exists = database.db.price_history.find_one({
                        "platform": _get_platform_mongo_pattern(p_plat),
                        "canonical_key": ck,
                        "date": {"$gte": p_dt - timedelta(hours=1), "$lte": p_dt + timedelta(hours=1)}
                    })
                    if not exists:
                        database.db.price_history.insert_one({
                            "product_name": p_title,
                            "platform": p_plat,
                            "price": float(p_price),
                            "formatted_price": f"₹{p_price:,.0f}",
                            "product_url": str(p_doc.get("product_url") or ""),
                            "product_id": str(p_doc.get("external_product_id") or p_doc.get("_id")),
                            "canonical_key": ck,
                            "recorded_at": p_dt,
                            "date": p_dt,
                            "created_at": p_dt,
                            "timestamp": p_dt.strftime("%Y-%m-%d %H:%M:%S"),
                            "source": "products_harvest"
                        })
                        harvested_count += 1
    except Exception as e:
        logger.debug(f"[PricePrediction] Products collection harvest error: {e}")

    if harvested_count > 0:
        logger.info(f"[PricePrediction] Harvested {harvested_count} real past price records for '{clean_pname}' ({ck}).")
    return harvested_count


def deduplicate_price_history() -> int:
    """
    Remove exact duplicate price records in MongoDB (same product, platform, price, and calendar date).
    Keeps the earliest record and deletes redundant duplicates.
    """
    if database.db is None:
        init_db()
    if database.db is None:
        return 0

    try:
        all_docs = list(database.db.price_history.find())
        seen = {}
        dup_ids = []

        for d in all_docs:
            pname = str(d.get("product_name", "")).strip().lower()
            plat = str(d.get("platform", "")).strip().lower()
            price = _parse_numeric_price(d.get("price"))
            raw_dt = d.get("recorded_at") or d.get("date") or d.get("created_at")
            dt_obj = _normalize_dt(raw_dt)
            date_key = dt_obj.strftime("%Y-%m-%d") if dt_obj else "unknown"

            key = (pname, plat, price, date_key)
            if key in seen:
                dup_ids.append(d["_id"])
            else:
                seen[key] = d["_id"]

        if dup_ids:
            res = database.db.price_history.delete_many({"_id": {"$in": dup_ids}})
            logger.info(f"[PricePrediction] Cleaned up {res.deleted_count} duplicate price records.")
            return res.deleted_count
        return 0
    except Exception as e:
        logger.error(f"[PricePrediction] Error during price_history deduplication: {e}")
        return 0


_bg_record_executor = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="price_rec_bg")


def _extract_all_product_items(platform_data: Any) -> List[Dict[str, Any]]:
    """Recursively extract individual product dictionaries from any results structure."""
    items: List[Dict[str, Any]] = []
    if not platform_data:
        return items

    if isinstance(platform_data, list):
        for entry in platform_data:
            if isinstance(entry, dict):
                items.append(entry)
            elif isinstance(entry, list):
                items.extend(_extract_all_product_items(entry))
    elif isinstance(platform_data, dict):
        if "all_results" in platform_data and isinstance(platform_data["all_results"], list):
            items.extend(_extract_all_product_items(platform_data["all_results"]))
        if "platform_results" in platform_data and isinstance(platform_data["platform_results"], dict):
            for p_list in platform_data["platform_results"].values():
                items.extend(_extract_all_product_items(p_list))
        if "best_per_platform" in platform_data and isinstance(platform_data["best_per_platform"], dict):
            for b_item in platform_data["best_per_platform"].values():
                if isinstance(b_item, dict):
                    items.append(b_item)
        if "matches" in platform_data and isinstance(platform_data["matches"], dict):
            for m_item in platform_data["matches"].values():
                if isinstance(m_item, dict):
                    items.append(m_item)

        if not items:
            if "title" in platform_data or "product_name" in platform_data:
                items.append(platform_data)
            else:
                for val in platform_data.values():
                    items.extend(_extract_all_product_items(val))

    return items


def _record_items_worker(items: List[Dict[str, Any]], query: str) -> int:
    """Background worker function to persist actual prices from all marketplaces to MongoDB."""
    if database.db is None:
        init_db()
    if database.db is None or not items:
        return 0

    now_utc = datetime.now(timezone.utc)
    one_hour_ago = now_utc - timedelta(hours=1)
    today_str = now_utc.strftime("%Y-%m-%d")
    inserted_count = 0
    seen_in_batch = set()

    for it in items:
        try:
            raw_title = it.get("title") or it.get("product_name") or query
            clean_title = str(raw_title or "").strip()
            if not clean_title or len(clean_title) < 2:
                continue

            raw_price = it.get("price_num") or it.get("price") or it.get("current_price")
            price_num = _parse_numeric_price(raw_price)
            if not price_num or price_num <= 0:
                continue

            raw_plat = str(it.get("platform") or "").strip().lower()
            target_plat = None
            for p in SUPPORTED_MARKETPLACES:
                if p in raw_plat or (p == "gec" and any(x in raw_plat for x in ("gececom", "gec", "gec ecom", "gec e-commerce"))):
                    target_plat = p
                    break
            if not target_plat:
                continue

            ck = compute_canonical_key(clean_title) or compute_canonical_key(query)
            batch_key = (ck, target_plat, price_num)
            if batch_key in seen_in_batch:
                continue
            seen_in_batch.add(batch_key)

            url = str(it.get("link") or it.get("product_url") or it.get("url") or "")
            p_id = str(it.get("product_id") or it.get("id") or "")

            # Deduplication: do not insert identical price for same product/platform within 1 hour or today
            existing = database.db.price_history.find_one({
                "platform": _get_platform_mongo_pattern(target_plat),
                "price": price_num,
                "canonical_key": ck,
                "$or": [
                    {"recorded_at": {"$gte": one_hour_ago}},
                    {"date": {"$gte": one_hour_ago}},
                    {"timestamp": re.compile(rf"^{today_str}")}
                ]
            })

            if not existing:
                record_price_history(
                    product_name=clean_title,
                    platform=target_plat,
                    price=price_num,
                    product_url=url,
                    product_id=p_id,
                    search_query=query,
                    canonical_key=ck,
                    recorded_at=now_utc,
                    date=now_utc
                )
                inserted_count += 1
        except Exception as e:
            logger.debug(f"[PricePrediction] Error recording item: {e}")

    if inserted_count > 0:
        logger.info(f"[PricePrediction] Persisted {inserted_count} live price records in MongoDB for query '{query}'.")
    return inserted_count


def record_search_prices_to_history(
    query: str,
    platform_data: Any
) -> Dict[str, Any]:
    """
    Whenever a user searches, asynchronously persist actual product prices collected from
    Amazon, Flipkart, Meesho, and GEC E-Commerce with timestamps into MongoDB `price_history`.
    Extracts all searched products without delaying search response time.
    """
    clean_q = str(query or "").strip()
    all_items = _extract_all_product_items(platform_data)
    if not all_items:
        return {"recorded": 0, "status": "no_items"}

    try:
        _bg_record_executor.submit(_record_items_worker, all_items, clean_q)
        return {"recorded": len(all_items), "status": "queued"}
    except Exception as e:
        logger.warning(f"[PricePrediction] Failed to dispatch background price recording: {e}")
        try:
            cnt = _record_items_worker(all_items, clean_q)
            return {"recorded": cnt, "status": "synced"}
        except Exception:
            return {"recorded": 0, "status": "error"}


def fetch_historical_points_for_platform(
    platform: str,
    query: str = "",
    product_name: str = "",
    canonical_key: str = "",
    limit: int = 100
) -> List[Dict[str, Any]]:
    """
    Retrieve real timestamped historical price observations from MongoDB for a marketplace.
    Guarantees product isolation using canonical_key so different products never share price history.
    Harvests real past prices from existing collections if historical observations < 2 without fake data.
    """
    if database.db is None:
        init_db()
    if database.db is None:
        return []

    plat_pattern = _get_platform_mongo_pattern(platform)
    clean_q = (query or "").strip()
    clean_pname = (product_name or "").strip()
    target_ck = str(canonical_key or compute_canonical_key(clean_pname or clean_q)).strip().lower()

    docs: List[Dict[str, Any]] = []

    # 1. Primary Query: exact canonical_key match for this platform
    if target_ck:
        try:
            docs = list(database.db.price_history.find({
                "platform": plat_pattern,
                "canonical_key": target_ck
            }).sort([("recorded_at", 1), ("date", 1)]).limit(limit))
        except Exception:
            docs = []

    # 2. Check distinct dates; if < 2, harvest real past price records for this product
    distinct_dates = set()
    for d in docs:
        dt_obj = _normalize_dt(d.get("recorded_at") or d.get("date") or d.get("created_at"))
        if dt_obj:
            distinct_dates.add(dt_obj.strftime("%Y-%m-%d"))

    if len(distinct_dates) < 2 and (clean_pname or clean_q):
        harvest_real_price_history_for_product(
            clean_pname or clean_q,
            canonical_key=target_ck,
            platform=platform
        )
        if target_ck:
            try:
                docs = list(database.db.price_history.find({
                    "platform": plat_pattern,
                    "canonical_key": target_ck
                }).sort([("recorded_at", 1), ("date", 1)]).limit(limit))
            except Exception:
                docs = []

    # 3. Fallback: if no docs found by canonical_key yet (e.g. unmigrated records), match by specific brand + model tokens
    if not docs:
        target_tokens = _extract_significant_tokens(clean_pname or clean_q)
        if len(target_tokens) >= 2:
            top_tokens = target_tokens[:3]
            lookahead_pat = "".join(f"(?=.*{re.escape(t)})" for t in top_tokens)
            try:
                docs = list(database.db.price_history.find({
                    "platform": plat_pattern,
                    "product_name": re.compile(lookahead_pat, re.IGNORECASE)
                }).sort([("recorded_at", 1), ("date", 1)]).limit(limit))
            except Exception:
                docs = []
            # Stamp matched unindexed docs with canonical_key
            if docs and target_ck:
                for d in docs:
                    if not d.get("canonical_key"):
                        try:
                            database.db.price_history.update_one(
                                {"_id": d["_id"]},
                                {"$set": {"canonical_key": target_ck}}
                            )
                        except Exception:
                            pass

    if not docs:
        return []

    # Parse and deduplicate by calendar date (keep latest price observation for each day)
    points_by_date: Dict[str, Dict[str, Any]] = {}

    for d in docs:
        price = _parse_numeric_price(d.get("price"))
        if not price or price <= 0:
            continue

        raw_dt = d.get("recorded_at") or d.get("date") or d.get("created_at") or d.get("timestamp")
        dt_obj = _normalize_dt(raw_dt)
        if not dt_obj:
            continue

        date_key = dt_obj.strftime("%Y-%m-%d")
        existing_point = points_by_date.get(date_key)

        # If already seen today, keep the latest chronological observation
        if not existing_point or dt_obj >= existing_point["datetime"]:
            points_by_date[date_key] = {
                "datetime": dt_obj,
                "date_str": dt_obj.strftime("%b %d"),
                "full_date": date_key,
                "price": float(price),
                "product_name": d.get("product_name", ""),
                "canonical_key": target_ck
            }

    # Sort strictly by chronological order
    cleaned_points = sorted(points_by_date.values(), key=lambda p: p["datetime"])
    return cleaned_points


def train_and_predict_7d_linear_regression(
    historical_points: List[Dict[str, Any]],
    current_price: Optional[float] = None
) -> Dict[str, Any]:
    """
    Train Linear Regression model on real historical price points and predict next 7 days.
    If historical points < 2 distinct dates, returns an explicit insufficient data response without fake data.
    """
    now_utc = datetime.now(timezone.utc)
    points_to_train = []

    for pt in historical_points:
        norm_dt = _normalize_dt(pt.get("datetime"))
        if norm_dt:
            points_to_train.append({
                "datetime": norm_dt,
                "date_str": pt.get("date_str") or norm_dt.strftime("%b %d"),
                "full_date": pt.get("full_date") or norm_dt.strftime("%Y-%m-%d"),
                "price": float(pt["price"]),
                "product_name": pt.get("product_name", "")
            })

    # If current live price is provided and newer than last point, include it
    if current_price and current_price > 0:
        c_price = float(current_price)
        if not points_to_train:
            points_to_train.append({
                "datetime": now_utc,
                "date_str": now_utc.strftime("%b %d"),
                "full_date": now_utc.strftime("%Y-%m-%d"),
                "price": c_price,
                "product_name": "Current Live Price"
            })
        else:
            last_dt = points_to_train[-1]["datetime"]
            # If newer than last recorded point by at least 10 minutes or different calendar day
            if (now_utc - last_dt).total_seconds() > 600 or now_utc.strftime("%Y-%m-%d") != last_dt.strftime("%Y-%m-%d"):
                points_to_train.append({
                    "datetime": now_utc,
                    "date_str": now_utc.strftime("%b %d"),
                    "full_date": now_utc.strftime("%Y-%m-%d"),
                    "price": c_price,
                    "product_name": "Current Live Price"
                })

    distinct_dates = {p["datetime"].strftime("%Y-%m-%d") for p in points_to_train}

    # Need at least 2 distinct temporal observations to fit a linear regression line
    if len(points_to_train) < 2 or len(distinct_dates) < 2:
        hist_count = len(points_to_train)
        curr = current_price if current_price else (points_to_train[0]["price"] if points_to_train else None)
        curr_val = round(curr, 2) if curr else None
        target_pred_date = (now_utc + timedelta(days=7)).strftime("%b %d, %Y")
        return {
            "sufficient_data": False,
            "has_prediction": False,
            "historical_count": hist_count,
            "data_points_count": hist_count,
            "days_ahead": 7,
            "message": (
                f"Insufficient historical price data ({hist_count} observation recorded, minimum 3 required). "
                "SmartBuy logs live prices to MongoDB on every search. "
                "Linear Regression price forecasting will activate automatically once additional price points are logged."
            ),
            "current_price": curr_val,
            "current_price_formatted": f"₹{curr_val:,.2f}" if curr_val else "—",
            "predicted_7d_price": None,
            "predicted_price_7d": None,
            "predicted_price_formatted": "—",
            "price_difference": 0.0,
            "price_difference_formatted": "—",
            "price_change_7d": None,
            "price_change_pct": None,
            "percentage_change": 0.0,
            "percentage_change_formatted": "—",
            "direction": "INSUFFICIENT_DATA",
            "trend": "insufficient_data",
            "trend_label": "Insufficient historical data for reliable prediction",
            "recommendation": f"SmartBuy has recorded {hist_count} of 3 required price observations. Live tracking active.",
            "prediction_date": target_pred_date,
            "badge_class": "badge bg-secondary-subtle text-secondary border",
            "icon": "fas fa-clock",
            "r2_score": 0.0,
            "model_slope": 0.0,
            "history": [
                {
                    "date": p["date_str"],
                    "full_date": p["full_date"],
                    "price": p["price"],
                    "formatted_price": f"₹{p['price']:,.2f}"
                }
                for p in points_to_train
            ],
            "historical_data": [
                {
                    "date": p["date_str"],
                    "full_date": p["full_date"],
                    "price": p["price"]
                }
                for p in points_to_train
            ],
            "daily_forecast": [],
            "prediction_data": []
        }

    # Prepare features for Linear Regression
    t0 = points_to_train[0]["datetime"]
    X = np.array([[(p["datetime"] - t0).total_seconds() / 86400.0] for p in points_to_train])
    y = np.array([p["price"] for p in points_to_train])

    # Fit Linear Regression Model
    reg = LinearRegression()
    reg.fit(X, y)

    slope = float(reg.coef_[0])  # Rupees per day
    intercept = float(reg.intercept_)

    try:
        if np.all(y == y[0]):
            r2 = 1.0
        else:
            r2 = float(reg.score(X, y))
            r2 = max(0.0, min(1.0, r2))
    except Exception:
        r2 = 0.5
    r2_score_val = round(r2, 3)

    # Generate 7-day predictions from today (Day 0 to Day 7)
    prediction_series = []
    for day_offset in range(8):  # Day 0 (today) to Day 7
        target_dt = now_utc + timedelta(days=day_offset)
        x_val = (target_dt - t0).total_seconds() / 86400.0
        pred_val = float(reg.predict([[x_val]])[0])

        # Logical boundary: ensure predicted price does not fall below 1 rupee
        pred_val = max(1.0, round(pred_val, 2))

        prediction_series.append({
            "day_offset": day_offset,
            "day": day_offset,
            "date": target_dt.strftime("%b %d"),
            "full_date": target_dt.strftime("%Y-%m-%d"),
            "predicted_price": pred_val,
            "formatted_price": f"₹{pred_val:,.2f}"
        })

    # Effective current price (actual current price if available, else Day 0 prediction)
    effective_current = current_price if current_price else prediction_series[0]["predicted_price"]
    predicted_7d = prediction_series[7]["predicted_price"]
    price_change = round(predicted_7d - effective_current, 2)
    change_pct = round((price_change / effective_current) * 100, 2) if effective_current > 0 else 0.0

    # Trend categorization & recommendation
    if change_pct <= -0.5:
        trend = "falling"
        direction = "DECREASE"
        trend_label = f"Expected to decrease by ₹{abs(price_change):,.2f} ({abs(change_pct):.1f}%)"
        trend_msg = f"AI Linear Regression projects a price drop of ₹{abs(price_change):,.0f} ({abs(change_pct):.1f}%) over the next 7 days."
        recommendation = "Price drop projected: Consider waiting before purchase for potential savings."
        badge_class = "badge bg-success-subtle text-success border border-success"
        icon = "fas fa-arrow-trend-down"
    elif change_pct >= 0.5:
        trend = "rising"
        direction = "INCREASE"
        trend_label = f"Expected to increase by ₹{price_change:,.2f} (+{change_pct:.1f}%)"
        trend_msg = f"AI Linear Regression projects a price rise of ₹{price_change:,.0f} (+{change_pct:.1f}%) over the next 7 days."
        recommendation = "Price rise projected: Good time to buy before the price increases."
        badge_class = "badge bg-danger-subtle text-danger border border-danger"
        icon = "fas fa-arrow-trend-up"
    else:
        trend = "stable"
        direction = "STABLE"
        trend_label = "Price likely to remain stable over the next 7 days"
        trend_msg = f"AI Linear Regression projects price will remain stable around ₹{effective_current:,.0f} over the next 7 days."
        recommendation = "Price stable: You can purchase now without waiting for significant fluctuations."
        badge_class = "badge bg-info-subtle text-info border border-info"
        icon = "fas fa-arrows-left-right"

    diff_str = f"+₹{price_change:,.2f}" if price_change > 0 else (f"-₹{abs(price_change):,.2f}" if price_change < 0 else "₹0.00")
    pct_str = f"+{change_pct:.1f}%" if change_pct > 0 else f"{change_pct:.1f}%"
    target_pred_date = (now_utc + timedelta(days=7)).strftime("%b %d, %Y")

    formatted_history = [
        {
            "date": p["date_str"],
            "full_date": p["full_date"],
            "price": p["price"],
            "formatted_price": f"₹{p['price']:,.2f}"
        }
        for p in points_to_train
    ]

    return {
        "sufficient_data": True,
        "has_prediction": True,
        "historical_count": len(points_to_train),
        "data_points_count": len(points_to_train),
        "days_ahead": 7,
        "model": "Linear Regression (Ordinary Least Squares)",
        "algorithm": "Scikit-Learn Linear Regression",
        "daily_rate_rs": round(slope, 2),
        "model_slope": round(slope, 2),
        "r2_score": r2_score_val,
        "current_price": round(effective_current, 2),
        "current_price_formatted": f"₹{effective_current:,.2f}",
        "predicted_7d_price": predicted_7d,
        "predicted_price_7d": predicted_7d,
        "predicted_price_formatted": f"₹{predicted_7d:,.2f}",
        "price_change_7d": price_change,
        "price_difference": price_change,
        "price_difference_formatted": diff_str,
        "price_change_pct": change_pct,
        "percentage_change": change_pct,
        "percentage_change_formatted": pct_str,
        "direction": direction,
        "trend": trend,
        "trend_label": trend_label,
        "recommendation": recommendation,
        "badge_class": badge_class,
        "icon": icon,
        "prediction_date": target_pred_date,
        "message": trend_msg,
        "history": formatted_history,
        "historical_data": [
            {
                "date": p["date_str"],
                "full_date": p["full_date"],
                "price": p["price"]
            }
            for p in points_to_train
        ],
        "daily_forecast": prediction_series[1:],  # Day 1 to 7
        "prediction_data": prediction_series
    }


def get_marketplace_7d_predictions(
    query: str,
    product_name: str = "",
    canonical_key: str = "",
    current_prices: Optional[Dict[str, float]] = None
) -> Dict[str, Any]:
    """
    Main aggregator: trains and computes 7-day linear regression predictions
    separately for Amazon, Flipkart, Meesho, and GEC E-Commerce.
    Formats multi-series Chart.js payload with strictly chronological dates and gap-spanning.
    """
    migrate_backfill_canonical_keys()

    clean_q = (query or "").strip()
    clean_pname = (product_name or clean_q).strip()
    target_ck = str(canonical_key or compute_canonical_key(clean_pname or clean_q)).strip().lower()
    current_prices = current_prices or {}

    marketplaces_result = {}
    any_sufficient = False
    best_7d_deal = None
    min_7d_price = float("inf")

    now_utc = datetime.now(timezone.utc)
    today_full_date = now_utc.strftime("%Y-%m-%d")

    # Generate future 7 days (Day 0 to Day 7)
    future_full_dates = [(now_utc + timedelta(days=d)).strftime("%Y-%m-%d") for d in range(8)]
    all_dates_set = set(future_full_dates)

    # 1. Fetch historical observations and train predictions per platform
    for plat in SUPPORTED_MARKETPLACES:
        curr_p = current_prices.get(plat) or current_prices.get(plat.capitalize())
        points = fetch_historical_points_for_platform(
            platform=plat,
            query=clean_q,
            product_name=clean_pname,
            canonical_key=target_ck
        )

        pred_res = train_and_predict_7d_linear_regression(
            historical_points=points,
            current_price=curr_p
        )

        display_name = MARKETPLACE_DISPLAY_NAMES[plat]
        pred_res["platform_name"] = display_name
        pred_res["platform"] = display_name
        pred_res["product_name"] = clean_pname or clean_q
        pred_res["colors"] = MARKETPLACE_COLORS[plat]
        marketplaces_result[plat] = pred_res

        if pred_res.get("sufficient_data"):
            any_sufficient = True
            p7 = pred_res.get("predicted_7d_price")
            if p7 and p7 < min_7d_price:
                min_7d_price = p7
                best_7d_deal = {
                    "platform": display_name,
                    "predicted_price": p7,
                    "change_pct": pred_res.get("price_change_pct"),
                    "trend": pred_res.get("trend")
                }

        # Collect unique full dates
        for h in pred_res.get("historical_data", []):
            if h.get("full_date"):
                all_dates_set.add(h["full_date"])

    # Map predictions to display keys for results.html
    predictions_payload = {
        "Amazon": marketplaces_result["amazon"],
        "Flipkart": marketplaces_result["flipkart"],
        "Meesho": marketplaces_result["meesho"],
        "GEC ECom": marketplaces_result["gec"]
    }

    # Determine best drop platform (platform with highest percentage drop)
    best_drop_platform = None
    max_drop = 0.0
    for plat_key, p_res in predictions_payload.items():
        if p_res.get("sufficient_data") and p_res.get("direction") == "DECREASE":
            drop_val = abs(p_res.get("percentage_change", 0.0))
            if drop_val > max_drop:
                max_drop = drop_val
                best_drop_platform = plat_key

    # 2. Sort all unique dates strictly chronologically
    sorted_full_dates = sorted(all_dates_set)

    # Format user-friendly X-axis display labels (e.g. "Oct 09", "Oct 14")
    chart_labels = []
    for f_date in sorted_full_dates:
        try:
            d_obj = datetime.strptime(f_date, "%Y-%m-%d")
            chart_labels.append(d_obj.strftime("%b %d"))
        except Exception:
            chart_labels.append(f_date)

    # 3. Build multi-series Chart.js datasets with solid actual lines & dashed forecast lines
    chart_datasets = []

    for plat in SUPPORTED_MARKETPLACES:
        p_res = marketplaces_result[plat]
        color_info = MARKETPLACE_COLORS[plat]
        plat_name = MARKETPLACE_DISPLAY_NAMES[plat]

        # Actual prices map by full_date
        hist_by_date = {h["full_date"]: h["price"] for h in p_res.get("historical_data", [])}
        if p_res.get("current_price"):
            hist_by_date[today_full_date] = p_res.get("current_price")

        actual_data_points = []
        for f_date in sorted_full_dates:
            if f_date <= today_full_date and f_date in hist_by_date:
                actual_data_points.append(hist_by_date[f_date])
            else:
                actual_data_points.append(None)

        if any(v is not None for v in actual_data_points):
            chart_datasets.append({
                "label": f"{plat_name} (Actual Price)",
                "data": actual_data_points,
                "borderColor": color_info["primary"],
                "backgroundColor": color_info["bg"],
                "borderWidth": 2.5,
                "pointRadius": 4,
                "pointHoverRadius": 6,
                "pointBackgroundColor": color_info["primary"],
                "fill": False,
                "tension": 0.2,
                "borderDash": [],
                "spanGaps": True
            })

        if p_res.get("sufficient_data"):
            pred_by_date = {p["full_date"]: p["predicted_price"] for p in p_res.get("prediction_data", [])}
            pred_data_points = []
            for f_date in sorted_full_dates:
                if f_date >= today_full_date and f_date in pred_by_date:
                    pred_data_points.append(pred_by_date[f_date])
                else:
                    pred_data_points.append(None)

            chart_datasets.append({
                "label": f"{plat_name} (7-Day AI Forecast)",
                "data": pred_data_points,
                "borderColor": color_info["primary"],
                "backgroundColor": "transparent",
                "borderWidth": 2.5,
                "pointRadius": 5,
                "pointHoverRadius": 7,
                "pointStyle": "rectRot",
                "pointBackgroundColor": "#ffffff",
                "pointBorderColor": color_info["primary"],
                "borderDash": [6, 4],
                "fill": False,
                "tension": 0.1,
                "spanGaps": True
            })

    return {
        "success": True,
        "algorithm": "Linear Regression (Scikit-Learn)",
        "days_ahead": 7,
        "query": clean_q,
        "product_name": clean_pname,
        "canonical_key": target_ck,
        "has_any_prediction": any_sufficient,
        "has_predictions": any_sufficient,
        "best_drop_platform": best_drop_platform,
        "best_7d_deal": best_7d_deal,
        "predictions": predictions_payload,
        "marketplaces": marketplaces_result,
        "chart": {
            "labels": chart_labels,
            "datasets": chart_datasets
        },
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    }


def predict_single_product_7d(
    product_name: str,
    platform: Optional[str] = None,
    current_price: Optional[float] = None,
    canonical_key: Optional[str] = None,
    min_points: int = 3
) -> Dict[str, Any]:
    """
    Train and predict 7-day linear regression forecast for any single product on the website.
    Uses product's own historical price data recorded in MongoDB.
    """
    migrate_backfill_canonical_keys()

    clean_pname = (product_name or "").strip()
    target_ck = str(canonical_key or compute_canonical_key(clean_pname)).strip().lower()

    clean_plat = (platform or "").strip().lower()
    norm_plat_key = None
    for p in SUPPORTED_MARKETPLACES:
        if p == clean_plat or (p == "gec" and any(x in clean_plat for x in ("gececom", "gec", "gec ecom", "gec e-commerce"))):
            norm_plat_key = p
            break

    points = []
    if norm_plat_key:
        points = fetch_historical_points_for_platform(
            platform=norm_plat_key,
            product_name=clean_pname,
            canonical_key=target_ck
        )

    # Fallback: if specific platform has < min_points, try fetching across platforms for this canonical product
    if len(points) < min_points and target_ck:
        all_pts = []
        for p in SUPPORTED_MARKETPLACES:
            if p == norm_plat_key:
                continue
            all_pts.extend(fetch_historical_points_for_platform(
                platform=p,
                product_name=clean_pname,
                canonical_key=target_ck
            ))
        if all_pts:
            combined = points + all_pts
            by_date = {}
            for pt in combined:
                d_key = pt.get("full_date") or pt["datetime"].strftime("%Y-%m-%d")
                if d_key not in by_date or pt["datetime"] >= by_date[d_key]["datetime"]:
                    by_date[d_key] = pt
            points = sorted(by_date.values(), key=lambda x: x["datetime"])

    pred_res = train_and_predict_7d_linear_regression(
        historical_points=points,
        current_price=current_price
    )

    disp_plat = MARKETPLACE_DISPLAY_NAMES.get(norm_plat_key, platform or "All Marketplaces")
    pred_res["product_name"] = clean_pname
    pred_res["platform"] = disp_plat
    pred_res["platform_name"] = disp_plat
    pred_res["canonical_key"] = target_ck

    # Build single-product Chart.js payload
    color_info = MARKETPLACE_COLORS.get(norm_plat_key, {"primary": "#4f46e5", "bg": "rgba(79, 70, 229, 0.15)"})
    labels = []
    hist_prices = []
    fore_prices = []

    for h in pred_res.get("history", []):
        labels.append(h["date"])
        hist_prices.append(h["price"])
        fore_prices.append(None)

    if pred_res.get("has_prediction") and pred_res.get("daily_forecast"):
        if hist_prices:
            fore_prices[-1] = hist_prices[-1]
        for f in pred_res.get("daily_forecast", []):
            labels.append(f"{f['date']} (+{f['day']}d)")
            hist_prices.append(None)
            fore_prices.append(f["predicted_price"])

    pred_res["chart"] = {
        "labels": labels,
        "datasets": [
            {
                "label": f"Recorded Price History ({disp_plat})",
                "data": hist_prices,
                "borderColor": color_info["primary"],
                "backgroundColor": color_info["bg"],
                "borderWidth": 2.8,
                "pointRadius": 5,
                "pointHoverRadius": 7,
                "pointBackgroundColor": color_info["primary"],
                "tension": 0.2,
                "fill": False,
                "borderDash": [],
                "spanGaps": True
            },
            {
                "label": f"7-Day AI Forecast ({disp_plat})",
                "data": fore_prices,
                "borderColor": color_info["primary"],
                "backgroundColor": color_info["bg"],
                "borderWidth": 2.8,
                "borderDash": [6, 4],
                "pointRadius": 6,
                "pointHoverRadius": 8,
                "pointStyle": "rectRot",
                "pointBackgroundColor": "#ffffff",
                "pointBorderColor": color_info["primary"],
                "pointBorderWidth": 2,
                "tension": 0.1,
                "fill": False,
                "spanGaps": True
            }
        ]
    }
    return pred_res
