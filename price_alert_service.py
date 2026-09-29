"""
price_alert_service.py
======================
Top-level export for SmartBuy Price Alert Service.
"""
from services.price_alert_service import (
    fetch_latest_product_price,
    send_price_alert_email,
    check_single_alert,
    check_all_active_alerts,
    PriceAlertScheduler,
    start_price_alert_scheduler,
    stop_price_alert_scheduler,
    run_price_check_now,
    get_platform_scraper,
)

__all__ = [
    "fetch_latest_product_price",
    "send_price_alert_email",
    "check_single_alert",
    "check_all_active_alerts",
    "PriceAlertScheduler",
    "start_price_alert_scheduler",
    "stop_price_alert_scheduler",
    "run_price_check_now",
    "get_platform_scraper",
]
