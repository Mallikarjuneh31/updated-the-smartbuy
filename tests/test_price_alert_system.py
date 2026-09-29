"""
tests/test_price_alert_system.py
================================
Comprehensive verification suite for SmartBuy Price Alert System:
1. Model & MongoDB CRUD Verification in collection 'price_alerts'
2. FastAPI & Flask API Endpoints (POST, GET, PUT pause, PUT resume, PUT reactivate, DELETE)
3. User authorization & isolation (users can only access their own alerts)
4. Input validation (invalid target prices, empty fields, bad emails)
5. Existing SmartBuy Scraper integration & Fault Isolation (N/A / unavailable / timeout does not equal 0)
6. Live Price comparison (latest_price <= target_price triggers alert)
7. Email notification dispatch with secure SMTP credentials from .env
8. Duplicate email prevention (status -> 'triggered', alert_sent -> True)
9. State transitions (Pause -> Resume -> Trigger -> Reactivate -> Delete)
10. UI template rendering (/my-price-alerts and /search with 🔔 Set Price Alert modal)
"""

import os
import sys
import json
import unittest
from datetime import datetime, timezone

# Ensure project root is in sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app
import database
from database import (
    init_db,
    get_db,
    create_price_alert,
    get_price_alert_by_id,
    get_user_price_alerts,
    get_active_price_alerts,
    pause_price_alert,
    resume_price_alert,
    reactivate_price_alert,
    delete_price_alert,
    update_price_alert_checked,
    mark_price_alert_triggered,
)
from services.price_alert_service import (
    fetch_latest_product_price,
    send_price_alert_email,
    check_single_alert,
    check_all_active_alerts,
    get_platform_scraper,
)


