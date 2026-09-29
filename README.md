# 🛒 SmartBuy — Multi-Platform Price, Review & Comparison System

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![Flask](https://img.shields.io/badge/Flask-3.x-green.svg)](https://palletsprojects.com/p/flask/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110%2B-teal.svg)](https://fastapi.tiangolo.com/)
[![MongoDB](https://img.shields.io/badge/MongoDB-6.x%2F7.x-brightgreen.svg)](https://www.mongodb.com/)
[![Playwright](https://img.shields.io/badge/Playwright-WebScraper-orange.svg)](https://playwright.dev/)

**SmartBuy** is a real-time, multi-platform product comparison and price-drop tracking engine. It scrapes, normalizes, and compares live pricing, ratings, and customer reviews across **Amazon**, **Flipkart**, and **Meesho** — coupled with an intelligent **Price Drop Alert & Notification System** powered by MongoDB, live scrapers, background scheduling, and instant email delivery.

---

## 🌐 Live Server Links (Servers Connected & Online)

When the servers are running, access the following interactive interfaces:

| Service / Interface | URL | Description |
|---|---|---|
| 🏠 **SmartBuy Web Application** | [http://localhost:5000](http://localhost:5000) | Main search, multi-platform comparison, and deal discovery |
| 🔔 **My Price Alerts Dashboard** | [http://localhost:5000/my-price-alerts](http://localhost:5000/my-price-alerts) | Manage tracked products, pause/resume/reactivate/delete alerts |
| 🔍 **Live Search & Compare** | [http://localhost:5000/search?q=phone](http://localhost:5000/search?q=phone) | Direct side-by-side comparison across Amazon, Flipkart, Meesho |
| 📚 **FastAPI Interactive Docs (Swagger UI)** | [http://localhost:8000/docs](http://localhost:8000/docs) | Interactive API exploration and testing for price alert endpoints |
| 📖 **FastAPI ReDoc Reference** | [http://localhost:8000/redoc](http://localhost:8000/redoc) | Alternative clean REST API documentation |
| 🩺 **FastAPI Health Check** | [http://localhost:8000/health](http://localhost:8000/health) | API status & MongoDB connection health check |
| 🗄️ **MongoDB Database** | `mongodb://localhost:27017` / `smartbuy` | Active database collection for users, searches, and `price_alerts` |

---

## 🚀 Key Features

### 1. 🔍 Universal Multi-Platform Comparison
- **Parallel Scraping**: Concurrently queries **Amazon.in**, **Flipkart.com**, and **Meesho.com** via thread pools and Playwright engines.
- **Strict Similarity Matching (≥ 90%)**: Powered by **RapidFuzz** (`token_set_ratio`, `token_sort_ratio`) with anti-pattern filters to prevent false matches against phone cases, accessories, or mismatched storage variants.
- **Scraper Fault Isolation**: If one retailer is slow or blocked, SmartBuy isolates the error and seamlessly displays available deals from the other platforms with non-blocking status badges.
- **Deal Badges**: Automatically detects and highlights **Lowest Price**, **Best Rated**, and **Best Value** deals.

### 2. 🔔 Real-Time Price Alert System
- **“🔔 Set Price Alert” Modal**: Accessible on canonical cards, platform cards, best deal banners, and grid comparisons.
- **One-Click Target Discounts**: Built-in `-5%`, `-10%`, `-15%`, and `-20%` quick-calculation buttons.
- **Strict Validation**: Requires authentic positive prices (`target_price > 0` and `target_price < current_price`) and valid email formats.
- **Authentic Scraped Prices Only**:
  - Never treats timeouts, blocks, or N/A as ₹0.
  - Scraper errors are safely isolated and never trigger false alarms.
  - URL scrapers enforce strict product matching before evaluating price triggers.
- **Automated Email Notifications**:
  - Delivers a branded, responsive HTML email with the product thumbnail, target vs current price, savings amount, and direct marketplace button.
  - Automatically sends an in-app message to the SmartBuy Inbox.
- **Anti-Duplicate Guarantee**:
  - Upon reaching `latest_price <= target_price`, the alert transitions to `triggered` and `alert_sent=True`. No repeated or duplicate emails will be sent.
- **Interactive “My Price Alerts” Dashboard**:
  - Live metric KPI cards (Total Alerts, Active Monitoring, Triggered Deals, Total Potential Savings).
  - Filter pills: **All**, **Active**, **Triggered**, **Paused**.
  - Action buttons: **Pause**, **Resume**, **Reactivate**, **Delete** (with confirmation modal), and **“Check Prices Now”**.

---

## 🏗️ Architecture & Project Structure

```
updated-the-smartbuy/
├── app.py                      # Flask application factory & route coordinator
├── api.py                      # Flask API blueprint (Auth, Search, Price Alerts, Inbox)
├── fastapi_alerts.py           # Standalone FastAPI router & OpenAPI spec for Price Alerts
├── database.py                 # MongoDB layer (Users, Searches, Inbox, price_alerts)
├── price_alert_service.py      # Root export for background price checker service
├── services/
│   └── price_alert_service.py  # Scraper price check engine, SMTP mailer, background scheduler
├── scrapers/                   # Retailer web scrapers
│   ├── amazon/                 # Amazon search and direct URL scrapers
│   ├── flipkart/               # Flipkart search and direct URL scrapers
│   └── meesho/                 # Meesho search and direct URL scrapers
├── search/                     # Core search pipeline, RapidFuzz matching, ranking
├── templates/                  # Jinja2 HTML templates
│   ├── index.html              # Homepage with category cards and navigation
│   ├── results.html            # Comparison results with "🔔 Set Price Alert" modal
│   ├── my_price_alerts.html    # Price alert management dashboard
│   ├── login.html              # Sign-in page
│   ├── signup.html             # User registration
│   └── ...
├── tests/
│   └── test_price_alert_system.py # 10 unit tests for price alerts, scrapers, APIs
├── .env                        # Local environment configuration
├── .env.example                # Sample environment template
└── requirements.txt            # Python dependencies
```

---

## 🗄️ MongoDB `price_alerts` Schema

Stored in database `smartbuy`, collection `price_alerts`:

```json
{
  "_id": "6abb7814fda8d81e24b142a0",
  "user_id": "670123abc...",
  "email": "shopper@example.com",
  "product_name": "Vivo T4 5G (8GB RAM, 128GB)",
  "product_url": "https://www.flipkart.com/vivo-t4-5g/p/itm123456",
  "platform": "flipkart",
  "current_price": 27000,
  "target_price": 25000,
  "status": "active",
  "alert_status": "active",
  "last_checked_price": 26499,
  "alert_sent": false,
  "alert_sent_status": "pending",
  "product_image": "https://rukminim2.flixcart.com/...",
  "created_at": "2026-09-29T08:30:00Z",
  "updated_at": "2026-09-29T08:35:00Z",
  "last_checked_at": "2026-09-29T08:35:00Z"
}
```

### Collection Indexes:
- `[("user_id", ASCENDING), ("created_at", DESCENDING)]` — fast lookup per authenticated user
- `[("email", ASCENDING), ("created_at", DESCENDING)]` — fast lookup per email address
- `[("status", ASCENDING)]` — background scheduler query optimization

---

## 📡 REST API Reference

The Price Alert API is available on both **Flask** (`http://localhost:5000`) and **FastAPI** (`http://localhost:8000`):

| Method | Endpoint | Description | Request Body / Headers |
|---|---|---|---|
| `POST` | `/price-alert` | Create a new price drop alert | JSON: `{ product_name, product_url, platform, current_price, target_price, email, product_image? }` |
| `GET` | `/price-alerts` | Retrieve user's price alerts | Header `X-User-ID` or `X-User-Email` (or query param) |
| `PUT` | `/price-alert/{id}/pause` | Pause price monitoring | Header `X-User-ID` / `X-User-Email` |
| `PUT` | `/price-alert/{id}/resume` | Resume monitoring an alert | Header `X-User-ID` / `X-User-Email` |
| `PUT` | `/price-alert/{id}/reactivate` | Reactivate a triggered alert | Header `X-User-ID` / `X-User-Email` |
| `DELETE`| `/price-alert/{id}` | Delete a price alert | Header `X-User-ID` / `X-User-Email` |
| `POST` | `/price-alert/{id}/check` | Check live price immediately for one alert | Header `X-User-ID` / `X-User-Email` |
| `POST` | `/price-alerts/check-now` | Trigger batch price check for all active alerts | None |
| `GET` | `/my-price-alerts` | Render responsive web dashboard | Session cookie (Flask) |

---

## ⚙️ Installation & Setup

### 1. Prerequisites
- **Python 3.10+**
- **MongoDB** running locally on `localhost:27017` (or MongoDB Atlas URI)
- **Playwright** browser binaries

### 2. Clone & Navigate into Project Directory
```powershell
cd "d:\hithesh github\updated-the-smartbuy"
```

### 3. Install Python Dependencies
```powershell
pip install -r requirements.txt
python -m playwright install chromium
```

### 4. Configure Environment Variables (`.env`)
Create or edit `.env` in the `updated-the-smartbuy` folder:
```env
PORT=5000
SECRET_KEY=smartbuy_super_secret_key_2026

# MongoDB Connection
MONGO_URI=mongodb://localhost:27017
DB_NAME=smartbuy

# Price Alert Background Scheduler Interval (seconds)
PRICE_CHECK_INTERVAL_SECONDS=300

# SMTP Email Configuration (for Price Drop Notifications)
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=your_email@gmail.com
SMTP_PASS=your_app_password
EMAIL_FROM="SmartBuy Alerts" <your_email@gmail.com>
```

---

## 🏃 Running the Application

### Option A: Start the SmartBuy Web Application (Flask + Scrapers + Background Scheduler)
```powershell
cd "d:\hithesh github\updated-the-smartbuy"
python app.py
```
- Web Application: **[http://localhost:5000](http://localhost:5000)**
- Price Alerts Page: **[http://localhost:5000/my-price-alerts](http://localhost:5000/my-price-alerts)**

> *Note: The background price checker starts automatically when `app.py` is launched and checks prices every 300 seconds.*

---

### Option B: Start the FastAPI Alert Microservice (With Interactive Swagger Docs)
```powershell
cd "d:\hithesh github\updated-the-smartbuy"
uvicorn fastapi_alerts:app --port 8000 --reload
```
- Interactive Swagger UI: **[http://localhost:8000/docs](http://localhost:8000/docs)**
- ReDoc API Reference: **[http://localhost:8000/redoc](http://localhost:8000/redoc)**

---

## 🧪 Running the Automated Unit Tests

> ⚠️ **Important**: Always run the test command from inside the `updated-the-smartbuy` project folder so Python can locate the modules and test files.

```powershell
cd "d:\hithesh github\updated-the-smartbuy"
python -m unittest tests/test_price_alert_system.py
```

### Expected Test Output:
```
INFO:smartbuy.price_alert_service:[PriceAlertScheduler] Background price checker started (interval: 300s).
INFO:smartbuy.database:[PriceAlert] Created alert ... for 'Vivo T4 5G' (flipkart)
...
INFO:smartbuy.price_alert_service:[PriceAlertEmail] Successfully delivered price alert email ...
INFO:smartbuy.database:[PriceAlert] Alert ... marked as TRIGGERED at price ₹24,999
...
----------------------------------------------------------------------
Ran 10 tests in 7.235s

OK
```

### Test Coverage Highlights:
1. `test_01_create_price_alert_valid`: Valid alert creation and MongoDB storage.
2. `test_02_create_price_alert_validation_errors`: Validates negative prices, invalid emails, missing URLs.
3. `test_03_api_create_price_alert_endpoint`: Tests `POST /price-alert` endpoint.
4. `test_04_user_isolation`: Ensures strict isolation — User A cannot view or modify User B's alerts.
5. `test_05_alert_state_transitions`: Verifies `Active ➔ Paused ➔ Resumed ➔ Triggered ➔ Reactivated ➔ Deleted`.
6. `test_06_scraper_unavailable_authentic_prices_only`: Verifies scraper timeouts/errors are never treated as ₹0 and never trigger false alarms.
7. `test_07_price_drop_triggers_email_and_prevents_duplicate`: Verifies live price `<= target_price` triggers email notification and marks `alert_sent=True` without duplicate triggers.
8. `test_08_paused_alert_is_skipped`: Verifies paused alerts are excluded from background checks.
9. `test_09_my_price_alerts_page_rendering`: Verifies `GET /my-price-alerts` HTML rendering and UI components.
10. `test_10_fastapi_endpoints`: Verifies all FastAPI endpoints via `TestClient`.

---

## 🔒 Security & Best Practices
- **Strict User Isolation**: All price alerts are partitioned by `user_id` and verified against the requester identity before permitting updates or deletions.
- **Environment Isolation**: SMTP credentials and MongoDB connection strings are managed exclusively via `.env`.
- **Fault-Tolerant Scrapers**: Browser scrapers run with individual try-catch blocks and stealth headers to prevent bans and gracefully isolate failures.
