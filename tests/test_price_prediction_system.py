"""
tests/test_price_prediction_system.py
=====================================
Comprehensive verification suite for the 7-day AI Price Prediction System:
1. Linear Regression mathematical fitting and 7-day forecast accuracy.
2. Ground truth integrity: Insufficient data handling without dummy/fake numbers.
3. MongoDB storage with timestamps for Amazon, Flipkart, Meesho, and GEC E-Commerce.
4. FastAPI microservice endpoints (/price-prediction, /price-prediction/record, /health).
5. Flask API mirror endpoints (/price-prediction, /api/price-prediction).
6. Frontend HTML rendering with Chart.js canvas, marketplace stat cards, and X/Y axis bindings.
7. Zero regression on product comparison matrix and price alert workflows.
"""

import os
import sys
import json
import unittest
from datetime import datetime, timezone, timedelta

# Ensure project root in sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app
import database
from database import init_db, get_db, record_price_history
from services.price_prediction_service import (
    train_and_predict_7d_linear_regression,
    fetch_historical_points_for_platform,
    get_marketplace_7d_predictions,
    record_search_prices_to_history,
    SUPPORTED_MARKETPLACES
)


class TestPricePredictionSystem(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        app.config["TESTING"] = True
        app.config["SECRET_KEY"] = "test_smartbuy_prediction_secret"
        cls.client = app.test_client()
        init_db(app)
        cls.db = get_db()

    def setUp(self):
        self.test_query = f"TestPhone_{int(datetime.now().timestamp())}"

    # ── Test 1: Linear Regression Mathematics (Sufficient Historical Data) ──
    def test_01_linear_regression_sufficient_data(self):
        """Verify Linear Regression accurately calculates slope, 7-day predictions, and trend."""
        now = datetime.now(timezone.utc)
        # Create 5 historical points with dropping price (-100 Rs/day)
        points = [
            {"datetime": now - timedelta(days=4), "date_str": "Oct 09", "full_date": "2026-10-09", "price": 25000.0},
            {"datetime": now - timedelta(days=3), "date_str": "Oct 10", "full_date": "2026-10-10", "price": 24900.0},
            {"datetime": now - timedelta(days=2), "date_str": "Oct 11", "full_date": "2026-10-11", "price": 24800.0},
            {"datetime": now - timedelta(days=1), "date_str": "Oct 12", "full_date": "2026-10-12", "price": 24700.0},
            {"datetime": now, "date_str": "Oct 13", "full_date": "2026-10-13", "price": 24600.0},
        ]

        result = train_and_predict_7d_linear_regression(points, current_price=24600.0)

        self.assertTrue(result["sufficient_data"])
        self.assertEqual(result["historical_count"], 5)
        self.assertEqual(result["trend"], "falling")
        self.assertLess(result["price_change_7d"], 0)
        self.assertLess(result["price_change_pct"], 0)
        self.assertEqual(len(result["prediction_data"]), 8)  # Day 0 to Day 7

        # Day +7 predicted price should be ~23900 (24600 - 700)
        day_7_pred = result["prediction_data"][7]["predicted_price"]
        self.assertAlmostEqual(day_7_pred, 23900.0, delta=20.0)
        self.assertEqual(result["predicted_7d_price"], day_7_pred)
        self.assertIn("projects a price drop", result["message"])

    # ── Test 2: Insufficient Data Ground Truth (Zero Dummy Predictions) ──
    def test_02_linear_regression_insufficient_data(self):
        """Verify insufficient data (< 2 points) returns clear explanation with no fake predictions."""
        now = datetime.now(timezone.utc)
        single_point = [
            {"datetime": now, "date_str": "Oct 13", "full_date": "2026-10-13", "price": 19999.0}
        ]

        result = train_and_predict_7d_linear_regression(single_point, current_price=19999.0)

        self.assertFalse(result["sufficient_data"])
        self.assertIsNone(result["predicted_7d_price"])
        self.assertIsNone(result["price_change_7d"])
        self.assertEqual(result["trend"], "insufficient_data")
        self.assertEqual(len(result["prediction_data"]), 0)
        self.assertIn("Insufficient historical price data", result["message"])

    # ── Test 3: Empty Data Handling ──
    def test_03_linear_regression_empty_data(self):
        """Verify empty data points list handled gracefully."""
        result = train_and_predict_7d_linear_regression([], current_price=None)
        self.assertFalse(result["sufficient_data"])
        self.assertEqual(result["historical_count"], 0)
        self.assertIsNone(result["current_price"])

    # ── Test 4: MongoDB Recording on Product Search ──
    def test_04_mongodb_price_recording_4_marketplaces(self):
        """Verify prices from all 4 marketplaces are saved with timestamps in MongoDB."""
        simulated_results = {
            "amazon": {"title": f"{self.test_query} Amz", "price": 15999, "product_url": "https://amazon.in/p/1"},
            "flipkart": {"title": f"{self.test_query} FK", "price": 15499, "product_url": "https://flipkart.com/p/2"},
            "meesho": {"title": f"{self.test_query} Mee", "price": 14999, "product_url": "https://meesho.com/p/3"},
            "gec": {"title": f"{self.test_query} GEC", "price": 14799, "product_url": "/gececom#p-4"}
        }

        status_map = record_search_prices_to_history(self.test_query, simulated_results)

        for plat in SUPPORTED_MARKETPLACES:
            self.assertTrue(status_map.get(plat), f"Failed to record price for {plat}")

            # Verify in MongoDB
            doc = database.db.price_history.find_one({
                "search_query": self.test_query.lower(),
                "platform": plat
            })
            self.assertIsNotNone(doc, f"No document found in MongoDB for {plat}")
            self.assertIn("price", doc)
            self.assertIn("recorded_at", doc)
            self.assertIn("formatted_price", doc)

    # ── Test 5: Aggregator with Multiple Marketplaces ──
    def test_05_get_marketplace_7d_predictions_aggregator(self):
        """Verify multi-marketplace aggregator compiles chart labels, datasets, and predictions."""
        # Use isolated test query
        agg_query = f"AggTest_{int(datetime.now().timestamp())}_{os.urandom(3).hex()}"
        now = datetime.now(timezone.utc)
        for i, p in enumerate([18000, 18200, 18500]):
            dt = now - timedelta(days=2 - i)
            database.db.price_history.insert_one({
                "product_name": f"{agg_query} Special",
                "search_query": agg_query.lower(),
                "platform": "gec",
                "price": float(p),
                "recorded_at": dt,
                "date": dt
            })

        preds = get_marketplace_7d_predictions(query=agg_query, current_prices={"gec": 18500})

        self.assertTrue(preds["success"])
        self.assertIn("marketplaces", preds)
        self.assertIn("chart", preds)
        self.assertIn("labels", preds["chart"])
        self.assertIn("datasets", preds["chart"])

        # GEC should have sufficient data
        gec_info = preds["marketplaces"]["gec"]
        self.assertTrue(gec_info["sufficient_data"])
        self.assertEqual(gec_info["trend"], "rising")
        self.assertGreater(gec_info["predicted_7d_price"], gec_info["current_price"])

    # ── Test 6: FastAPI Microservice Endpoints ──
    def test_06_fastapi_price_prediction_endpoints(self):
        """Verify standalone FastAPI microservice endpoints work with TestClient."""
        from fastapi.testclient import TestClient
        import fastapi_alerts

        fastapi_client = TestClient(fastapi_alerts.app)

        # GET /health
        res_h = fastapi_client.get("/health")
        self.assertEqual(res_h.status_code, 200)
        self.assertEqual(res_h.json().get("status"), "online")

        # GET /price-prediction
        res_pred = fastapi_client.get(f"/price-prediction?query={self.test_query}")
        self.assertEqual(res_pred.status_code, 200)
        data = res_pred.json()
        self.assertTrue(data.get("success"))
        self.assertIn("marketplaces", data)
        self.assertIn("chart", data)

        # GET /price-prediction with single platform filter
        res_single = fastapi_client.get(f"/price-prediction?query={self.test_query}&platform=gec")
        self.assertEqual(res_single.status_code, 200)
        s_data = res_single.json()
        self.assertIn("gec", s_data.get("marketplaces", {}))
        self.assertEqual(len(s_data.get("marketplaces", {})), 1)

        # POST /price-prediction/record
        res_rec = fastapi_client.post("/price-prediction/record", json={
            "query": f"{self.test_query}_fastapi",
            "platform_data": {
                "amazon": {"title": "FastAPI Amz Prod", "price": 999},
                "flipkart": {"title": "FastAPI FK Prod", "price": 899}
            }
        })
        self.assertEqual(res_rec.status_code, 201)
        self.assertTrue(res_rec.json().get("success"))

    # ── Test 7: Flask API Endpoints (/price-prediction & /api/price-prediction) ──
    def test_07_flask_price_prediction_routes(self):
        """Verify Flask API routes return proper JSON predictions."""
        res1 = self.client.get(f"/price-prediction?query={self.test_query}")
        self.assertEqual(res1.status_code, 200)
        data1 = json.loads(res1.data)
        self.assertTrue(data1.get("success"))

        res2 = self.client.get(f"/api/price-prediction?query={self.test_query}")
        self.assertEqual(res2.status_code, 200)
        data2 = json.loads(res2.data)
        self.assertTrue(data2.get("success"))

    # ── Test 8: Frontend HTML Integration in /search ──
    def test_08_results_template_renders_prediction_system(self):
        """Verify /search renders the AI prediction section, stat cards, and Chart.js canvas."""
        res = self.client.get(f"/search?q={self.test_query}")
        self.assertEqual(res.status_code, 200)
        html = res.data.decode("utf-8")

        # Section presence
        self.assertIn("aiPricePredictionSection", html)
        self.assertIn("7-Day Price Prediction", html)
        self.assertIn("pricePredictionChart", html)
        self.assertIn("chart-filter-btn", html)
        self.assertIn("refreshFastApiPrediction", html)
        self.assertIn("chart.js", html.lower())

        # All 4 marketplaces present in prediction cards
        self.assertIn("Amazon", html)
        self.assertIn("Flipkart", html)
        self.assertIn("Meesho", html)
        self.assertIn("GEC E-Commerce", html)

    # ── Test 9: Zero Regression on Existing Price Alerts ──
    def test_09_zero_regression_on_price_alerts(self):
        """Verify existing price alerts CRUD operations continue to work without regression."""
        payload = {
            "product_name": f"{self.test_query} Alert Item",
            "product_url": "https://www.flipkart.com/test",
            "platform": "flipkart",
            "current_price": 20000,
            "target_price": 18000,
            "email": "predict_tester@smartbuy.local"
        }
        res = self.client.post("/price-alert", json=payload)
        self.assertEqual(res.status_code, 201)
        data = json.loads(res.data)
        self.assertTrue(data.get("success"))
        alert_id = data.get("alert_id")

        # Cleanup with authorization header
        if alert_id:
            del_res = self.client.delete(
                f"/price-alert/{alert_id}",
                headers={"X-User-Email": payload["email"], "Accept": "application/json"}
            )
            self.assertEqual(del_res.status_code, 200)


if __name__ == "__main__":
    unittest.main()
