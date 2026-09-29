"""
scrapers/flipkart/url_scraper.py
================================
Flipkart.com Product URL Extractor with 4-Tier Fallback:
1. Configured Flipkart API (Level 1)
2. Fast Direct HTTP Request with JSON-LD / HTML parser (Level 2)
3. Playwright Chromium Browser Scraper (Level 3)
4. Structured Error State (Level 4)
"""

import os
import re
import json
import logging
import urllib.parse
from typing import Optional, Dict, Any, Tuple
import requests
import bs4

from scrapers.flipkart.client import FlipkartClient
from product_normalizer import normalize_canonical_product
from search.url_resolver import is_short_or_share_url, resolve_product_url

logger = logging.getLogger("smartbuy.scrapers.flipkart.url")

FLIPKART_API_URL = os.getenv("FLIPKART_API_URL", "").strip()
FLIPKART_API_KEY = os.getenv("FLIPKART_API_KEY", "").strip()


class FlipkartUrlScraper:
    """Extracts authentic product details directly from a Flipkart.com product URL."""

    def __init__(self, client: Optional[FlipkartClient] = None):
        self.client = client or FlipkartClient()

    def scrape_url(self, url: str) -> Tuple[Optional[Dict[str, Any]], str, Optional[str]]:
        """
        Execute fallback sequence for Flipkart product URL:
        API -> Fast HTTP & JSON-LD -> Playwright -> Fallback.
        Returns: (normalized_product_dict, status_code, error_message)
        """
        clean_url = re.sub(r'[\s\\\'\"]+$', '', url.strip())
        clean_url = re.sub(r'^[\s\\\'\"]+', '', clean_url)
        logger.info(f"[FlipkartUrlScraper] Fetching product from URL: {clean_url}")

        # Resolve short / mobile share URLs (e.g. dl.flipkart.com/s/XXXX, fkrt.it/...) first
        if is_short_or_share_url(clean_url):
            logger.info(f"[FlipkartUrlScraper] Resolving short share URL: {clean_url}")
            resolved = resolve_product_url(clean_url, "flipkart")
            if resolved and resolved != clean_url:
                logger.info(f"[FlipkartUrlScraper] Resolved short URL to: {resolved}")
                clean_url = resolved

        # Extract PID
        pid = None
        parsed = urllib.parse.urlparse(clean_url)
        qs = urllib.parse.parse_qs(parsed.query)
        if 'pid' in qs and qs['pid']:
            pid = qs['pid'][0]
        elif '/p/' in clean_url:
            m = re.search(r'/p/([a-zA-Z0-9]+)', clean_url)
            if m:
                pid = m.group(1)

        # ── Level 1: Configured API ──────────────────────────────────────────
        if FLIPKART_API_URL and FLIPKART_API_KEY:
            try:
                headers = {"Authorization": f"Bearer {FLIPKART_API_KEY}", "Accept": "application/json"}
                params = {"pid": pid, "url": clean_url} if pid else {"url": clean_url}
                resp = requests.get(FLIPKART_API_URL, headers=headers, params=params, timeout=12)
                if resp.status_code == 200:
                    data = resp.json()
                    prod_raw = data.get("product") or data.get("item") or data
                    norm = normalize_canonical_product(prod_raw, "flipkart", clean_url)
                    if norm and norm.get("price_num", 0) > 0:
                        logger.info(f"[FlipkartUrlScraper] Successfully extracted via API: '{norm['title'][:40]}'")
                        return norm, "success", None
            except Exception as e:
                logger.debug(f"[FlipkartUrlScraper] API fallback failed: {e}")

        # ── Level 2: Fast Direct HTTP & JSON-LD Parser ───────────────────────
        try:
            http_headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "en-IN,en-GB;q=0.9,en-US;q=0.8,en;q=0.7",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            }
            resp = requests.get(clean_url, headers=http_headers, timeout=12)
            if resp.status_code == 200 and len(resp.text) > 5000:
                raw_data = self._parse_flipkart_page_html(resp.text, clean_url, pid)
                if raw_data and raw_data.get("price_num", 0) > 0:
                    norm = normalize_canonical_product(raw_data, "flipkart", clean_url)
                    if norm and norm.get("price_num", 0) > 0:
                        logger.info(f"[FlipkartUrlScraper] Successfully extracted via fast HTTP: '{norm['title'][:40]}' ({norm.get('price')})")
                        return norm, "success", None
        except Exception as http_err:
            logger.debug(f"[FlipkartUrlScraper] Fast HTTP fetch skipped or failed: {http_err}")

        # ── Level 3: Playwright Browser Scraper ──────────────────────────────
        def _browser_task(page):
            page.goto(clean_url, wait_until="domcontentloaded", timeout=28000)

            title_text = page.title().lower()
            if "access denied" in title_text or "blocked" in title_text or "challenge" in page.content().lower():
                raise RuntimeError("Flipkart anti-bot challenge detected")

            # Wait for key product elements (supports both modern and legacy classes)
            try:
                page.wait_for_selector(
                    "h1, div.v1zwn20, div[class*='v1zwn20'], div.Nx9bqj, div._30jeq3, script[type='application/ld+json']",
                    timeout=5000
                )
            except Exception:
                pass

            html = page.content()
            return self._parse_flipkart_page_html(html, clean_url, pid)

        browser_res, browser_stat, browser_err = self.client.execute_browser_task(_browser_task)
        if browser_stat == "success" and browser_res:
            norm = normalize_canonical_product(browser_res, "flipkart", clean_url)
            if norm:
                logger.info(f"[FlipkartUrlScraper] Successfully extracted via Playwright: '{norm['title'][:40]}' ({norm.get('price')})")
                return norm, "success", None

        # ── Level 4: Clear Failure ──────────────────────────────────────────
        error_msg = browser_err or "Unable to retrieve product from Flipkart URL"
        return None, "scraper_error", error_msg

    def _parse_flipkart_page_html(self, html: str, url: str, pid: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Parse raw Flipkart product page HTML for title, price, specs, ratings, and image."""
        if not html:
            return None

        soup = bs4.BeautifulSoup(html, "html.parser")

        # 1. Parse JSON-LD structured data first
        json_ld_prod = {}
        for sc in soup.find_all('script', type='application/ld+json'):
            try:
                content = sc.string or sc.get_text()
                if not content:
                    continue
                d = json.loads(content)
                items = d if isinstance(d, list) else [d]
                for it in items:
                    if isinstance(it, dict) and (it.get('@type') == 'Product' or 'offers' in it or 'aggregateRating' in it):
                        json_ld_prod = it
                        break
                if json_ld_prod:
                    break
            except Exception:
                pass

        # 2. Title Extraction
        title = json_ld_prod.get('name') or ""
        if not title:
            title_elem = soup.select_one("h1, span.VU-ZEz, span.B_NuCI, h1._6EBuvT, h1.yhB1nd, h1[class*='v1zwn27']")
            if title_elem:
                title = title_elem.get_text(strip=True)
        if not title:
            og_title = soup.select_one('meta[property="og:title"]')
            if og_title and og_title.get('content'):
                title = og_title['content'].strip()

        # Clean Flipkart SEO suffixes from title
        if title:
            title = re.sub(r'\s*-\s*Price in India.*', '', title, flags=re.I)
            title = re.sub(r'\s*Online at Best Price On Flipkart\.com.*', '', title, flags=re.I)
            title = re.sub(r'\s*Buy\s+.*?\s+Online In India.*', '', title, flags=re.I)
            title = re.sub(r'\s*\|\s*Flipkart\.com.*', '', title, flags=re.I)
            title = title.strip()

        if not title or len(title) < 3:
            return None

        # 3. Price Extraction
        price_num = 0
        # Check JSON-LD offers
        offers = json_ld_prod.get('offers')
        if isinstance(offers, dict) and offers.get('price'):
            try:
                price_num = int(float(offers['price']))
            except Exception:
                pass
        elif isinstance(offers, list) and offers and offers[0].get('price'):
            try:
                price_num = int(float(offers[0]['price']))
            except Exception:
                pass

        # Fallback to DOM price selectors
        if price_num <= 0:
            price_elem = soup.select_one(
                "div.v1zwn20, div[class*='v1zwn20'], div.Nx9bqj.CxhGGd, div.Nx9bqj, "
                "div._30jeq3._16Jk6d, div._30jeq3, div.hl05eU, div.hZ3P6w, div._25b18c"
            )
            if price_elem:
                clean_p = re.sub(r'[^\d.]', '', price_elem.get_text(strip=True))
                if clean_p:
                    try:
                        price_num = int(float(clean_p))
                    except Exception:
                        pass

        # Fallback: scan for Rupee text patterns near product area
        if price_num <= 0:
            for el in soup.find_all(string=re.compile(r'[₹Rs]')):
                txt = str(el).strip()
                m = re.search(r'[₹Rs\.]{1,3}\s*([\d,]+)', txt)
                if m:
                    digits = m.group(1).replace(',', '')
                    try:
                        val = int(digits)
                        if 10 <= val <= 2000000:
                            price_num = val
                            break
                    except Exception:
                        pass

        # 4. MRP Extraction
        mrp_num = price_num
        mrp_elem = soup.select_one(
            "div.v1zwn29, div[class*='v1zwn29'], div.yRaY8j.A68aKn, div.yRaY8j, "
            "div._3I9_wc._2p6cR8, div._3I9_wc, div.y739f8, div.kRYCnD"
        )
        if mrp_elem:
            clean_m = re.sub(r'[^\d.]', '', mrp_elem.get_text(strip=True))
            if clean_m:
                try:
                    mrp_val = int(float(clean_m))
                    if mrp_val >= price_num:
                        mrp_num = mrp_val
                except Exception:
                    pass

        # 5. Image Extraction
        image_url = ""
        json_img = json_ld_prod.get('image')
        if isinstance(json_img, list) and json_img:
            image_url = json_img[0]
        elif isinstance(json_img, str) and json_img:
            image_url = json_img

        if not image_url:
            img_elem = soup.select_one(
                "img.DByuf4, img.UCc1lI, img._396cs4._2amPTt, img._396cs4, img._2r_T1I, img._0DkuPH, img.q6DClP"
            )
            if img_elem:
                image_url = img_elem.get('src') or img_elem.get('data-src') or ""
        if not image_url or "data:" in image_url:
            og_img = soup.select_one('meta[property="og:image"]')
            if og_img and og_img.get('content'):
                image_url = og_img['content']

        # 6. Rating & Review Count
        rating = None
        agg_rating = json_ld_prod.get('aggregateRating')
        if isinstance(agg_rating, dict):
            if agg_rating.get('ratingValue'):
                try:
                    rating = float(agg_rating['ratingValue'])
                except Exception:
                    pass

        if rating is None:
            rate_elem = soup.select_one("div.XQDdHH, div._3LWZlK, div.hGSR34, div.Wphh3Z, div[class*='XQDdHH']")
            if rate_elem:
                m_r = re.search(r'([\d.]+)', rate_elem.get_text())
                if m_r:
                    try:
                        rating = float(m_r.group(1))
                    except Exception:
                        pass

        reviews = 0
        if isinstance(agg_rating, dict):
            if agg_rating.get('reviewCount'):
                try:
                    reviews = int(agg_rating['reviewCount'])
                except Exception:
                    pass
            elif agg_rating.get('ratingCount'):
                try:
                    reviews = int(agg_rating['ratingCount'])
                except Exception:
                    pass

        if reviews == 0:
            rev_elem = soup.select_one("span.Wphh3Z, span._2_R_DZ")
            if rev_elem:
                rev_text = rev_elem.get_text()
                m_rev = re.search(r'([\d,]+)\s*Reviews?', rev_text, re.I)
                if not m_rev:
                    m_rev = re.search(r'([\d,]+)\s*Ratings?', rev_text, re.I)
                if m_rev:
                    try:
                        reviews = int(m_rev.group(1).replace(',', ''))
                    except Exception:
                        pass

        # 7. Brand
        brand = None
        json_brand = json_ld_prod.get('brand')
        if isinstance(json_brand, dict) and json_brand.get('name'):
            brand = json_brand['name']
        elif isinstance(json_brand, str):
            brand = json_brand
        if not brand:
            bc_brand = soup.select_one("div._75nlfW a, a.R0cyWM, div.Fo1I0b, div.syl9yP")
            if bc_brand and len(bc_brand.get_text(strip=True)) < 30:
                brand = bc_brand.get_text(strip=True)

        # 8. Specifications Extraction (Tables + Modern Grid)
        specifications: Dict[str, str] = {}
        for tr in soup.select("table._14cfVK tr, div._3k-BhJ tr, div.row._30-a, div.G6XhRU tr"):
            tds = tr.select("td")
            if len(tds) >= 2:
                k = tds[0].get_text(strip=True)
                v = tds[1].get_text(strip=True)
                if k and v:
                    specifications[k] = v

        for row in soup.select("div.grid-formation-dynamic, div.grid-formation, div.G6XhRU, div.row._30-a"):
            inner = row.find('div')
            if inner:
                ch = inner.find_all(recursive=False)
                parts = [c.get_text(strip=True) for c in ch if c.get_text(strip=True)]
                if len(parts) >= 2:
                    k = parts[0]
                    v = parts[1]
                    if k and v and len(k) < 50 and len(v) < 300:
                        if not any(ign in k.lower() for ign in ['cancellation', 'return', 'delivery', 'warranty details']):
                            specifications[k] = v

        # Key Highlights
        features = []
        for li in soup.select("div._2418kt li, ul._1gHwDe li, ul.G4BRas li"):
            f_text = li.get_text(strip=True)
            if f_text and len(f_text) > 3:
                features.append(f_text)

        # In Stock
        in_stock = True
        if isinstance(offers, dict) and offers.get('availability'):
            avail_str = str(offers['availability']).lower()
            if 'outofstock' in avail_str:
                # If price is present, keep in_stock true for comparison view
                in_stock = True if price_num > 0 else False

        return {
            "platform": "flipkart",
            "title": title,
            "product_name": title,
            "product_id": pid or "",
            "price_num": price_num,
            "mrp_num": mrp_num,
            "image_url": image_url,
            "image": image_url,
            "rating": rating,
            "review_count": reviews,
            "product_url": url,
            "url": url,
            "specifications": specifications,
            "key_features": features,
            "in_stock": in_stock,
            "brand": brand,
            "availability": "In Stock" if in_stock else "Out of Stock",
            "source": "live"
        }
