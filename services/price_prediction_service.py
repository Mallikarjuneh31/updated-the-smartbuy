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
"""

import os
import re
import logging
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


def _get_platform_mongo_pattern(platform: str) -> re.Pattern:
    """Return regex pattern to match platform variants stored in MongoDB."""
    plat = (platform or "").strip().lower()
    if plat in ("gec", "gececom", "gec ecom", "gec e-commerce"):
        return re.compile(r"^gec", re.IGNORECASE)
    return re.compile(rf"^{re.escape(plat)}", re.IGNORECASE)


def record_search_prices_to_history(
    query: str,
    platform_data: Dict[str, Any]
) -> Dict[str, bool]:
    """
    Whenever a user searches, record actual scraped prices from Amazon, Flipkart,
    Meesho, and GEC E-Commerce with timestamps in MongoDB `price_history`.
    """
    if database.db is None:
        init_db()
    if database.db is None:
        return {}

    clean_q = str(query or "").strip()
    status_map = {}

    for plat_key in SUPPORTED_MARKETPLACES:
        item = None
        # Support various shapes passed in from pipeline / comparison / search
        if plat_key in platform_data and isinstance(platform_data[plat_key], dict):
            item = platform_data[plat_key]
        elif plat_key.capitalize() in platform_data and isinstance(platform_data[plat_key.capitalize()], dict):
            item = platform_data[plat_key.capitalize()]
        elif plat_key in platform_data and isinstance(platform_data[plat_key], list) and platform_data[plat_key]:
            item = platform_data[plat_key][0]
        elif plat_key.capitalize() in platform_data and isinstance(platform_data[plat_key.capitalize()], list) and platform_data[plat_key.capitalize()]:
            item = platform_data[plat_key.capitalize()][0]

        if not item or not isinstance(item, dict):
            continue

        raw_price = item.get("price") or item.get("price_num") or item.get("current_price")
        price_num = _parse_numeric_price(raw_price)
        if not price_num or price_num <= 0:
            continue

        title = item.get("title") or item.get("product_name") or clean_q
        url = item.get("product_url") or item.get("link") or item.get("url") or ""
        p_id = item.get("product_id") or item.get("id") or ""

        # Dedup check: Don't flood with identical price within last 5 minutes
        five_mins_ago = datetime.now(timezone.utc) - timedelta(minutes=5)
        existing = database.db.price_history.find_one({
            "platform": _get_platform_mongo_pattern(plat_key),
            "price": price_num,
            "$and": [
                {"$or": [
                    {"product_name": title},
                    {"search_query": clean_q.lower()}
                ]},
                {"$or": [
                    {"recorded_at": {"$gte": five_mins_ago}},
                    {"date": {"$gte": five_mins_ago}}
                ]}
            ]
        })

        if not existing:
            doc_id = record_price_history(
                product_name=title,
                platform=plat_key,
                price=price_num,
                product_url=url,
                product_id=p_id,
                search_query=clean_q
            )
            status_map[plat_key] = bool(doc_id)
        else:
            status_map[plat_key] = True

    return status_map


def fetch_historical_points_for_platform(
    platform: str,
    query: str = "",
    product_name: str = "",
    limit: int = 60
) -> List[Dict[str, Any]]:
    """
    Retrieve real timestamped historical price observations from MongoDB for a marketplace.
    """
    if database.db is None:
        init_db()
    if database.db is None:
        return []

    plat_pattern = _get_platform_mongo_pattern(platform)

    # Build search match criteria
    match_conditions = []
    clean_q = (query or "").strip()
    clean_pname = (product_name or "").strip()

    if clean_q:
        q_words = [w for w in re.split(r"\s+", clean_q) if len(w) > 1]
        if q_words:
            # Word-based flexible matching for product titles
            word_pattern = ".*".join([re.escape(w) for w in q_words])
            match_conditions.append({"product_name": re.compile(word_pattern, re.IGNORECASE)})
            match_conditions.append({"search_query": re.compile(re.escape(clean_q), re.IGNORECASE)})

    if clean_pname and clean_pname != clean_q:
        match_conditions.append({"product_name": re.compile(re.escape(clean_pname), re.IGNORECASE)})

    if not match_conditions:
        return []

    mongo_query: Dict[str, Any] = {
        "platform": plat_pattern,
        "$or": match_conditions
    }

    try:
        docs = list(database.db.price_history.find(mongo_query).sort("recorded_at", 1).limit(limit))
        if not docs:
            # Fallback sort by date
            docs = list(database.db.price_history.find(mongo_query).sort("date", 1).limit(limit))
    except Exception as e:
        logger.error(f"[PricePrediction] Error querying price history for {platform}: {e}")
        return []

    cleaned_points: List[Dict[str, Any]] = []
    seen_timestamps = set()

    for d in docs:
        price = _parse_numeric_price(d.get("price"))
        if not price or price <= 0:
            continue

        raw_dt = d.get("recorded_at") or d.get("date") or d.get("created_at") or d.get("timestamp")
        dt_obj: Optional[datetime] = None

        if isinstance(raw_dt, datetime):
            dt_obj = raw_dt
        elif isinstance(raw_dt, str):
            try:
                dt_obj = datetime.fromisoformat(raw_dt.replace("Z", "+00:00"))
            except Exception:
                for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                    try:
                        dt_obj = datetime.strptime(raw_dt[:19], fmt)
                        break
                    except Exception:
                        pass

        if not dt_obj:
            continue

        if dt_obj.tzinfo is None:
            dt_obj = dt_obj.replace(tzinfo=timezone.utc)

        # Normalize to day-level key to deduplicate identical timestamp spikes
        ts_key = dt_obj.strftime("%Y-%m-%d %H:%M")
        if ts_key in seen_timestamps:
            continue
        seen_timestamps.add(ts_key)

        cleaned_points.append({
            "datetime": dt_obj,
            "date_str": dt_obj.strftime("%b %d"),
            "full_date": dt_obj.strftime("%Y-%m-%d"),
            "price": float(price),
            "product_name": d.get("product_name", "")
        })

    # Sort strictly by chronological order
    cleaned_points.sort(key=lambda p: p["datetime"])
    return cleaned_points


def train_and_predict_7d_linear_regression(
    historical_points: List[Dict[str, Any]],
    current_price: Optional[float] = None
) -> Dict[str, Any]:
    """
    Train Linear Regression model on real historical price points and predict next 7 days.
    If historical points < 2, returns an explicit insufficient data response without fake data.
    """
    now_utc = datetime.now(timezone.utc)
    points_to_train = list(historical_points)

    # If current live price is provided and newer than last point, include it
    if current_price and current_price > 0:
        if not points_to_train or (now_utc - points_to_train[-1]["datetime"]).total_seconds() > 600:
            points_to_train.append({
                "datetime": now_utc,
                "date_str": now_utc.strftime("%b %d"),
                "full_date": now_utc.strftime("%Y-%m-%d"),
                "price": float(current_price),
                "product_name": "Current Live Price"
            })

    # Deduplicate distinct timestamps
    distinct_dates = {p["datetime"].strftime("%Y-%m-%d") for p in points_to_train}

    # Strict check: Need at least 2 distinct temporal data points to fit a linear regression
    if len(points_to_train) < 2 or len(distinct_dates) < 2:
        hist_count = len(points_to_train)
        curr = current_price if current_price else (points_to_train[0]["price"] if points_to_train else None)
        return {
            "sufficient_data": False,
            "historical_count": hist_count,
            "message": (
                f"Insufficient historical price data ({hist_count} observation recorded, minimum 2 required). "
                "SmartBuy logs live prices to MongoDB on every search. "
                "Linear Regression price forecasting will activate automatically once additional price points are logged."
            ),
            "current_price": round(curr, 2) if curr else None,
            "predicted_7d_price": None,
            "price_change_7d": None,
            "price_change_pct": None,
            "trend": "insufficient_data",
            "historical_data": [
                {
                    "date": p["date_str"],
                    "full_date": p["full_date"],
                    "price": p["price"]
                }
                for p in points_to_train
            ],
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

    # Generate 7-day predictions from today (Day 0 to Day +7)
    prediction_series = []
    for day_offset in range(8):  # Day 0 (today) to Day 7
        target_dt = now_utc + timedelta(days=day_offset)
        x_val = (target_dt - t0).total_seconds() / 86400.0
        pred_val = float(reg.predict([[x_val]])[0])

        # Logical boundary: ensure predicted price does not fall below 1 rupee
        pred_val = max(1.0, round(pred_val, 2))

        prediction_series.append({
            "day_offset": day_offset,
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

    # Trend categorization
    if change_pct <= -0.5:
        trend = "falling"
        trend_label = "Price Expected to Drop"
        trend_msg = f"AI Linear Regression projects a price drop of ₹{abs(price_change):,.0f} ({abs(change_pct):.1f}%) over the next 7 days."
    elif change_pct >= 0.5:
        trend = "rising"
        trend_label = "Price Expected to Rise"
        trend_msg = f"AI Linear Regression projects a price rise of ₹{price_change:,.0f} (+{change_pct:.1f}%) over the next 7 days."
    else:
        trend = "stable"
        trend_label = "Price Likely Stable"
        trend_msg = f"AI Linear Regression projects price will remain stable around ₹{effective_current:,.0f} over the next 7 days."

    return {
        "sufficient_data": True,
        "historical_count": len(points_to_train),
        "model": "Linear Regression (Ordinary Least Squares)",
        "daily_rate_rs": round(slope, 2),
        "current_price": round(effective_current, 2),
        "predicted_7d_price": predicted_7d,
        "price_change_7d": price_change,
        "price_change_pct": change_pct,
        "trend": trend,
        "trend_label": trend_label,
        "message": trend_msg,
        "historical_data": [
            {
                "date": p["date_str"],
                "full_date": p["full_date"],
                "price": p["price"]
            }
            for p in points_to_train
        ],
        "prediction_data": prediction_series
    }


def get_marketplace_7d_predictions(
    query: str,
    product_name: str = "",
    current_prices: Optional[Dict[str, float]] = None
) -> Dict[str, Any]:
    """
    Main aggregator: trains and computes 7-day linear regression predictions
    separately for Amazon, Flipkart, Meesho, and GEC E-Commerce.
    """
    clean_q = (query or "").strip()
    clean_pname = (product_name or clean_q).strip()
    current_prices = current_prices or {}

    marketplaces_result = {}
    any_sufficient = False
    best_7d_deal = None
    min_7d_price = float("inf")

    # Collect union of all dates for unified chart X-axis
    all_historical_dates = []
    future_dates = []
    now_utc = datetime.now(timezone.utc)
    for d in range(8):
        future_dates.append((now_utc + timedelta(days=d)).strftime("%b %d"))

    for plat in SUPPORTED_MARKETPLACES:
        curr_p = current_prices.get(plat) or current_prices.get(plat.capitalize())
        points = fetch_historical_points_for_platform(
            platform=plat,
            query=clean_q,
            product_name=clean_pname
        )

        pred_res = train_and_predict_7d_linear_regression(
            historical_points=points,
            current_price=curr_p
        )

        pred_res["platform_name"] = MARKETPLACE_DISPLAY_NAMES[plat]
        pred_res["colors"] = MARKETPLACE_COLORS[plat]
        marketplaces_result[plat] = pred_res

        if pred_res.get("sufficient_data"):
            any_sufficient = True
            p7 = pred_res.get("predicted_7d_price")
            if p7 and p7 < min_7d_price:
                min_7d_price = p7
                best_7d_deal = {
                    "platform": MARKETPLACE_DISPLAY_NAMES[plat],
                    "predicted_price": p7,
                    "change_pct": pred_res.get("price_change_pct"),
                    "trend": pred_res.get("trend")
                }

        for h in pred_res.get("historical_data", []):
            if h["date"] not in all_historical_dates and h["date"] not in future_dates:
                all_historical_dates.append(h["date"])

    # Build Chart.js formatted multi-series datasets
    # X-Axis labels: Historical dates + Future 7-day dates
    chart_labels = all_historical_dates + future_dates

    chart_datasets = []
    for plat in SUPPORTED_MARKETPLACES:
        p_res = marketplaces_result[plat]
        color_info = MARKETPLACE_COLORS[plat]
        plat_name = MARKETPLACE_DISPLAY_NAMES[plat]

        # 1. Historical Actual Prices Dataset (Solid Line)
        hist_dict = {h["date"]: h["price"] for h in p_res.get("historical_data", [])}
        actual_data_points = []
        for lbl in chart_labels:
            if lbl in hist_dict:
                actual_data_points.append(hist_dict[lbl])
            elif lbl == future_dates[0] and p_res.get("current_price"):
                # Connect to Day 0
                actual_data_points.append(p_res.get("current_price"))
            else:
                actual_data_points.append(None)

        # Only add actual dataset if at least 1 point exists
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
                "borderDash": []
            })

        # 2. Predicted Prices Dataset (Dashed Line)
        if p_res.get("sufficient_data"):
            pred_dict = {p["date"]: p["predicted_price"] for p in p_res.get("prediction_data", [])}
            pred_data_points = []
            for lbl in chart_labels:
                if lbl in pred_dict:
                    pred_data_points.append(pred_dict[lbl])
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
                "tension": 0.1
            })

    return {
        "success": True,
        "query": clean_q,
        "product_name": clean_pname,
        "marketplaces": marketplaces_result,
        "has_predictions": any_sufficient,
        "best_7d_deal": best_7d_deal,
        "chart": {
            "labels": chart_labels,
            "datasets": chart_datasets
        },
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    }
