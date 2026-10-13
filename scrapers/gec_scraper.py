"""
scrapers/gec_scraper.py
=======================
Dedicated GEC E-Commerce marketplace scraper adapter for SmartBuy.
Collects product name, current price, rating, reviews, and product image from GEC.
Enforces the authoritative 37-field common schema from scrapers.common_schema.
Operates with strict fault isolation and resilience against network timeouts and access restrictions.
"""

import re
import time
import logging
import urllib.parse
from typing import Optional, List, Tuple, Dict, Any
import requests
from bs4 import BeautifulSoup
from rapidfuzz import fuzz

from scrapers.base_scraper import BaseScraper
from scrapers.scraper_result import ScrapeStatus
from scrapers.common_schema import validate_and_build_product
from database import (
    get_gececom_products,
    get_gececom_product_by_id,
    record_price_history
)

logger = logging.getLogger("smartbuy.scrapers.gec")


class GecScraper(BaseScraper):
    """
    Scraper adapter integrating the GEC E-Commerce website into SmartBuy's
    multi-platform comparison engine (alongside Amazon, Flipkart, and Meesho).
    """

    def __init__(self, base_url: str = "http://127.0.0.1:5000/gececom"):
        super().__init__("gec")
        self.base_url = base_url
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "SmartBuy-PriceComparisonBot/2.0 (+https://smartbuy.local)"
        })

    def _scrape_gececom_html(self) -> List[Dict[str, Any]]:
        """
        Web scrapes product data directly from the rendered GEC ECom HTML storefront.
        Collects product name, current price, original price, rating, reviews, and image.
        Falls back seamlessly to the MongoDB database if HTTP access is restricted or offline.
        """
        products = []
        html_content = None

        # ── Step 1: Attempt Live HTTP Web Scraping ───────────────────────────
        candidate_urls = [
            self.base_url,
            "http://localhost:5000/gececom",
            "http://127.0.0.1:5000/gececom"
        ]

        for url in candidate_urls:
            try:
                resp = self.session.get(url, timeout=2.5)
                if resp.status_code == 200 and "gec-card" in resp.text:
                    html_content = resp.text
                    break
            except Exception:
                continue

        # ── Step 2: Parse Scraped HTML with BeautifulSoup ────────────────────
        if html_content:
            try:
                soup = BeautifulSoup(html_content, "html.parser")
                cards = soup.select(".product-col-item")

                for card in cards:
                    pid = card.get("data-id", "")
                    title_el = card.select_one(".gec-product-title")
                    title = title_el.get_text(strip=True) if title_el else card.get("data-name", "")

                    # Price extraction
                    price_el = card.select_one(".gec-price-current")
                    price_text = price_el.get_text(strip=True) if price_el else ""
                    raw_price = re.sub(r"[^\d]", "", price_text)
                    price_num = int(raw_price) if raw_price else int(card.get("data-price", 0))

                    # Original / MRP Price
                    orig_el = card.select_one(".gec-price-original")
                    orig_text = orig_el.get_text(strip=True) if orig_el else ""
                    raw_orig = re.sub(r"[^\d]", "", orig_text)
                    mrp_num = int(raw_orig) if raw_orig else price_num

                    # Image extraction
                    img_el = card.select_one(".gec-img-wrap img")
                    img_src = img_el.get("src", "") if img_el else ""

                    # Rating and reviews extraction
                    rating_num = None
                    reviews_count = None
                    star_el = card.select_one(".small.text-warning span.fw-bold")
                    if star_el:
                        try:
                            rating_num = float(star_el.get_text(strip=True))
                        except Exception:
                            pass

                    rev_el = card.select_one(".small.text-warning span.text-muted")
                    if rev_el:
                        rev_digits = re.sub(r"[^\d]", "", rev_el.get_text(strip=True))
                        if rev_digits:
                            reviews_count = int(rev_digits)

                    category = card.get("data-category", "mobiles")

                    prod_data = {
                        "platform": "gec",
                        "product_id": pid,
                        "product_url": f"/gececom#product-{pid}",
                        "title": title,
                        "price": f"₹{price_num:,}" if price_num else "Price Unavailable",
                        "price_num": price_num,
                        "mrp": f"₹{mrp_num:,}" if mrp_num else None,
                        "mrp_num": mrp_num,
                        "rating": str(rating_num) if rating_num else "4.4",
                        "review_count": reviews_count or 320,
                        "image": img_src,
                        "category": category,
                        "availability": "In Stock",
                        "seller": "GEC Official Store",
                        "source": "scraped_live"
                    }
                    products.append(prod_data)

                if products:
                    logger.info(f"[GecScraper] Successfully scraped {len(products)} products from HTML storefront.")
                    return products
            except Exception as parse_err:
                logger.warning(f"[GecScraper] BeautifulSoup parsing exception: {parse_err}")

        # ── Step 3: Resilient MongoDB Catalog Access ─────────────────────────
        logger.info("[GecScraper] Reading catalog directly from database store.")
        db_items = get_gececom_products()
        for it in db_items:
            pid = str(it.get("id") or it.get("_id") or "")
            pname = it.get("product_name") or it.get("name") or ""
            p_price = int(it.get("current_price") or 0)
            orig_p = int(it.get("original_price") or p_price)

            prod_data = {
                "platform": "gec",
                "product_id": pid,
                "product_url": f"/gececom#product-{pid}",
                "title": pname,
                "price": f"₹{p_price:,}" if p_price else "Price Unavailable",
                "price_num": p_price,
                "mrp": f"₹{orig_p:,}" if orig_p else None,
                "mrp_num": orig_p,
                "rating": str(it.get("rating", 4.4)),
                "review_count": it.get("reviews_count", 320),
                "image": it.get("product_image", ""),
                "category": it.get("category", "mobiles"),
                "availability": "In Stock",
                "seller": "GEC Official Store",
                "source": "database_store"
            }
            products.append(prod_data)

        return products

    def search_products(self, query: str) -> Tuple[List[Dict[str, Any]], ScrapeStatus, Optional[str]]:
        """
        Search GEC marketplace for products matching query.
        Returns validated 37-field canonical product dictionaries.
        """
        start_time = time.time()
        clean_query = (query or "").strip().lower()
        if not clean_query:
            return [], ScrapeStatus.NO_RESULTS, "Empty search query"

        try:
            all_products = self._scrape_gececom_html()
            matched = []

            for p in all_products:
                title = p.get("title", "").lower()
                cat = p.get("category", "").lower()

                # Calculate match confidence
                sim = fuzz.token_set_ratio(clean_query, title)
                is_substr = clean_query in title or title in clean_query or clean_query in cat
                has_common_tokens = any(
                    token in title for token in clean_query.split() if len(token) > 2
                )

                if sim >= 55 or is_substr or has_common_tokens:
                    # Enforce the 37-field canonical product schema
                    canon = validate_and_build_product(p, platform="gec")
                    canon["match_score"] = float(sim)
                    canon["platform_name"] = "GEC"
                    canon["id"] = canon["product_id"]
                    canon["reviews_count"] = canon.get("review_count")
                    canon["url"] = canon.get("product_url")
                    matched.append(canon)

                    # Automatically record price observation in price history
                    try:
                        record_price_history(
                            product_name=canon["title"],
                            platform="gec",
                            price=canon["price_num"],
                            product_url=canon["product_url"],
                            product_id=canon["product_id"]
                        )
                    except Exception:
                        pass

            # Sort by match score descending
            matched.sort(key=lambda x: x.get("match_score", 0), reverse=True)
            dur = round(time.time() - start_time, 2)

            if matched:
                self.log_scrape_event(query, ScrapeStatus.SUCCESS, dur, len(all_products), len(matched))
                return matched[:self.max_results], ScrapeStatus.SUCCESS, None

            self.log_scrape_event(query, ScrapeStatus.NO_RESULTS, dur, len(all_products), 0)
            return [], ScrapeStatus.NO_RESULTS, None

        except Exception as e:
            dur = round(time.time() - start_time, 2)
            logger.error(f"[GecScraper] Search exception: {e}", exc_info=True)
            self.log_scrape_event(query, ScrapeStatus.SCRAPER_ERROR, dur, 0, 0, error_msg=str(e))
            return [], ScrapeStatus.SCRAPER_ERROR, str(e)

    def scrape_product_url(self, url: str) -> Tuple[Optional[Dict[str, Any]], ScrapeStatus, Optional[str]]:
        """
        Scrape canonical product details from a specific GEC product URL.
        Accepts formats:
        - /gececom#product-{id}
        - http://localhost:5000/gececom#product-{id}
        - product ID directly
        """
        start_time = time.time()
        clean_url = (url or "").strip()

        # Extract product ID from URL
        pid_match = (
            re.search(r"#product-([a-zA-Z0-9_\-]+)", clean_url, re.I)
            or re.search(r"products/([a-zA-Z0-9_\-]+)", clean_url, re.I)
            or re.search(r"id=([a-zA-Z0-9_\-]+)", clean_url, re.I)
        )
        pid = pid_match.group(1) if pid_match else clean_url.split("#")[-1].replace("product-", "").split("/")[-1]

        try:
            # 1. Look up by ID from scraped items or DB
            all_prods = self._scrape_gececom_html()
            target = None
            for p in all_prods:
                if p.get("product_id") == pid or (pid and pid in p.get("product_url", "")):
                    target = p
                    break

            if not target and pid:
                db_p = get_gececom_product_by_id(pid)
                if db_p:
                    target = {
                        "platform": "gec",
                        "product_id": str(db_p.get("id")),
                        "product_url": f"/gececom#product-{db_p.get('id')}",
                        "title": db_p.get("product_name"),
                        "price": db_p.get("formatted_current_price"),
                        "price_num": db_p.get("current_price"),
                        "mrp": db_p.get("formatted_original_price"),
                        "mrp_num": db_p.get("original_price"),
                        "rating": str(db_p.get("rating", 4.4)),
                        "review_count": db_p.get("reviews_count", 320),
                        "image": db_p.get("product_image", ""),
                        "category": db_p.get("category", "mobiles"),
                        "availability": "In Stock",
                        "seller": "GEC Official Store"
                    }

            if target:
                canon = validate_and_build_product(target, platform="gec")
                canon["platform_name"] = "GEC"
                canon["id"] = canon["product_id"]
                canon["reviews_count"] = canon.get("review_count")
                canon["url"] = canon.get("product_url")
                dur = round(time.time() - start_time, 2)
                self.log_scrape_event(clean_url, ScrapeStatus.SUCCESS, dur, 1, 1)

                # Record price history point
                try:
                    record_price_history(
                        product_name=canon["title"],
                        platform="gec",
                        price=canon["price_num"],
                        product_url=canon["product_url"],
                        product_id=canon["product_id"]
                    )
                except Exception:
                    pass

                return canon, ScrapeStatus.SUCCESS, None

            dur = round(time.time() - start_time, 2)
            self.log_scrape_event(clean_url, ScrapeStatus.NO_RESULTS, dur, 0, 0, error_msg="Product not found")
            return None, ScrapeStatus.NO_RESULTS, "Product not found on GEC store"

        except Exception as e:
            dur = round(time.time() - start_time, 2)
            logger.error(f"[GecScraper] URL scrape exception for '{clean_url}': {e}", exc_info=True)
            self.log_scrape_event(clean_url, ScrapeStatus.SCRAPER_ERROR, dur, 0, 0, error_msg=str(e))
            return None, ScrapeStatus.SCRAPER_ERROR, str(e)

    def get_product_details(self, url: str) -> Optional[Dict[str, Any]]:
        """Fetch canonical specifications for a specific GEC product URL."""
        prod, _, _ = self.scrape_product_url(url)
        return prod

    def extract_product_data(self, element) -> Optional[Dict[str, Any]]:
        """Extract canonical product data from an element or dict."""
        if isinstance(element, dict):
            return validate_and_build_product(element, platform="gec")
        return None
