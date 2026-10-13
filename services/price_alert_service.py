"""
services/price_alert_service.py
===============================
SmartBuy Price Alert Monitoring & Notification Engine:
- Connects to existing Amazon, Flipkart, and Meesho scrapers
- Periodically checks live product prices with complete fault isolation
- Enforces strict authenticity: never uses fake prices or treats N/A as ₹0
- Compares latest price with user's target price (current_price <= target_price)
- Dispatches rich HTML email alerts via secure SMTP configuration from .env
- Prevents duplicate alerts by transitioning status to 'triggered'
- Supports on-demand checking and configurable background scheduler
"""

import os
import re
import time
import logging
import threading
from typing import Optional, Dict, Any, Tuple
from datetime import datetime, timezone

from utils import parse_price, clean_text
from otp_utils import send_email as utils_send_email, get_smtp_config
from database import (
    get_price_alert_by_id,
    get_active_price_alerts,
    update_price_alert_checked,
    mark_price_alert_triggered,
    create_inbox_message,
)

logger = logging.getLogger("smartbuy.price_alert_service")

# Lazy scraper registry to reuse existing SmartBuy scrapers without overhead
_scraper_instances: Dict[str, Any] = {}
_scraper_lock = threading.Lock()


def get_platform_scraper(platform: str):
    """
    Retrieve or initialize the platform scraper adapter.
    Reuses existing BaseScraper implementations.
    """
    plat = (platform or "").strip().lower()
    with _scraper_lock:
        if plat in _scraper_instances:
            return _scraper_instances[plat]

        try:
            if plat in ("amazon", "amzn"):
                from scrapers.amazon_scraper import AmazonScraper
                _scraper_instances[plat] = AmazonScraper()
            elif plat in ("flipkart", "fk"):
                from scrapers.flipkart_scraper import FlipkartScraper
                _scraper_instances[plat] = FlipkartScraper()
            elif plat in ("meesho", "mee"):
                from scrapers.meesho_scraper import MeeshoScraper
                _scraper_instances[plat] = MeeshoScraper()
            elif plat in ("gec", "gececom"):
                from scrapers.gec_scraper import GecScraper
                _scraper_instances[plat] = GecScraper()
            else:
                logger.warning(f"[PriceChecker] Unsupported platform: {platform}")
                return None
        except Exception as e:
            logger.error(f"[PriceChecker] Error initializing scraper for '{platform}': {e}")
            return None

        return _scraper_instances.get(plat)


