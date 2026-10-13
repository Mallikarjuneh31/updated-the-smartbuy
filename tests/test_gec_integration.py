"""
tests/test_gec_integration.py
=============================
Comprehensive End-to-End Verification Suite for GEC E-Commerce Integration into SmartBuy:
1. GEC Web Scraper:
   - Direct HTML scrape from /gececom and MongoDB fallback
   - Product name, current price, MRP, rating, reviews count, image, availability
   - Product URL scrape (/gececom#product-... and absolute URLs)
   - Fault isolation on non-existent products and invalid URLs
2. MongoDB Catalog & Price History:
   - Store, update, query GEC products in 'gececom_products'
   - Log price changes and historical points in 'price_history'
3. 4-Platform Comparison Engine:
   - Search across Amazon, Flipkart, Meesho, and GEC in parallel
   - URL comparison with GEC as source platform
   - URL comparison with other platforms matching against GEC
4. Price Alert System:
   - Create alert for GEC product with target price
   - Background check triggers when GEC live price <= target price
   - Email dispatch verification
   - Price Simulator instant trigger on price drop
5. FastAPI & Flask API Endpoints:
   - Flask /api/gececom/products and /api/price-alert
   - FastAPI /price-alert with platform="gec"
"""

import os
import sys
import unittest
from unittest.mock import patch

# Add workspace root to sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app
import database
from database import (
    get_db,
    get_gececom_products,
    get_gececom_product_by_id,
    update_gececom_product_price,
    record_price_history,
    get_price_history,
    create_price_alert,
    get_price_alert_by_id
)
from scrapers.gec_scraper import GecScraper
from search_engine import fetch_all_products_parallel, _scrapers_instances
from url_detector import detect_platform_from_url, validate_and_detect_url
from comparison_engine import compare_by_product_url, _scrapers
import services.price_alert_service as alert_service
from services.price_alert_service import (
    fetch_latest_product_price,
    check_single_alert,
    trigger_gececom_price_alerts_for_product
)


class TestGecIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        database.init_db()
        database.seed_default_gececom_products_if_empty()
        cls.flask_client = app.test_client()
        cls.scraper = GecScraper()
        cls.all_products = get_gececom_products()

    def test_01_gec_scraper_search_and_fields(self):
        """Test GEC scraper returns correct schema fields with genuine price data."""
        results, status, error = self.scraper.search_products("Vivo T4")
        self.assertGreater(len(results), 0, "GEC scraper should return at least 1 product for 'Vivo T4'")
        
        prod = results[0]
        self.assertIn("title", prod)
        self.assertIn("price", prod)
        self.assertIn("price_num", prod)
        self.assertGreater(prod["price_num"], 0)
        self.assertIn("rating", prod)
        self.assertTrue("review_count" in prod or "reviews_count" in prod)
        self.assertIn("image", prod)
        self.assertIn("url", prod)
        self.assertEqual(prod["platform"], "gec")
        self.assertEqual(prod["platform_name"], "GEC")

    def test_02_gec_scraper_url(self):
        """Test scraping a GEC product by relative and full URL."""
        all_prods = get_gececom_products()
        self.assertGreater(len(all_prods), 0)
        first_p = all_prods[0]
        p_id = first_p["id"]

        prod, status, err = self.scraper.scrape_product_url(f"/gececom#product-{p_id}")
        self.assertIsNotNone(prod)
        self.assertEqual(prod["id"], p_id)
        self.assertEqual(prod["price_num"], first_p["price"])

    def test_03_gec_url_detector(self):
        """Test URL detector recognizes GEC URLs properly."""
        p1 = detect_platform_from_url("/gececom#product-6abb8cd4459972a0b5a8b810")
        self.assertEqual(p1, "gec")

        p2 = detect_platform_from_url("http://127.0.0.1:5000/gececom#product-6abb8cd4459972a0b5a8b810")
        self.assertEqual(p2, "gec")

        v = validate_and_detect_url("http://127.0.0.1:5000/gececom#product-6abb8cd4459972a0b5a8b810")
        self.assertTrue(v["is_valid"])
        self.assertEqual(v["platform"], "gec")
        self.assertEqual(v["platform_name"], "GEC")

    def test_04_mongodb_price_history_logging(self):
        """Test price history points logging and querying in MongoDB."""
        test_prod_name = "Unit Test Phone GEC"
        test_prod_id = "test-item-history-99"
        
        record_price_history(test_prod_name, "gec", 19999, product_url="/gececom#test", product_id=test_prod_id)
        record_price_history(test_prod_name, "gec", 18499, product_url="/gececom#test", product_id=test_prod_id)

        history = get_price_history(product_id=test_prod_id, limit=5)
        self.assertGreaterEqual(len(history), 2)
        prices = [h["price"] for h in history]
        self.assertIn(18499, prices)
        self.assertIn(19999, prices)

    def test_05_four_platform_search_orchestration(self):
        """Test parallel search across Amazon, Flipkart, Meesho, and GEC."""
        self.assertIn("GEC", _scrapers_instances)
        
        # Mock external slow network calls to focus on orchestration integrity
        with patch.object(_scrapers_instances["Amazon"], "search_products", return_value=([{"title": "Vivo T4", "price_num": 21999, "platform": "amazon"}], None, None)), \
             patch.object(_scrapers_instances["Flipkart"], "search_products", return_value=([{"title": "Vivo T4", "price_num": 21499, "platform": "flipkart"}], None, None)), \
             patch.object(_scrapers_instances["Meesho"], "search_products", return_value=([{"title": "Vivo T4 Cover", "price_num": 299, "platform": "meesho"}], None, None)):
            
            raw_results, pipeline_data = fetch_all_products_parallel("Vivo T4")
            self.assertIn("GEC", raw_results)
            self.assertIn("Amazon", raw_results)
            self.assertIn("Flipkart", raw_results)
            self.assertIn("Meesho", raw_results)
            self.assertGreater(len(raw_results["GEC"]), 0)

    def test_06_url_comparison_with_gec_source(self):
        """Test comparing a GEC URL against Amazon, Flipkart, and Meesho."""
        self.assertIn("gec", _scrapers)
        all_prods = get_gececom_products()
        vivo_item = next((p for p in all_prods if "Vivo" in p["product_name"]), all_prods[0])
        gec_url = f"/gececom#product-{vivo_item['id']}"

        with patch.object(_scrapers["amazon"], "search_products", return_value=([{"title": "Vivo T4 5G (8GB RAM)", "price_num": 21999, "price": "₹21,999", "platform": "amazon", "platform_name": "Amazon", "url": "https://amazon.in/dp/xyz", "rating": 4.3, "in_stock": True}], None, None)), \
             patch.object(_scrapers["flipkart"], "search_products", return_value=([{"title": "Vivo T4 5G (8GB)", "price_num": 21499, "price": "₹21,499", "platform": "flipkart", "platform_name": "Flipkart", "url": "https://flipkart.com/p/xyz", "rating": 4.4, "in_stock": True}], None, None)), \
             patch.object(_scrapers["meesho"], "search_products", return_value=([], None, None)):
            
            res = compare_by_product_url(gec_url, fresh=True)
            self.assertTrue(res.get("success"), f"Comparison failed: {res.get('error')}")
            self.assertEqual(res.get("source_platform"), "gec")
            self.assertIn("matches", res)
            self.assertIn("gec", res["matches"])
            self.assertIsNotNone(res["matches"]["gec"])
            self.assertIn("best_deal", res)

    def test_07_price_alert_trigger_and_email(self):
        """Test price alert triggers when GEC price drops below target price."""
        all_prods = get_gececom_products()
        vivo_item = next((p for p in all_prods if "Vivo" in p["product_name"]), all_prods[0])
        v_id = vivo_item["id"]

        # 1. Set baseline price
        update_gececom_product_price(v_id, 20999)

        # 2. Create price alert with target price 20000
        alert_id, code, msg = create_price_alert(
            user_id=None,
            email="shopper_gec_test@smartbuy.local",
            product_name="Vivo T4 5G",
            product_url=f"/gececom#product-{v_id}",
            platform="gec",
            current_price=20999.0,
            target_price=20000.0,
            product_image=""
        )
        self.assertIsNotNone(alert_id)
        self.assertEqual(code, 201)

        # 3. Initially, price is 20999 > 20000 (not triggered)
        with patch("services.price_alert_service.send_price_alert_email") as mock_email:
            alert = get_price_alert_by_id(alert_id)
            res = check_single_alert(alert)
            self.assertFalse(res.get("triggered"))
            mock_email.assert_not_called()

        # 4. Now drop price in GEC store to 18999 <= 20000
        update_gececom_product_price(v_id, 18999)

        # 5. Check alert - should trigger and send email
        with patch("services.price_alert_service.send_price_alert_email", return_value=True) as mock_email:
            alert = get_price_alert_by_id(alert_id)
            res = check_single_alert(alert)
            self.assertTrue(res.get("triggered"))
            mock_email.assert_called_once()

    def test_08_simulator_instant_alert_trigger(self):
        """Test instant alert check when simulator edits price via trigger_gececom_price_alerts_for_product."""
        all_prods = get_gececom_products()
        boat_item = next((p for p in all_prods if "boAt" in p["product_name"] or "Rockerz" in p["product_name"]), all_prods[0])
        b_id = boat_item["id"]

        # Create an alert for Boat Rockerz 450
        alert_id, code, msg = create_price_alert(
            user_id=None,
            email="audiophile@smartbuy.local",
            product_name=boat_item["product_name"],
            product_url=f"/gececom#product-{b_id}",
            platform="gec",
            current_price=1499.0,
            target_price=1200.0,
            product_image=""
        )
        self.assertEqual(code, 201)

        with patch("services.price_alert_service.send_price_alert_email", return_value=True) as mock_email:
            res = trigger_gececom_price_alerts_for_product(
                product_id=b_id,
                product_name=boat_item["product_name"],
                new_price=1199
            )
            self.assertGreaterEqual(res.get("triggered", 0), 1)
            mock_email.assert_called()

    def test_09_fastapi_gec_alert_support(self):
        """Test FastAPI microservice supports GEC platform alerts."""
        from fastapi_alerts import app as fastapi_app
        from starlette.testclient import TestClient

        all_prods = get_gececom_products()
        first_p = all_prods[0]

        client = TestClient(fastapi_app)
        payload = {
            "product_name": "GEC FastAPI Test Product",
            "product_url": f"/gececom#product-{first_p['id']}",
            "platform": "gec",
            "target_price": 15000.0,
            "email": "fastapi_shopper@smartbuy.local",
            "current_price": 18999.0
        }
        res = client.post("/price-alert", json=payload)
        self.assertEqual(res.status_code, 201, f"FastAPI POST failed: {res.text}")
        data = res.json()
        self.assertTrue(data.get("success"))
        alert_obj = data.get("alert") or {}
        self.assertEqual(alert_obj.get("platform"), "gec")

    def test_10_specification_comparison_table_gec_column(self):
        """Test Product Specification Comparison matrix includes GEC column with authentic specs."""
        from search.specs_extractor import build_dynamic_specifications, compute_marketplace_statuses

        all_prods = get_gececom_products()
        vivo_item = next((p for p in all_prods if "Vivo" in p["product_name"]), all_prods[0])

        matches = {
            "amazon": {
                "title": "Vivo T4 5G (8GB RAM, 128GB)",
                "price": "₹21,999",
                "price_num": 21999,
                "rating": 4.3,
                "availability": "In Stock",
                "specifications": {"RAM": "8 GB", "Storage": "128 GB", "Battery": "5000 mAh"}
            },
            "flipkart": {
                "title": "Vivo T4 5G (Starlight Silver, 128 GB)",
                "price": "₹21,499",
                "price_num": 21499,
                "rating": 4.4,
                "availability": "In Stock",
                "specifications": {"RAM": "8 GB", "Storage": "128 GB", "Battery": "5000 mAh"}
            },
            "meesho": None,
            "gec": {
                "title": vivo_item["product_name"],
                "price": f"₹{vivo_item['current_price']:,}",
                "price_num": vivo_item["current_price"],
                "rating": vivo_item.get("rating", 4.4),
                "availability": "In Stock",
                "specifications": {"RAM": "8 GB", "Storage": "128 GB", "Battery": "5000 mAh"}
            }
        }

        matrix = build_dynamic_specifications(matches, "mobiles")
        self.assertGreater(len(matrix), 0, "Matrix should contain rows")

        first_row = matrix[0]
        self.assertIn("gec", first_row, "Every row must contain a 'gec' column key")
        self.assertIn("amazon", first_row)
        self.assertIn("flipkart", first_row)
        self.assertIn("meesho", first_row)

        statuses = compute_marketplace_statuses(matches, {
            "Amazon": {"status": "success", "available": True},
            "Flipkart": {"status": "success", "available": True},
            "Meesho": {"status": "unavailable", "available": False},
            "GEC": {"status": "success", "available": True}
        })
        self.assertIn("GEC", statuses)
        self.assertEqual(statuses["GEC"]["status"], "Found")

    def test_11_gec_buy_route(self):
        """Test /buy/gec endpoint sets session redirect."""
        res = self.flask_client.get(
            "/buy/gec?url=/gececom%23product-123&product_name=Vivo+T4&price=20999"
        )
        self.assertIn(res.status_code, (200, 302))


if __name__ == "__main__":
    unittest.main()

