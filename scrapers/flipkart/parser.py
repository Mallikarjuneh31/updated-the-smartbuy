"""
scrapers/flipkart/parser.py
===========================
Independent DOM & HTML parser for Flipkart search results.
"""

import re
import logging
from typing import Dict, Any, Optional
import bs4

logger = logging.getLogger("smartbuy.scrapers.flipkart.parser")


def parse_flipkart_card(card: bs4.element.Tag) -> Optional[Dict[str, Any]]:
    """Extract product data from a Flipkart card or container."""
    try:
        # Brand extraction (Flipkart displays brand in separate element for bags/fashion/footwear)
        brand_elem = card.select_one('div.Fo1I0b, div.syl9yP, div._2W96wQ, div.yKfJA4, div._2WkVRV, div.G6XhRU, span._2I90We, div._2B_ZTg')
        brand = brand_elem.get_text(strip=True) if brand_elem else ""

        # Title
        title_elem = card.select_one('div.KzDlHZ, a.wjcEIp, a.WKTcLC, div._4rR01T, a.s1Q9rs, div._2W96wQ, a.IRpwTa, div.RG5Slk, div.nZIRY7, a.atJtCj, a.k7wcnx, h1, h2, h3')
        if not title_elem:
            title_elem = card.select_one('a[title]')
        if not title_elem:
            for a_el in card.select('a[href*="/p/"]'):
                t_sub = a_el.get_text(strip=True)
                if t_sub and len(t_sub) >= 4:
                    title_elem = a_el
                    break
        if not title_elem:
            return None

        title = title_elem.get('title') or title_elem.get_text(strip=True)
        if not title or len(title) < 4:
            return None

        if brand and not title.lower().startswith(brand.lower()):
            title = f"{brand} {title}"

        # Price
        price_elem = card.select_one('div.v1zwn20, div[class*="v1zwn20"], div.Nx9bqj, div[class*="Nx9bqj"], div._30jeq3, div.hl05eU, div.hZ3P6w, div._25b18c')
        price_num = 0
        if price_elem:
            cleaned_p = re.sub(r'[^\d.]', '', price_elem.get_text(strip=True))
            try:
                price_num = int(float(cleaned_p))
            except (ValueError, TypeError):
                price_num = 0

        if price_num <= 0:
            for el in card.find_all(string=re.compile(r'[₹Rs]')):
                m = re.search(r'[₹Rs\.]{1,3}\s*([\d,]+)', str(el).strip())
                if m:
                    try:
                        val = int(m.group(1).replace(',', ''))
                        if 10 <= val <= 2000000:
                            price_num = val
                            break
                    except Exception:
                        pass
        if price_num <= 0:
            return None

        # MRP
        mrp_elem = card.select_one('div.v1zwn29, div[class*="v1zwn29"], div.yRaY8j, div._3I9_wc, div.y739f8, div.kRYCnD')
        mrp_str = mrp_elem.get_text(strip=True) if mrp_elem else None
        mrp_num = None
        if mrp_str:
            cleaned_m = re.sub(r'[^\d.]', '', mrp_str)
            try:
                mrp_num = int(float(cleaned_m))
            except Exception:
                pass

        # Discount
        disc_elem = card.select_one('div.UkUFwK span, div._3Ay6Sb span, div.UkUFwK, div.HQe8jr')
        discount = 0
        if disc_elem:
            d_match = re.search(r'(\d+)%', disc_elem.get_text())
            if d_match:
                discount = int(d_match.group(1))
        elif mrp_num and mrp_num > price_num:
            discount = round(((mrp_num - price_num) / mrp_num) * 100)

        # Rating
        rating = None
        rate_elem = card.select_one('div.XQDdHH, div._3LWZlK, div.hGSR34, div.Wphh3Z')
        if rate_elem:
            r_match = re.search(r'([\d.]+)', rate_elem.get_text())
            if r_match:
                try:
                    rating = float(r_match.group(1))
                except Exception:
                    pass

        # Reviews
        reviews = 0
        rev_elem = card.select_one('span.Wphh3Z, span._2_R_DZ, div.a7saXW')
        if rev_elem:
            rv_match = re.search(r'([\d,]+)\s*Reviews?', rev_elem.get_text(), re.IGNORECASE)
            if not rv_match:
                rv_match = re.search(r'([\d,]+)', rev_elem.get_text())
            if rv_match:
                try:
                    reviews = int(rv_match.group(1).replace(',', ''))
                except Exception:
                    pass

        # Image extraction with multi-layer fallback
        image = ""
        # 1. Primary product image selector for Flipkart
        img_elem = card.select_one('img.UCc1lI, img.DByuf4, img._396cs4, img._2r_T1I, img._530RAn, img._0DkuPH, img.q6DClP')
        if img_elem:
            for attr in ('src', 'data-src', 'srcset'):
                val = img_elem.get(attr)
                if val:
                    if attr == 'srcset':
                        val = val.split(',')[0].strip().split(' ')[0]
                    if val.startswith('//'):
                        val = 'https:' + val
                    if not val.startswith('data:') and 'static-assets' not in val:
                        image = val
                        break

        # 2. Search all img tags in card for Flipkart CDN product images
        if not image:
            for img in card.select('img'):
                for attr in ('src', 'data-src', 'srcset'):
                    val = img.get(attr)
                    if not val:
                        continue
                    if attr == 'srcset':
                        val = val.split(',')[0].strip().split(' ')[0]
                    if val.startswith('//'):
                        val = 'https:' + val
                    if ('rukminim' in val or 'flixcart.com/image/' in val) and not val.startswith('data:'):
                        image = val
                        break
                if image:
                    break

        # 3. Fallback to any non-static, non-data img tag
        if not image:
            for img in card.select('img'):
                val = img.get('src') or img.get('data-src') or ''
                if val.startswith('//'):
                    val = 'https:' + val
                if val and not val.startswith('data:') and 'static-assets' not in val and 'fa_9e47c1' not in val:
                    image = val
                    break

        # Product Link
        link_elem = card.select_one('a.CGtC5Q, a._1fQZEK, a.s1Q9rs, a.VJA3rP, a.k7wcnx, a[href*="/p/"]')
        href = link_elem.get('href') if link_elem else ""
        if href.startswith('/'):
            url = f"https://www.flipkart.com{href.split('?')[0]}"
        elif href.startswith('http'):
            url = href.split('?')[0]
        else:
            url = "https://www.flipkart.com"

        # Product ID
        product_id = ""
        m_pid = re.search(r'pid=([A-Z0-9]+)', href)
        if m_pid:
            product_id = m_pid.group(1)
        elif "/p/" in url:
            m_pid2 = re.search(r'/p/([a-zA-Z0-9]+)', url)
            if m_pid2:
                product_id = m_pid2.group(1)
        # Specifications from Flipkart listing bullets
        specs_extracted = {}
        spec_items = card.select('ul._1xgFaf li, li.rgWa7D, ul.G4BRas li, div._21AqiJ')
        warr_val = None
        for s_idx, sp in enumerate(spec_items):
            s_text = sp.get_text(strip=True)
            if s_text:
                if 'ram' in s_text.lower() or 'rom' in s_text.lower() or 'storage' in s_text.lower():
                    specs_extracted['RAM / Storage'] = s_text
                elif 'display' in s_text.lower() or 'inch' in s_text.lower() or 'cm' in s_text.lower():
                    specs_extracted['Display'] = s_text
                elif 'camera' in s_text.lower() or 'mp' in s_text.lower():
                    specs_extracted['Camera'] = s_text
                elif 'battery' in s_text.lower() or 'mah' in s_text.lower():
                    specs_extracted['Battery'] = s_text
                elif 'processor' in s_text.lower():
                    specs_extracted['Processor'] = s_text
                elif 'warranty' in s_text.lower() or 'guarantee' in s_text.lower():
                    warr_val = s_text
                    specs_extracted['Warranty'] = s_text
                else:
                    specs_extracted[f'Feature {s_idx+1}'] = s_text

        return {
            "platform": "flipkart",
            "product_id": product_id,
            "title": title,
            "brand": brand or None,
            "price": f"₹{price_num:,}",
            "price_num": price_num,
            "mrp": f"₹{mrp_num:,}" if mrp_num else f"₹{price_num:,}",
            "mrp_num": mrp_num or price_num,
            "original_price": f"₹{mrp_num:,}" if mrp_num else f"₹{price_num:,}",
            "discount": discount,
            "discount_percent": discount,
            "rating": rating,
            "review_count": reviews,
            "reviews": f"{reviews:,}" if reviews > 0 else "0",
            "image": image,
            "image_url": image,
            "url": url,
            "product_url": url,
            "link": url,
            "availability": "In Stock",
            "in_stock": True,
            "warranty": warr_val,
            "specifications": specs_extracted,
            "source": "flipkart"
        }
    except Exception as e:
        logger.debug(f"[FlipkartParser] Error parsing card: {e}")
        return None