def fetch_latest_product_price(
    platform: str,
    product_url: str = "",
    product_name: str = ""
) -> Tuple[Optional[int], Optional[str], Optional[Dict[str, Any]]]:
    """
    Fetch the authentic latest price for a product using the existing SmartBuy scrapers.
    
    Guarantees:
    - Never returns fake or placeholder prices.
    - If scraper returns N/A, unavailable, empty price, or an error, returns None (NEVER 0).
    - Fully isolated: exceptions are caught and never crash the caller.
    
    Returns:
        (price_num, error_message, product_data_dict)
    """
    plat = (platform or "").strip().lower()
    clean_url = (product_url or "").strip()
    clean_name = (product_name or "").strip()

    # ── Strategy 0: GEC ECom Direct Mock Store Platform ───────────────────────
    if plat in ("gececom", "gec"):
        try:
            from database import get_gececom_product_by_id, get_gececom_products
            pid_match = re.search(r'#product-([a-f0-9]+)', clean_url, re.IGNORECASE) or re.search(r'products/([a-f0-9]+)', clean_url, re.IGNORECASE)
            prod = None
            if pid_match:
                prod = get_gececom_product_by_id(pid_match.group(1))
            if not prod and clean_name:
                for p in get_gececom_products():
                    p_name = p.get("product_name", "").lower()
                    if clean_name.lower() in p_name or p_name in clean_name.lower():
                        prod = p
                        break
            if prod:
                return int(prod["current_price"]), None, prod
            return None, "GEC ECom product not found in inventory", None
        except Exception as e:
            return None, f"GEC ECom lookup error: {e}", None

    scraper = get_platform_scraper(plat)
    if not scraper:
        return None, f"Unsupported shopping platform: '{platform}'", None

    # ── Strategy 1: Direct Product URL Scrape ──────────────────────────────────
    if clean_url:
        try:
            logger.info(f"[PriceChecker] Scraping URL for {plat}: {clean_url[:80]}...")
            prod, status, err = scraper.scrape_product_url(clean_url)

            if prod and isinstance(prod, dict):
                # Try price_num first, then parse price string
                p_num = prod.get("price_num")
                if p_num is not None and isinstance(p_num, (int, float)) and int(p_num) > 0:
                    return int(p_num), None, prod

                p_str = prod.get("price")
                if p_str:
                    parsed = parse_price(p_str)
                    if parsed is not None and parsed > 0:
                        return parsed, None, prod

            logger.info(f"[PriceChecker] Direct URL scrape for {plat} returned no price (status: {status}, err: {err})")
        except Exception as e:
            logger.warning(f"[PriceChecker] Scraper exception while checking URL '{clean_url}': {e}")

    # ── Strategy 2: Fallback to Product Name Search (Strict Title Similarity) ──
    if clean_name and not clean_url:
        try:
            logger.info(f"[PriceChecker] Fallback search on {plat} for product: '{clean_name[:60]}'")
            items, status, err = scraper.search_products(clean_name)
            if items and isinstance(items, list):
                from rapidfuzz import fuzz
                for item in items[:5]:
                    item_title = item.get("title", "")
                    sim = fuzz.token_set_ratio(clean_name.lower(), item_title.lower())
                    if sim < 70:
                        continue
                    p_num = item.get("price_num")
                    if p_num is not None and isinstance(p_num, (int, float)) and int(p_num) > 0:
                        return int(p_num), None, item

                    p_str = item.get("price")
                    if p_str:
                        parsed = parse_price(p_str)
                        if parsed is not None and parsed > 0:
                            return parsed, None, item
        except Exception as e:
            logger.warning(f"[PriceChecker] Search exception while checking '{clean_name}': {e}")

    # Neither strategy produced an authentic live price
    return None, "Price unavailable from marketplace at this time", None