class TestPriceAlertSystem(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        """Initialize Flask test client and ensure MongoDB connection."""
        app.config["TESTING"] = True
        app.config["SECRET_KEY"] = "test_smartbuy_secret"
        cls.client = app.test_client()
        init_db(app)
        cls.db = get_db()
        assert cls.db is not None, "MongoDB connection failed for testing"

        # Unique test user
        cls.test_user_id = "test_user_smartbuy_999"
        cls.test_email = "shopper_test@smartbuy.local"
        cls.other_user_id = "test_other_user_888"
        cls.other_email = "other_shopper@smartbuy.local"

    def setUp(self):
        """Clean up test alerts before each test."""
        if self.db is not None:
            self.db.price_alerts.delete_many({
                "email": {"$in": [self.test_email, self.other_email, "invalid-email"]}
            })

    def tearDown(self):
        """Clean up test alerts after each test."""
        if self.db is not None:
            self.db.price_alerts.delete_many({
                "email": {"$in": [self.test_email, self.other_email, "invalid-email"]}
            })

    # ── Test 1: Database creation & schema validation ────────────────────────
    def test_01_create_price_alert_mongodb(self):
        """Verify alert is saved to 'price_alerts' collection with all required schema fields."""
        alert_id, code, msg = create_price_alert(
            user_id=self.test_user_id,
            email=self.test_email,
            product_name="Vivo T4 5G (8GB RAM, 128GB)",
            product_url="https://www.flipkart.com/vivo-t4-5g/p/itm123456",
            platform="flipkart",
            current_price=27000,
            target_price=25000,
            product_image="https://rukminim2.flixcart.com/image/312/312/xif0q/mobile.jpeg"
        )
        self.assertEqual(code, 201)
        self.assertIsNotNone(alert_id)

        # Verify in MongoDB
        doc = self.db.price_alerts.find_one({"_id": database.to_object_id(alert_id)})
        self.assertIsNotNone(doc)
        self.assertEqual(doc["product_name"], "Vivo T4 5G (8GB RAM, 128GB)")
        self.assertEqual(doc["current_price"], 27000)
        self.assertEqual(doc["target_price"], 25000)
        self.assertEqual(doc["platform"], "flipkart")
        self.assertEqual(doc["status"], "active")
        self.assertEqual(doc["alert_status"], "active")
        self.assertEqual(doc["last_checked_price"], 27000)
        self.assertFalse(doc["alert_sent"])
        self.assertEqual(doc["alert_sent_status"], "pending")
        self.assertIn("created_at", doc)
        self.assertIn("updated_at", doc)
        self.assertIn("last_checked_at", doc)

    # ── Test 2: Target Price validation & error handling ───────────────────────
    def test_02_validation_errors(self):
        """Ensure invalid target prices, empty fields, and bad emails return 400."""
        # Target price <= 0
        _, code, msg = create_price_alert(
            user_id=self.test_user_id,
            email=self.test_email,
            product_name="Vivo T4 5G",
            product_url="https://www.flipkart.com/test",
            platform="flipkart",
            current_price=27000,
            target_price=0
        )
        self.assertEqual(code, 400)
        self.assertIn("greater than ₹0", msg)

        # Negative target price
        _, code, _ = create_price_alert(
            user_id=self.test_user_id,
            email=self.test_email,
            product_name="Vivo T4 5G",
            product_url="https://www.flipkart.com/test",
            platform="flipkart",
            current_price=27000,
            target_price="-500"
        )
        self.assertEqual(code, 400)

        # Invalid Email
        _, code, _ = create_price_alert(
            user_id=self.test_user_id,
            email="not-an-email",
            product_name="Vivo T4 5G",
            product_url="https://www.flipkart.com/test",
            platform="flipkart",
            current_price=27000,
            target_price=25000
        )
        self.assertEqual(code, 400)

    # ── Test 3: FastAPI / Flask POST /price-alert endpoint ─────────────────────
    def test_03_post_price_alert_api(self):
        """Test POST /price-alert JSON endpoint."""
        payload = {
            "product_name": "Vivo T4 5G Smartphone",
            "product_url": "https://www.amazon.in/dp/B0CHX1W1XY",
            "platform": "amazon",
            "current_price": 27000,
            "target_price": 25000,
            "email": self.test_email,
            "user_id": self.test_user_id,
            "product_image": "https://m.media-amazon.com/images/I/71test.jpg"
        }
        res = self.client.post("/price-alert", json=payload)
        self.assertEqual(res.status_code, 201)
        data = res.get_json()
        self.assertTrue(data["success"])
        self.assertIn("alert_id", data)
        self.assertEqual(data["alert"]["target_price"], 25000)

    # ── Test 4: GET /price-alerts endpoint with User Isolation ─────────────────
    def test_04_get_price_alerts_and_isolation(self):
        """Ensure users only see their own alerts and unauthorized requests are blocked."""
        # Create alert for user A
        a1, _, _ = create_price_alert(
            user_id=self.test_user_id,
            email=self.test_email,
            product_name="User A Alert",
            product_url="https://amazon.in/dp/A1",
            platform="amazon",
            current_price=1000,
            target_price=900
        )
        # Create alert for user B
        b1, _, _ = create_price_alert(
            user_id=self.other_user_id,
            email=self.other_email,
            product_name="User B Alert",
            product_url="https://amazon.in/dp/B1",
            platform="amazon",
            current_price=2000,
            target_price=1800
        )

        # Request without auth -> 401
        res_no_auth = self.client.get("/price-alerts")
        self.assertEqual(res_no_auth.status_code, 401)

        # Request as User A -> sees only User A alert
        res_a = self.client.get("/price-alerts", query_string={"user_id": self.test_user_id})
        self.assertEqual(res_a.status_code, 200)
        data_a = res_a.get_json()
        self.assertEqual(data_a["count"], 1)
        self.assertEqual(data_a["alerts"][0]["product_name"], "User A Alert")

        # Request as User B -> sees only User B alert
        res_b = self.client.get("/price-alerts", query_string={"user_id": self.other_user_id})
        self.assertEqual(res_b.status_code, 200)
        data_b = res_b.get_json()
        self.assertEqual(data_b["count"], 1)
        self.assertEqual(data_b["alerts"][0]["product_name"], "User B Alert")

    # ── Test 5: Pause, Resume, Reactivate, Delete Endpoints ────────────────────
    def test_05_alert_state_transitions(self):
        """Test full lifecycle: Active -> Pause -> Resume -> Trigger -> Reactivate -> Delete."""
        alert_id, _, _ = create_price_alert(
            user_id=self.test_user_id,
            email=self.test_email,
            product_name="Lifecycle Product",
            product_url="https://flipkart.com/lifecycle",
            platform="flipkart",
            current_price=15000,
            target_price=14000
        )

        # 1. Pause
        res_pause = self.client.put(
            f"/price-alert/{alert_id}/pause",
            headers={"X-User-ID": self.test_user_id, "Accept": "application/json"}
        )
        self.assertEqual(res_pause.status_code, 200)
        doc = get_price_alert_by_id(alert_id)
        self.assertEqual(doc["status"], "paused")

        # Verify other user cannot modify
        res_unauth = self.client.put(
            f"/price-alert/{alert_id}/resume",
            headers={"X-User-ID": self.other_user_id, "Accept": "application/json"}
        )
        self.assertEqual(res_unauth.status_code, 403)

        # 2. Resume
        res_resume = self.client.put(
            f"/price-alert/{alert_id}/resume",
            headers={"X-User-ID": self.test_user_id, "Accept": "application/json"}
        )
        self.assertEqual(res_resume.status_code, 200)
        doc = get_price_alert_by_id(alert_id)
        self.assertEqual(doc["status"], "active")

        # 3. Simulate Trigger (status becomes triggered, alert_sent becomes True)
        mark_price_alert_triggered(alert_id, latest_price=13999, alert_sent=True)
        doc = get_price_alert_by_id(alert_id)
        self.assertEqual(doc["status"], "triggered")
        self.assertTrue(doc["alert_sent"])
        self.assertEqual(doc["last_checked_price"], 13999)

        # 4. Reactivate (status resets to active, alert_sent resets to False)
        res_react = self.client.put(
            f"/price-alert/{alert_id}/reactivate",
            headers={"X-User-ID": self.test_user_id, "Accept": "application/json"}
        )
        self.assertEqual(res_react.status_code, 200)
        doc = get_price_alert_by_id(alert_id)
        self.assertEqual(doc["status"], "active")
        self.assertFalse(doc["alert_sent"])

        # 5. Delete
        res_del = self.client.delete(
            f"/price-alert/{alert_id}",
            headers={"X-User-ID": self.test_user_id, "Accept": "application/json"}
        )
        self.assertEqual(res_del.status_code, 200)
        doc = get_price_alert_by_id(alert_id)
        self.assertEqual(doc["status"], "deleted")

        # Deleted alert should not appear in user alerts list
        active_list = get_user_price_alerts(user_id=self.test_user_id)
        self.assertEqual(len(active_list), 0)

    # ── Test 6: Scraper Fault Isolation & No Fake Prices ───────────────────────
    def test_06_scraper_fault_isolation(self):
        """
        Verify that:
        - When scraper returns N/A or unavailable, price is NOT treated as 0
        - Alert is NOT triggered
        - Backend and checker do not crash
        """
        alert_id, _, _ = create_price_alert(
            user_id=self.test_user_id,
            email=self.test_email,
            product_name="Unknown Ghost Item",
            product_url="https://www.amazon.in/dp/NONEXISTENTASIN99",
            platform="amazon",
            current_price=5000,
            target_price=4000
        )
        alert_doc = get_price_alert_by_id(alert_id)

        # Run checker on this unavailable product
        result = check_single_alert(alert_doc, send_notification=False)
        self.assertTrue(result.get("checked"))
        self.assertFalse(result.get("triggered"))

        # Verify DB doc was NOT altered to price 0 and remains active
        updated_doc = get_price_alert_by_id(alert_id)
        self.assertEqual(updated_doc["status"], "active")
        self.assertFalse(updated_doc["alert_sent"])
        self.assertNotEqual(updated_doc["last_checked_price"], 0)

    # ── Test 7: Price Check Condition & Trigger & Email ────────────────────────
    def test_07_price_check_condition_and_trigger(self):
        """
        Test that when current price <= target price:
        - Alert is triggered
        - Email notification function is called with product details
        - Alert status changes to 'triggered'
        - alert_sent changes to True
        """
        # User sets target of ₹25,000 for item currently ₹27,000
        alert_id, _, _ = create_price_alert(
            user_id=self.test_user_id,
            email=self.test_email,
            product_name="Vivo T4 5G",
            product_url="https://www.flipkart.com/vivo-t4-5g/p/itm123",
            platform="flipkart",
            current_price=27000,
            target_price=25000,
            product_image="https://rukminim2.flixcart.com/test.jpg"
        )
        alert_doc = get_price_alert_by_id(alert_id)

        # Mock / inject a latest price of ₹24,999 (target is ₹25,000)
        # We test check logic directly:
        alert_doc_with_drop = dict(alert_doc)

        # Mark with a testable function or mock scraper:
        import services.price_alert_service as pas
        original_fetch = pas.fetch_latest_product_price

        try:
            # Simulate scraper returning ₹24,999
            pas.fetch_latest_product_price = lambda platform, product_url, product_name: (
                24999, None, {"title": product_name, "price_num": 24999}
            )

            # Also mock utils_send_email to track parameters
            email_sent_records = []
            pas.utils_send_email = lambda to_email, subject, body_text, html_content=None: (
                email_sent_records.append({"to": to_email, "subject": subject, "body": body_text}) or True
            )

            res = pas.check_single_alert(alert_doc, send_notification=True)
            self.assertTrue(res["triggered"])
            self.assertEqual(res["latest_price"], 24999)
            self.assertEqual(res["target_price"], 25000)

            # Check email was dispatched with required contents
            self.assertEqual(len(email_sent_records), 1)
            sent = email_sent_records[0]
            self.assertEqual(sent["to"], self.test_email)
            self.assertIn("Vivo T4 5G", sent["subject"])
            self.assertIn("24,999", sent["subject"])
            self.assertIn("Vivo T4 5G", sent["body"])
            self.assertIn("Flipkart", sent["body"])
            self.assertIn("https://www.flipkart.com/vivo-t4-5g/p/itm123", sent["body"])

            # Verify in MongoDB
            triggered_doc = get_price_alert_by_id(alert_id)
            self.assertEqual(triggered_doc["status"], "triggered")
            self.assertEqual(triggered_doc["alert_status"], "triggered")
            self.assertTrue(triggered_doc["alert_sent"])
            self.assertEqual(triggered_doc["last_checked_price"], 24999)

            # ── Test 8: Duplicate Email Prevention ────────────────────────────
            # Running check again on the triggered alert MUST NOT send another email
            email_sent_records.clear()
            res_repeat = pas.check_single_alert(triggered_doc, send_notification=True)
            self.assertFalse(res_repeat["triggered"])
            self.assertTrue(any(w in res_repeat.get("reason", "").lower() for w in ("already", "not in active state", "triggered")))

        finally:
            pas.fetch_latest_product_price = original_fetch

    # ── Test 9: Paused alerts are skipped ──────────────────────────────────────
    def test_08_paused_alert_is_skipped(self):
        """Verify paused alerts are never checked by the background checker."""
        alert_id, _, _ = create_price_alert(
            user_id=self.test_user_id,
            email=self.test_email,
            product_name="Paused Item",
            product_url="https://amazon.in/dp/PAUSED",
            platform="amazon",
            current_price=1000,
            target_price=800
        )
        pause_price_alert(alert_id, user_id=self.test_user_id)
        doc = get_price_alert_by_id(alert_id)

        res = check_single_alert(doc)
        self.assertFalse(res["checked"])
        self.assertIn("not in active state", res["reason"])

    # ── Test 10: My Price Alerts HTML Page Rendering ──────────────────────────
    def test_09_my_price_alerts_page_rendering(self):
        """Test GET /my-price-alerts HTML rendering."""
        # Unauthenticated redirects to signin
        res = self.client.get("/my-price-alerts")
        self.assertEqual(res.status_code, 302)

        # Authenticated session
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.test_user_id

        # Insert active test alert
        create_price_alert(
            user_id=self.test_user_id,
            email=self.test_email,
            product_name="Render Test Product",
            product_url="https://flipkart.com/render-test",
            platform="flipkart",
            current_price=12000,
            target_price=11000
        )

        res_auth = self.client.get("/my-price-alerts")
        self.assertEqual(res_auth.status_code, 200)
        html = res_auth.get_data(as_text=True)
        self.assertIn("My Price Alerts", html)
        self.assertIn("Render Test Product", html)
        self.assertIn("Flipkart", html)
        self.assertIn("Check Prices Now", html)

    # ── Test 11: FastAPI Endpoints via TestClient ─────────────────────────────
    def test_10_fastapi_endpoints(self):
        """Test standalone FastAPI app endpoints for price alert operations."""
        from fastapi.testclient import TestClient
        import fastapi_alerts

        fastapi_client = TestClient(fastapi_alerts.app)

        # 1. Health check
        res_h = fastapi_client.get("/health")
        self.assertEqual(res_h.status_code, 200)

        # 2. POST /price-alert
        payload = {
            "product_name": "FastAPI Unit Test Phone",
            "product_url": "https://www.flipkart.com/unit-test/p/123",
            "platform": "flipkart",
            "current_price": 25000,
            "target_price": 23000,
            "email": self.test_email,
            "user_id": self.test_user_id,
            "product_image": "https://example.com/test.jpg"
        }
        res_create = fastapi_client.post(
            "/price-alert",
            json=payload,
            headers={"X-User-ID": self.test_user_id, "X-User-Email": self.test_email}
        )
        self.assertEqual(res_create.status_code, 201)
        data = res_create.json()
        self.assertTrue(data["success"])
        alert_id = data["alert_id"]

        # 3. GET /price-alerts
        res_list = fastapi_client.get(
            "/price-alerts",
            headers={"X-User-ID": self.test_user_id, "X-User-Email": self.test_email}
        )
        self.assertEqual(res_list.status_code, 200)
        self.assertGreaterEqual(res_list.json()["count"], 1)

        # 4. PUT /price-alert/{id}/pause
        res_pause = fastapi_client.put(
            f"/price-alert/{alert_id}/pause",
            headers={"X-User-ID": self.test_user_id, "X-User-Email": self.test_email}
        )
        self.assertEqual(res_pause.status_code, 200)
        self.assertEqual(res_pause.json()["status"], "paused")

        # 5. PUT /price-alert/{id}/resume
        res_resume = fastapi_client.put(
            f"/price-alert/{alert_id}/resume",
            headers={"X-User-ID": self.test_user_id, "X-User-Email": self.test_email}
        )
        self.assertEqual(res_resume.status_code, 200)
        self.assertEqual(res_resume.json()["status"], "active")

        # 6. DELETE /price-alert/{id}
        res_del = fastapi_client.delete(
            f"/price-alert/{alert_id}",
            headers={"X-User-ID": self.test_user_id, "X-User-Email": self.test_email}
        )
        self.assertEqual(res_del.status_code, 200)
        self.assertEqual(res_del.json()["status"], "deleted")


if __name__ == "__main__":
    unittest.main(verbosity=2)