def send_price_alert_email(
    to_email: str,
    product_name: str,
    platform: str,
    current_price: int,
    target_price: int,
    product_url: str,
    product_image: str = ""
) -> bool:
    """
    Send a rich HTML notification email to the user when an alert triggers.
    Includes product name, platform, current price, target price, and product link.
    Credentials are read securely from .env via get_smtp_config().
    """
    clean_email = str(to_email).strip()
    if not clean_email or "@" not in clean_email:
        logger.warning("[PriceAlertEmail] Missing or invalid recipient email.")
        return False

    plat_title = platform.strip().capitalize() if platform else "Marketplace"
    formatted_current = f"₹{current_price:,}"
    formatted_target = f"₹{target_price:,}"
    savings_amount = target_price - current_price

    subject = f"🔔 Price Drop Alert: {product_name[:40]} is now {formatted_current} on {plat_title}!"

    # Plain text version
    plain_text = f"""Hello,

Great news! The price for "{product_name}" on {plat_title} has reached your target price!

• Platform: {plat_title}
• Current Price: {formatted_current}
• Your Target Price: {formatted_target}
{"• Extra Savings: ₹" + f"{savings_amount:,}" if savings_amount > 0 else ""}
• View Product: {product_url}

Visit SmartBuy to manage your alerts or compare more products:
https://smartbuy.local/my-price-alerts

Best regards,
SmartBuy Price Alert System
"""

    # Image block
    img_html = ""
    if product_image and product_image.startswith("http"):
        img_html = f"""
        <div style="text-align: center; margin-bottom: 20px;">
            <img src="{product_image}" alt="{product_name}" style="max-height: 180px; max-width: 200px; object-fit: contain; border-radius: 12px; background: #ffffff; padding: 8px; border: 1px solid #e2e8f0;">
        </div>
        """

    savings_badge = ""
    if savings_amount > 0:
        savings_badge = f"""
        <div style="background: #ecfdf5; border: 1px solid #a7f3d0; color: #047857; font-weight: bold; padding: 8px 14px; border-radius: 20px; display: inline-block; font-size: 13px; margin-top: 8px;">
            🎉 Extra Savings: ₹{savings_amount:,} below your target!
        </div>
        """

    # Modern responsive HTML email template using SmartBuy Green & Royal Blue theme
    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>{subject}</title>
    </head>
    <body style="margin: 0; padding: 24px 10px; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #f0fdf4; color: #1e293b;">
        <table align="center" border="0" cellpadding="0" cellspacing="0" width="100%" style="max-width: 600px; background-color: #ffffff; border-radius: 20px; overflow: hidden; box-shadow: 0 10px 30px rgba(16, 185, 129, 0.1); border: 1px solid #d1fae5;">
            <!-- Brand Header -->
            <tr>
                <td style="background: linear-gradient(135deg, #10b981 0%, #059669 100%); padding: 30px 24px; text-align: center;">
                    <div style="font-size: 28px; font-weight: 800; color: #ffffff; letter-spacing: -0.5px;">
                        Smart<span style="color: #fef08a;">-Buy</span>
                    </div>
                    <div style="color: #d1fae5; font-size: 13px; font-weight: 500; margin-top: 4px; letter-spacing: 1px; text-transform: uppercase;">
                        Instant Price Drop Notification
                    </div>
                </td>
            </tr>

            <!-- Content Body -->
            <tr>
                <td style="padding: 32px 28px;">
                    <div style="text-align: center; margin-bottom: 24px;">
                        <span style="background: #dcfce7; color: #15803d; font-size: 13px; font-weight: 700; padding: 6px 14px; border-radius: 30px; text-transform: uppercase; letter-spacing: 0.5px;">
                            🎯 Target Price Reached!
                        </span>
                        <h2 style="color: #0f172a; font-size: 22px; font-weight: 800; margin: 16px 0 8px 0; line-height: 1.3;">
                            Your Desired Price is Live
                        </h2>
                        <p style="color: #64748b; font-size: 14px; margin: 0; line-height: 1.5;">
                            Good news! A product you are tracking on <strong>{plat_title}</strong> just reached or dropped below your target price.
                        </p>
                    </div>

                    {img_html}

                    <!-- Product Card Box -->
                    <div style="background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 16px; padding: 20px; margin-bottom: 24px;">
                        <div style="font-size: 16px; font-weight: 700; color: #0f172a; margin-bottom: 16px; line-height: 1.4;">
                            {product_name}
                        </div>

                        <table border="0" cellpadding="0" cellspacing="0" width="100%" style="font-size: 14px;">
                            <tr>
                                <td style="padding: 8px 0; color: #64748b;">Shopping Platform:</td>
                                <td style="padding: 8px 0; font-weight: 700; color: #0f172a; text-align: right;">{plat_title}</td>
                            </tr>
                            <tr>
                                <td style="padding: 8px 0; color: #64748b;">Your Target Price:</td>
                                <td style="padding: 8px 0; font-weight: 700; color: #475569; text-align: right;">{formatted_target}</td>
                            </tr>
                            <tr style="border-top: 1px dashed #cbd5e1;">
                                <td style="padding: 12px 0 6px 0; color: #059669; font-weight: 700; font-size: 16px;">Current Live Price:</td>
                                <td style="padding: 12px 0 6px 0; font-weight: 800; color: #10b981; font-size: 24px; text-align: right;">{formatted_current}</td>
                            </tr>
                        </table>

                        <div style="text-align: center;">
                            {savings_badge}
                        </div>
                    </div>

                    <!-- Call to action button -->
                    <div style="text-align: center; margin: 28px 0 16px 0;">
                        <a href="{product_url}" target="_blank" rel="noopener noreferrer" style="background: linear-gradient(135deg, #10b981 0%, #047857 100%); color: #ffffff; text-decoration: none; padding: 14px 32px; border-radius: 30px; font-weight: 700; font-size: 15px; display: inline-block; box-shadow: 0 4px 15px rgba(16, 185, 129, 0.35);">
                            🛒 Buy Now on {plat_title} &rarr;
                        </a>
                    </div>

                    <p style="text-align: center; color: #94a3b8; font-size: 12px; margin-top: 16px;">
                        Prices and availability are subject to change on the retailer's website. Act quickly before stock runs out!
                    </p>
                </td>
            </tr>

            <!-- Footer -->
            <tr>
                <td style="background-color: #f8fafc; border-top: 1px solid #e2e8f0; padding: 20px 24px; text-align: center; font-size: 12px; color: #94a3b8;">
                    <p style="margin: 0 0 6px 0;">
                        You received this email because you subscribed to a Price Alert on <strong>SmartBuy</strong>.
                    </p>
                    <p style="margin: 0;">
                        SmartBuy &bull; Multi-Platform Price, Review and Comparison System
                    </p>
                </td>
            </tr>
        </table>
    </body>
    </html>
    """

    try:
        success = utils_send_email(
            to_email=clean_email,
            subject=subject,
            body_text=plain_text,
            html_content=html_content
        )
        if success:
            logger.info(f"[PriceAlertEmail] Successfully delivered price alert email to {clean_email} for '{product_name}'")
        else:
            logger.warning(f"[PriceAlertEmail] Could not send price alert email to {clean_email}. Check SMTP settings in .env.")
        return success
    except Exception as e:
        logger.error(f"[PriceAlertEmail] Exception while sending email to {clean_email}: {e}")
        return False


def check_single_alert(alert_doc: Dict[str, Any], send_notification: bool = True) -> Dict[str, Any]:
    """
    Check an individual price alert against the live marketplace:
    1. Validates alert status is active
    2. Fetches authentic live price using existing scraper
    3. If price <= target_price:
       - Sends notification email
       - Updates MongoDB status to 'triggered' and alert_sent to True
       - Creates user inbox message
    4. Enforces fault isolation: never crashes on scraper error or network failure
    
    Returns report dictionary.
    """
    if not alert_doc:
        return {"success": False, "error": "No alert document provided"}

    alert_id = str(alert_doc.get("id") or alert_doc.get("_id") or "")
    status = alert_doc.get("status") or alert_doc.get("alert_status")

    if status != "active":
        return {
            "alert_id": alert_id,
            "checked": False,
            "triggered": False,
            "reason": f"Alert is not in active state (current status: '{status}')"
        }

    if alert_doc.get("alert_sent") is True:
        return {
            "alert_id": alert_id,
            "checked": False,
            "triggered": False,
            "reason": "Alert notification has already been sent (prevent duplicate notifications)"
        }

    platform = alert_doc.get("platform", "")
    product_url = alert_doc.get("product_url", "")
    product_name = alert_doc.get("product_name", "")
    target_price = alert_doc.get("target_price") or 0
    email = alert_doc.get("email", "")
    user_id = alert_doc.get("user_id")

    try:
        latest_price, err, raw_prod = fetch_latest_product_price(
            platform=platform,
            product_url=product_url,
            product_name=product_name
        )

        # Scraper failure or price unavailable
        if latest_price is None or latest_price <= 0:
            logger.info(f"[PriceChecker] Alert {alert_id} ({product_name[:30]}): Price unavailable or scraper returned error ({err}). Alert not triggered.")
            # Record that we checked this alert at this time, without altering current price
            update_price_alert_checked(alert_id, latest_price=None)
            return {
                "alert_id": alert_id,
                "checked": True,
                "triggered": False,
                "latest_price": None,
                "target_price": target_price,
                "error": err or "Price unavailable"
            }

        # Authentic price successfully retrieved
        logger.info(f"[PriceChecker] Alert {alert_id} ({product_name[:30]}): Live price is ₹{latest_price:,} (Target: ₹{target_price:,})")

        # Update last checked timestamp & latest price in database
        update_price_alert_checked(alert_id, latest_price=latest_price)

        # Check condition: current price <= target price
        if latest_price <= target_price:
            logger.info(f"[PriceChecker] 🎯 TARGET REACHED for alert {alert_id}! Live ₹{latest_price:,} <= Target ₹{target_price:,}")
            email_sent = False
            if send_notification:
                email_sent = send_price_alert_email(
                    to_email=email,
                    product_name=product_name,
                    platform=platform,
                    current_price=latest_price,
                    target_price=target_price,
                    product_url=product_url,
                    product_image=alert_doc.get("product_image", "")
                )

                # Also create in-app inbox notification for logged-in users
                if user_id:
                    try:
                        create_inbox_message(
                            user_id=user_id,
                            message_type="price_alert",
                            title=f"Price Alert: {product_name[:40]} reached ₹{latest_price:,}!",
                            message=(
                                f"Great news! Your tracked product '{product_name}' on {platform.title()} "
                                f"has dropped to ₹{latest_price:,}, matching your target price of ₹{target_price:,}."
                            )
                        )
                    except Exception as inb_err:
                        logger.warning(f"[PriceChecker] Could not create inbox message: {inb_err}")

            # Transition status to 'triggered' and set alert_sent=True to prevent duplicate notifications
            mark_price_alert_triggered(alert_id, latest_price=latest_price, alert_sent=True)

            return {
                "alert_id": alert_id,
                "checked": True,
                "triggered": True,
                "latest_price": latest_price,
                "target_price": target_price,
                "difference": target_price - latest_price,
                "email_sent": email_sent,
                "product_name": product_name,
                "platform": platform
            }
        else:
            return {
                "alert_id": alert_id,
                "checked": True,
                "triggered": False,
                "latest_price": latest_price,
                "target_price": target_price,
                "difference": latest_price - target_price,
                "product_name": product_name
            }

    except Exception as e:
        logger.error(f"[PriceChecker] Unexpected exception checking alert {alert_id}: {e}", exc_info=True)
        # Never crash: record error and leave alert active
        return {
            "alert_id": alert_id,
            "checked": False,
            "triggered": False,
            "error": str(e)
        }


def check_all_active_alerts(send_notifications: bool = True) -> Dict[str, Any]:
    """
    Check all active price alerts across the system.
    Processes each alert with individual fault isolation so a single failed
    scraper will never crash the batch or backend.
    """
    active_alerts = get_active_price_alerts()
    total = len(active_alerts)
    logger.info(f"[PriceChecker] Starting price check cycle for {total} active alert(s)...")

    checked_count = 0
    triggered_count = 0
    error_count = 0
    results = []

    for alert in active_alerts:
        try:
            res = check_single_alert(alert, send_notification=send_notifications)
            results.append(res)
            if res.get("checked"):
                checked_count += 1
            if res.get("triggered"):
                triggered_count += 1
            if res.get("error"):
                error_count += 1
        except Exception as alert_err:
            error_count += 1
            logger.error(f"[PriceChecker] Error processing alert {alert.get('id')}: {alert_err}")

    summary = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_active": total,
        "checked": checked_count,
        "triggered": triggered_count,
        "errors": error_count,
        "details": results
    }
    logger.info(f"[PriceChecker] Check cycle finished. Checked: {checked_count}, Triggered: {triggered_count}, Errors: {error_count}")
    return summary


# ════════════════════ BACKGROUND SCHEDULED WORKER ════════════════════

class PriceAlertScheduler:
    """
    Background daemon thread that periodically checks all active price alerts
    at a configurable interval.
    """

    def __init__(self, interval_seconds: Optional[int] = None):
        default_interval = int(os.getenv("PRICE_CHECK_INTERVAL_SECONDS", "300"))
        self.interval = interval_seconds or default_interval
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._is_running = False

    def start(self):
        if self._is_running:
            logger.info("[PriceAlertScheduler] Scheduler is already active.")
            return

        self._stop_event.clear()
        self._is_running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="PriceAlertWorker")
        self._thread.start()
        logger.info(f"[PriceAlertScheduler] Background price checker started (interval: {self.interval}s).")

    def stop(self):
        if not self._is_running:
            return
        logger.info("[PriceAlertScheduler] Stopping background price checker...")
        self._stop_event.set()
        self._is_running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)
        logger.info("[PriceAlertScheduler] Background price checker stopped.")

    def _run_loop(self):
        # Initial sleep on startup to let server fully initialize
        self._stop_event.wait(10)
        while not self._stop_event.is_set():
            try:
                check_all_active_alerts(send_notifications=True)
            except Exception as e:
                logger.error(f"[PriceAlertScheduler] Unexpected error in price check loop: {e}", exc_info=True)

            # Wait for next interval or stop event
            self._stop_event.wait(self.interval)


# Global singleton instance
_global_scheduler = PriceAlertScheduler()


def start_price_alert_scheduler(interval_seconds: Optional[int] = None):
    """Start background price alert scheduler daemon."""
    global _global_scheduler
    if interval_seconds:
        _global_scheduler.interval = interval_seconds
    _global_scheduler.start()


def stop_price_alert_scheduler():
    """Stop background price alert scheduler."""
    global _global_scheduler
    _global_scheduler.stop()


def run_price_check_now(alert_id: Optional[str] = None) -> Dict[str, Any]:
    """Manually trigger a check on all alerts or a specific alert."""
    if alert_id:
        doc = get_price_alert_by_id(alert_id)
        if not doc:
            return {"success": False, "error": f"Alert '{alert_id}' not found"}
        return check_single_alert(doc, send_notification=True)
    return check_all_active_alerts(send_notifications=True)


def trigger_gececom_price_alerts_for_product(
    product_id: str,
    product_name: str,
    new_price: int,
    product_url: str = "",
    product_image: str = ""
) -> Dict[str, Any]:
    """
    Evaluate all active price alerts matching a GEC ECom product.
    If new_price <= target_price, dispatches rich email notification,
    creates user inbox message, and transitions alert status to 'triggered'.
    """
    from database import get_active_price_alerts
    active_alerts = get_active_price_alerts()
    evaluated = 0
    triggered = 0

    clean_pname = (product_name or "").strip().lower()

    for alert in active_alerts:
        alert_plat = str(alert.get("platform") or "").strip().lower()
        alert_pname = str(alert.get("product_name") or "").strip().lower()
        alert_url = str(alert.get("product_url") or "").strip()

        # Match logic: by product ID in URL, or by platform 'gececom', or matching title
        is_match = False
        if product_id and (f"product-{product_id}" in alert_url or f"products/{product_id}" in alert_url):
            is_match = True
        elif alert_plat in ("gececom", "gec"):
            if not clean_pname or not alert_pname:
                is_match = True
            elif clean_pname in alert_pname or alert_pname in clean_pname:
                is_match = True
        elif clean_pname and (clean_pname == alert_pname or (len(clean_pname) > 6 and clean_pname[:15] in alert_pname)):
            is_match = True

        if is_match:
            evaluated += 1
            target_price = int(alert.get("target_price") or 0)
            aid = str(alert.get("id") or alert.get("_id"))
            update_price_alert_checked(aid, latest_price=new_price)

            if new_price <= target_price:
                email = alert.get("email")
                user_id = alert.get("user_id")
                img = product_image or alert.get("product_image", "")
                p_url = product_url or alert.get("product_url", f"/gececom#product-{product_id}")

                email_ok = send_price_alert_email(
                    to_email=email,
                    product_name=alert.get("product_name") or product_name,
                    platform="GEC ECom",
                    current_price=new_price,
                    target_price=target_price,
                    product_url=p_url,
                    product_image=img
                )

                if user_id:
                    try:
                        create_inbox_message(
                            user_id=user_id,
                            message_type="price_alert",
                            title=f"Price Alert: {product_name[:40]} dropped to ₹{new_price:,} on GEC ECom!",
                            message=(
                                f"Great news! Your tracked item '{product_name}' on GEC ECom "
                                f"has dropped to ₹{new_price:,}, matching your target price of ₹{target_price:,}."
                            )
                        )
                    except Exception as inb_err:
                        logger.warning(f"[PriceChecker] Error creating inbox message: {inb_err}")

                mark_price_alert_triggered(aid, latest_price=new_price, alert_sent=email_ok)
                triggered += 1

    return {"evaluated": evaluated, "triggered": triggered}

