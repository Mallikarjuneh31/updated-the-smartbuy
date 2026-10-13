"""
api.py
======
Smart-Buy: Multiplatform Price Review Comparison System
======================================================
Flask API Blueprint & Route Handlers:
- / (Home UI)
- /search (Product search and side-by-side comparison page)
- /api/search (JSON API endpoint)
- /autocomplete (Live search query suggestions)
- /api/health (System status health check)
"""

import json
import time
import re
import os
import logging
import secrets
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone
from functools import wraps
from flask import Blueprint, render_template, request, redirect, jsonify, session, flash, url_for, Response, stream_with_context
from werkzeug.security import generate_password_hash, check_password_hash
from search_engine import fetch_all_products_parallel, fetch_all_products_with_fallbacks, process_results
from cache import get_cached_search, set_cached_search
from url_detector import validate_and_detect_url, detect_search_type
from comparison_engine import compare_by_product_url, compare_by_product_url_stream
from search.search_router import route_search

from database import (
    log_search_query, get_trending_queries, get_query_count,
    create_user, get_user_by_email, get_user_by_id, update_user_name,
    update_user_password, update_user_password_by_email,
    log_user_search, get_user_search_history,
    create_user_feedback, get_user_feedback_list, get_feedback_by_id,
    update_user_feedback, delete_user_feedback, get_user_stats,
    create_inbox_message, get_user_inbox_messages, get_user_unread_inbox_count,
    mark_inbox_message_read, delete_inbox_message, get_inbox_message_by_id,
    save_user_comparison, get_user_saved_comparisons, save_user_selected_product,
    get_user_selected_products, get_latest_user_selected_product,
    set_search_selected_platform, _format_selected_product,
    update_user_last_login, create_admin_inbox_notification,
    get_admin_inbox_notifications, get_admin_unread_count,
    mark_admin_notification_read, delete_admin_notification,
    get_all_users_for_admin, get_user_full_details_for_admin,
    store_otp, verify_otp_code, check_otp_resend_cooldown, get_otp_expiry_minutes,
    delete_otp, is_email_verified,
    create_price_alert, get_price_alert_by_id, get_user_price_alerts,
    get_active_price_alerts, pause_price_alert, resume_price_alert,
    reactivate_price_alert, delete_price_alert,
    get_gececom_products, get_gececom_product_by_id,
    create_gececom_product, update_gececom_product_price,
    delete_gececom_product
)
from services.price_alert_service import (
    run_price_check_now, check_all_active_alerts, check_single_alert,
    trigger_gececom_price_alerts_for_product
)
from autocomplete_engine import build_trie_from_history, get_autocomplete_suggestions
from search.normalizer import normalize_query, build_search_query
from search.filters import parse_filter_params
from utils import clean_text
from otp_utils import send_otp_email, send_email as utils_send_email, generate_otp, validate_password

logger = logging.getLogger("smartbuy.api")
api_bp = Blueprint("api", __name__)


def send_email(to_email: str, subject: str, body_text: str, html_content: str = None) -> bool:
    """
    Send an email via SMTP using credentials from .env or safe verified fallback.
    Returns True on success, False on any failure.
    """
    return utils_send_email(to_email=to_email, subject=subject, body_text=body_text, html_content=html_content)


def login_required(f):
    """Decorator to require login for user routes."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash("Please Sign Up or Sign In to access your profile.", "warning")
            return redirect(url_for('api.signin', next=request.url))
        return f(*args, **kwargs)
    return decorated_function


def admin_required(f):
    """Decorator to require Admin role for admin routes."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        user_id = session.get('user_id')
        if not user_id:
            flash("Please sign in with Administrator credentials.", "warning")
            return redirect(url_for('api.admin_login', next=request.url))
        user = get_user_by_id(user_id)
        if not user or not user.get('is_admin'):
            return render_template('403.html'), 403
        return f(*args, **kwargs)
    return decorated_function


@api_bp.app_errorhandler(404)
def not_found_error(error):
    if request.path.startswith('/api/'):
        return jsonify({"status": "error", "message": "Resource not found"}), 404
    return render_template('index.html', category_hints=CATEGORY_HINTS, trending_searches=get_trending_queries(limit=8)), 404


@api_bp.app_errorhandler(500)
def internal_server_error(error):
    logger.error(f"Internal Server Error: {error}", exc_info=True)
    if request.path.startswith('/api/'):
        return jsonify({"status": "error", "message": "Internal Server Error"}), 500
    return render_template('index.html', category_hints=CATEGORY_HINTS, trending_searches=get_trending_queries(limit=8)), 500


# Category hint chips for home page
CATEGORY_HINTS = [
    {"label": "Mobiles",         "icon": "fas fa-mobile-alt",   "query": "Mobile Phone"},
    {"label": "Laptops",         "icon": "fas fa-laptop",        "query": "Laptop"},
    {"label": "Headphones",      "icon": "fas fa-headphones",    "query": "Headphones"},
    {"label": "Smartwatches",    "icon": "fas fa-clock",         "query": "Smartwatch"},
    {"label": "Televisions",     "icon": "fas fa-tv",            "query": "Television"},
    {"label": "Grocery",         "icon": "fas fa-shopping-basket","query": "Grocery"},
    {"label": "Fashion",         "icon": "fas fa-tshirt",        "query": "Fashion"},
    {"label": "Beauty",          "icon": "fas fa-spa",           "query": "Beauty Products"},
    {"label": "Sports",          "icon": "fas fa-futbol",        "query": "Sports Equipment"},
    {"label": "Books",           "icon": "fas fa-book",          "query": "Books"},
    {"label": "Home Appliances", "icon": "fas fa-blender",       "query": "Home Appliances"},
    {"label": "Gaming",          "icon": "fas fa-gamepad",       "query": "Gaming"},
    {"label": "Baby Care",       "icon": "fas fa-baby",          "query": "Baby Products"},
    {"label": "Pet Supplies",    "icon": "fas fa-paw",           "query": "Pet Supplies"},
    {"label": "Furniture",       "icon": "fas fa-couch",         "query": "Furniture"},
    {"label": "Kitchen",         "icon": "fas fa-utensils",      "query": "Kitchen Appliances"},
]

# Trie cache
_trie_cache = {'trie': None, 'built_at': 0}
TRIE_TTL = 120


def get_current_trie():
    now = time.time()
    if _trie_cache['trie'] is None or (now - _trie_cache['built_at'] > TRIE_TTL):
        _trie_cache['trie'] = build_trie_from_history()
        _trie_cache['built_at'] = now
    return _trie_cache['trie']


@api_bp.route('/', methods=['GET'])
def index():
    """Home page — dynamic search-first UI."""
    trending = get_trending_queries(limit=8)
    return render_template('index.html',
                           category_hints=CATEGORY_HINTS,
                           trending_searches=trending)


@api_bp.route('/about', methods=['GET'])
def about():
    """About SmartBuy system page."""
    return render_template('about.html')


def normalize_external_url(url: str, platform: str = "") -> str:
    """Ensure external product link starts with valid http/https protocol."""
    if not url or url.strip() in ["#", "None", "null", "undefined", "javascript:void(0)"]:
        plat_lower = (platform or "").lower()
        if "amazon" in plat_lower:
            return "https://www.amazon.in"
        elif "flipkart" in plat_lower:
            return "https://www.flipkart.com"
        elif "meesho" in plat_lower:
            return "https://www.meesho.com"
        return "https://www.google.com"

    u = url.strip()
    if u.startswith("//"):
        return "https:" + u
    if u.startswith("http://") or u.startswith("https://"):
        return u

    plat_lower = (platform or "").lower()
    if "amazon" in plat_lower:
        base = "https://www.amazon.in"
    elif "flipkart" in plat_lower:
        base = "https://www.flipkart.com"
    elif "meesho" in plat_lower:
        base = "https://www.meesho.com"
    else:
        base = "https://"

    if not u.startswith("/") and not base.endswith("/"):
        return base + "/" + u
    elif u.startswith("/") and base.endswith("/"):
        return base + u[1:]
    else:
        return base + u


# ── Buy Button Routes (/buy/amazon, /buy/flipkart, /buy/meesho, /buy/gec, /deal/redirect) ──
@api_bp.route('/buy/amazon', methods=['GET'])
@api_bp.route('/buy/flipkart', methods=['GET'])
@api_bp.route('/buy/meesho', methods=['GET'])
@api_bp.route('/buy/gec', methods=['GET'])
@api_bp.route('/deal/redirect', methods=['GET'])
def handle_buy_click():
    """Handler for user clicking Buy on Amazon/Flipkart/Meesho/GEC. Checks auth; saves product in session; redirects to signup if unauthenticated."""
    deal_url_raw = request.args.get('url', '').strip()
    platform = request.args.get('platform', '').strip()
    if not platform:
        path = request.path.lower()
        if 'amazon' in path:
            platform = 'Amazon'
        elif 'flipkart' in path:
            platform = 'Flipkart'
        elif 'meesho' in path:
            platform = 'Meesho'
        elif 'gec' in path:
            platform = 'GEC E-Commerce'
        else:
            platform = 'Online Store'

    # Sanitize and normalize platform
    platform_clean = platform.strip()
    p_low = platform_clean.lower()
    if 'amazon' in p_low:
        platform_clean = 'Amazon'
    elif 'flipkart' in p_low:
        platform_clean = 'Flipkart'
    elif 'meesho' in p_low:
        platform_clean = 'Meesho'
    elif 'gec' in p_low:
        platform_clean = 'GEC E-Commerce'
    elif platform_clean:
        platform_clean = platform_clean.capitalize()
    else:
        platform_clean = 'Online Store'

    product_name = request.args.get('product_name', '').strip() or request.args.get('title', '').strip() or 'Searched Product'
    price = request.args.get('price', '').strip()
    price_num_raw = request.args.get('price_num', '').strip()
    rating = request.args.get('rating', '').strip()
    reviews = request.args.get('reviews', '').strip()
    image = request.args.get('image', '').strip()
    specifications = request.args.get('specifications', '').strip()
    search_query = request.args.get('search_query', '').strip()

    # If price is missing or placeholder, resolve from price_num, recent scraped search results, or catalog
    if not price or price.lower() in ('', 'not available', 'none', 'price unavailable', 'n/a', 'best deal', 'best', '0', '0.0'):
        if price_num_raw and price_num_raw.isdigit():
            price = f"₹{int(price_num_raw):,}"
        else:
            # Check recent search history platform_results for authentic scraped price
            from database import db, CATALOG_DEALS, to_object_id
            found_price = None
            if db is not None:
                q_filters = []
                if 'user_id' in session:
                    uid = to_object_id(session['user_id'])
                    if uid:
                        q_filters.append({"user_id": uid})
                q_filter = q_filters[0] if q_filters else {}
                try:
                    recent_searches = db.search_history.find(q_filter).sort("searched_at", -1).limit(5)
                    for s in recent_searches:
                        pr = s.get("platform_results", {})
                        for plat_k, items in pr.items():
                            if isinstance(items, list):
                                for it in items:
                                    if isinstance(it, dict):
                                        it_title = str(it.get("title") or "").lower()
                                        it_link = str(it.get("link") or it.get("product_url") or "").lower()
                                        if (deal_url_raw and deal_url_raw.lower() in it_link) or (product_name and (product_name.lower() in it_title or it_title in product_name.lower())):
                                            if it.get("price_num"):
                                                found_price = f"₹{int(it['price_num']):,}"
                                                break
                                            elif it.get("price"):
                                                found_price = str(it["price"])
                                                break
                                if found_price:
                                    break
                        if found_price:
                            break
                except Exception:
                    pass
            if found_price:
                price = found_price
            else:
                full_title = f"{product_name} {search_query}".lower()
                for k, d in CATALOG_DEALS.items():
                    if k in full_title:
                        price = f"₹{d['price']:,}"
                        break

    deal_url = normalize_external_url(deal_url_raw, platform_clean)
    search_id = request.args.get('search_id', '').strip() or None

    raw_pending = {
        'product_name': product_name,
        'platform': platform_clean,
        'price': price,
        'rating': rating,
        'reviews': reviews,
        'image_url': image,
        'specifications': specifications,
        'product_url': deal_url,
        'search_query': search_query,
        'search_id': search_id
    }
    pending_product = _format_selected_product(raw_pending)

    # Save selected product in session (session["pending_product"])
    session['pending_product'] = pending_product
    session['pending_purchase'] = pending_product
    session['pending_selected_product'] = pending_product
    session['pending_deal_url'] = deal_url

    # Check if user is logged in
    if 'user_id' in session:
        user_id = session['user_id']
        set_search_selected_platform(
            user_id=user_id,
            search_id=search_id,
            platform=pending_product.get('platform', platform_clean),
            price=pending_product.get('price', price),
            product_name=pending_product.get('product_name', product_name),
            product_url=pending_product.get('product_url', deal_url),
            image_url=pending_product.get('image_url', image)
        )
        return redirect(url_for('api.profile'))

    # Unauthenticated -> redirect to Sign Up
    flash("Please create an account or sign in to continue your purchase.", "info")
    return redirect(url_for('api.signup'))


@api_bp.route('/api/auth/signup', methods=['POST'])
@api_bp.route('/api/signup', methods=['POST'])
@api_bp.route('/signup', methods=['GET', 'POST'])
def signup():
    """
    User Registration Endpoint:
    - Supports JSON API requests (POST /api/signup, POST /api/auth/signup) and standard HTML form POSTs (POST /signup).
    - Validates name, email, password (>=6 chars), confirm_password match.
    - Checks duplicate email (HTTP 409).
    - Hashes password using Werkzeug.
    - Saves user in database and creates Admin Inbox notification.
    - Returns HTTP 201, 400, 409, 500 with exact JSON response specification.
    """
    if request.method == 'GET':
        if 'user_id' in session:
            return redirect(url_for('api.profile'))
        return render_template('signup.html')

    # Detect if request is JSON / AJAX
    is_json_req = request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest' or 'application/json' in request.headers.get('Accept', '') or request.path in ('/api/signup', '/api/auth/signup')
    is_auth_api = request.path == '/api/auth/signup'

    if request.is_json:
        data = request.get_json() or {}
        name = str(data.get('name', '')).strip()
        email = str(data.get('email', '')).strip().lower()
        password = str(data.get('password', ''))
        confirm_password = str(data.get('confirm_password', ''))
    else:
        name = request.form.get('name', '').strip()
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')

    # Requirement 1 & 2 Backend Validation
    if not name:
        msg = "Please enter valid account details"
        if is_json_req:
            return jsonify({"success": False, "message": msg}), 400
        return render_template('signup.html', errors=["Full Name is required."], name=name, email=email), 400

    if not email or '@' not in email or '.' not in email:
        msg = "Please enter valid account details"
        if is_json_req:
            return jsonify({"success": False, "message": msg}), 400
        return render_template('signup.html', errors=["Please enter a valid email address."], name=name, email=email), 400

    if len(password) < 6:
        msg = "Password must be at least 6 characters long."
        if is_json_req:
            return jsonify({"success": False, "message": msg}), 400
        return render_template('signup.html', errors=[msg], name=name, email=email), 400

    if password != confirm_password:
        msg = "Passwords do not match."
        if is_json_req:
            return jsonify({"success": False, "message": msg}), 400
        return render_template('signup.html', errors=[msg], name=name, email=email), 400

    if is_auth_api:
        if not is_email_verified(email):
            return jsonify({"success": False, "message": "Please verify your email before creating an account."}), 400
        is_pwd_ok, pwd_errors = validate_password(password)
        if not is_pwd_ok:
            return jsonify({"success": False, "message": pwd_errors[0] if pwd_errors else "Password does not meet complexity requirements."}), 400

    # Password Hash & Database Insert
    pwd_hash = generate_password_hash(password)
    user_id, status_code, db_msg = create_user(name, email, pwd_hash)

    if status_code == 201 and user_id:
        if is_json_req:
            return jsonify({"success": True, "message": "Account created successfully"}), 201

        flash("User registered successfully and saved to MongoDB.", "success")
        return redirect(url_for('api.signin'))
    elif status_code == 409:
        msg = "Email already registered. Please sign in."
        if is_json_req:
            return jsonify({"success": False, "message": msg}), 409
        return render_template('signup.html', errors=[msg], name=name, email=email), 409
    else:
        logger.error(f"Signup database error for email {email}: {db_msg}")
        msg = "Unable to create account"
        if is_json_req:
            return jsonify({"success": False, "message": msg}), status_code or 500
        return render_template('signup.html', errors=[msg], name=name, email=email), status_code or 500


# ════════════════════ FORGOT / RESET PASSWORD ════════════════════
# 4-step flow: forgot_password.html (enter email) -> /send-reset-otp ->
# enter OTP -> /verify-reset-otp -> /reset-password?email=... (set new
# password) -> /reset-password POST -> back to /signin.
# None of these routes existed before — "Forgot Password?" had nowhere
# real to go. Reuses store_otp/verify_otp_code/update_user_password_by_email
# from database.py, which already existed but were unused for this purpose.

@api_bp.route('/forgot-password', methods=['GET'])
def forgot_password():
    """Step 1 page: enter registered email to receive a reset OTP."""
    email = request.args.get('email', '').strip()
    return render_template('forgot_password.html', email=email)


@api_bp.route('/send-reset-otp', methods=['POST'])
def send_reset_otp():
    """Generate a 6-digit OTP, store it (purpose='password_reset'), and
    email it to the user. Matches forgot_password.html's sendOtpForm."""
    data = request.get_json(silent=True) or {}
    email = str(data.get('email', '')).strip().lower()

    if not email or '@' not in email or '.' not in email:
        return jsonify({"success": False, "message": "Please enter a valid email address."}), 400

    user = get_user_by_email(email)
    if not user:
        # Don't reveal whether an email is registered (avoids account
        # enumeration) — respond the same way whether or not it exists,
        # but only actually generate/send an OTP when it does.
        logger.info(f"[Password Reset] OTP requested for unregistered email: {email}")
        return jsonify({"success": True, "message": f"If {email} is registered, an OTP has been sent."}), 200

    allowed, cooldown_msg = check_otp_resend_cooldown(email, cooldown_seconds=60)
    if not allowed:
        return jsonify({"success": False, "message": cooldown_msg}), 429

    otp_code = f"{secrets.randbelow(1_000_000):06d}"
    expiry_minutes = get_otp_expiry_minutes()

    if not store_otp(email, otp_code, channel='email', purpose='password_reset', ttl_minutes=expiry_minutes):
        return jsonify({"success": False, "message": "Unable to generate OTP right now. Please try again."}), 500

    sent, send_err = send_otp_email(
        to_email=email,
        otp_code=otp_code,
        purpose='password_reset'
    )
    if not sent:
        logger.error(f"[Password Reset] Failed to send OTP email to {email}: {send_err}")
        return jsonify({"success": False, "message": "Unable to send the OTP email right now. Please try again shortly."}), 500

    return jsonify({"success": True, "message": f"OTP sent to {email}."}), 200


@api_bp.route('/verify-reset-otp', methods=['POST'])
def verify_reset_otp():
    """Verify the submitted OTP. Matches forgot_password.html's verifyOtpForm."""
    data = request.get_json(silent=True) or {}
    email = str(data.get('email', '')).strip().lower()
    otp = str(data.get('otp', '')).strip()

    if not email or not otp:
        return jsonify({"success": False, "message": "Email and OTP are required."}), 400

    ok, msg = verify_otp_code(email, otp, channel='email', purpose='password_reset')
    if not ok:
        return jsonify({"success": False, "message": msg}), 400

    # Single-purpose, short-lived session flag proving this email actually
    # completed OTP verification — required by /reset-password below so
    # nobody can jump straight to /reset-password?email=someone-else and
    # set their password without ever proving ownership of that inbox.
    session['reset_password_email'] = email
    session['reset_password_verified_at'] = datetime.now(timezone.utc).isoformat()

    return jsonify({"success": True, "message": "OTP verified successfully."}), 200


@api_bp.route('/api/send-otp', methods=['POST'])
@api_bp.route('/api/auth/send-otp', methods=['POST'])
def api_send_otp():
    """Generate and dispatch OTP for signup/email verification via SMTP."""
    data = request.get_json(silent=True) or {}
    email = str(data.get('email', '')).strip().lower()

    if not email or '@' not in email or '.' not in email:
        return jsonify({"success": False, "message": "Please enter a valid email address."}), 400

    allowed, cooldown_msg = check_otp_resend_cooldown(email, cooldown_seconds=60)
    if not allowed:
        return jsonify({"success": False, "message": cooldown_msg}), 429

    otp_code = generate_otp()
    expiry_minutes = get_otp_expiry_minutes()

    if not store_otp(email, otp_code, channel='email', purpose='signup', ttl_minutes=expiry_minutes):
        return jsonify({"success": False, "message": "Unable to generate OTP right now. Please try again."}), 500

    sent, send_err = send_otp_email(to_email=email, otp_code=otp_code, purpose='signup')
    if not sent:
        logger.error(f"[API OTP] Failed to send OTP email to {email}: {send_err}")
        return jsonify({"success": False, "message": "Unable to send the OTP email right now. Please try again shortly."}), 500

    return jsonify({"success": True, "message": "OTP sent successfully"}), 200


@api_bp.route('/api/verify-otp', methods=['POST'])
@api_bp.route('/api/auth/verify-otp', methods=['POST'])
def api_verify_otp():
    """Verify submitted OTP code for signup/verification."""
    data = request.get_json(silent=True) or {}
    email = str(data.get('email', '')).strip().lower()
    otp = str(data.get('otp', '')).strip()

    if not email or not otp:
        return jsonify({"success": False, "message": "Email and OTP are required."}), 400

    ok, msg = verify_otp_code(email, otp, channel='email', purpose='signup')
    if not ok:
        ok_any, _ = verify_otp_code(email, otp, channel='email', purpose='password_reset')
        if not ok_any:
            return jsonify({"success": False, "message": msg or "Invalid OTP"}), 400

    session['verified_email'] = email
    return jsonify({"success": True, "message": "Email verified successfully"}), 200


@api_bp.route('/api/auth/resend-otp', methods=['POST'])
@api_bp.route('/resend-otp', methods=['POST'])
def api_resend_otp():
    """Resend OTP code subject to cooldown."""
    if request.is_json:
        data = request.get_json(silent=True) or {}
        email = str(data.get('email', '')).strip().lower()
    else:
        email = str(request.form.get('email', '')).strip().lower()

    if not email:
        email = session.get('pending_user_email', session.get('reset_password_email', ''))

    if not email or '@' not in email:
        return jsonify({"success": False, "message": "Valid email is required."}), 400

    allowed, cooldown_msg = check_otp_resend_cooldown(email, cooldown_seconds=60)
    if not allowed:
        return jsonify({"success": False, "message": cooldown_msg}), 429

    otp_code = generate_otp()
    expiry_minutes = get_otp_expiry_minutes()
    store_otp(email, otp_code, channel='email', purpose='signup', ttl_minutes=expiry_minutes)

    sent, send_err = send_otp_email(to_email=email, otp_code=otp_code, purpose='signup')
    if not sent:
        return jsonify({"success": False, "message": "Failed to resend OTP email."}), 500

    return jsonify({"success": True, "message": "OTP resent successfully"}), 200


@api_bp.route('/reset-password', methods=['GET'])
def reset_password_page():
    """Step 3 page: set a new password. Requires a prior verified OTP for
    this exact email in the current session (set by /verify-reset-otp)."""
    email = request.args.get('email', '').strip().lower()
    if not email or session.get('reset_password_email') != email:
        # BUG-AVOIDANCE: forgot_password.html reads `error`/`message` as
        # template variables passed directly by render_template — it does
        # NOT render Flask's flash() messages (no get_flashed_messages
        # block in that template). flash()-then-redirect here would have
        # silently discarded this message.
        return render_template(
            'forgot_password.html', email=email,
            error="Please verify your email with an OTP before resetting your password."
        )
    return render_template('reset_password.html', email=email)


@api_bp.route('/reset-password', methods=['POST'])
def reset_password_submit():
    """Final step: hash and save the new password, then send the user to sign in."""
    is_json_req = request.is_json or 'application/json' in request.headers.get('Accept', '')

    if request.is_json:
        data = request.get_json(silent=True) or {}
    else:
        data = request.form

    # reset_password.html's JS never sends "email" in its fetch body — only
    # new_password/confirm_new_password. The authoritative email is whatever
    # was verified via OTP in /verify-reset-otp and stored in the session;
    # we never trust a client-supplied email for this step, which also
    # closes off any possibility of resetting a different account's password
    # by tampering with a form field.
    email = session.get('reset_password_email', '')
    password = str(data.get('new_password', ''))
    confirm_password = str(data.get('confirm_new_password', ''))

    if not email:
        msg = "Your verification session expired. Please request a new OTP."
        if is_json_req:
            return jsonify({"success": False, "message": msg}), 401
        flash(msg, "danger")
        return redirect(url_for('api.forgot_password'))

    # Matches the exact rules reset_password.html's own JS already shows the
    # user live feedback on (8+ chars, starts uppercase, has a number, has a
    # special char) — enforced server-side too, since client-only validation
    # can always be bypassed by calling the API directly.
    if len(password) < 8:
        msg = "Password must be at least 8 characters long."
        if is_json_req:
            return jsonify({"success": False, "message": msg}), 400
        return render_template('reset_password.html', email=email, errors=[msg]), 400
    if not re.match(r'^[A-Z]', password):
        msg = "Password must start with an uppercase English letter (A-Z)."
        if is_json_req:
            return jsonify({"success": False, "message": msg}), 400
        return render_template('reset_password.html', email=email, errors=[msg]), 400
    if not re.search(r'[0-9]', password):
        msg = "Password must contain at least one number (0-9)."
        if is_json_req:
            return jsonify({"success": False, "message": msg}), 400
        return render_template('reset_password.html', email=email, errors=[msg]), 400
    if not re.search(r'[^A-Za-z0-9]', password):
        msg = "Password must contain at least one special character (e.g. @ ! # $ % ^ & *)."
        if is_json_req:
            return jsonify({"success": False, "message": msg}), 400
        return render_template('reset_password.html', email=email, errors=[msg]), 400

    if password != confirm_password:
        msg = "Confirm Password must exactly match New Password."
        if is_json_req:
            return jsonify({"success": False, "message": msg}), 400
        return render_template('reset_password.html', email=email, errors=[msg]), 400

    pwd_hash = generate_password_hash(password)
    if not update_user_password_by_email(email, pwd_hash):
        msg = "Unable to reset password right now. Please try again."
        if is_json_req:
            return jsonify({"success": False, "message": msg}), 500
        return render_template('reset_password.html', email=email, errors=[msg]), 500

    delete_otp(email)
    session.pop('reset_password_email', None)
    session.pop('reset_password_verified_at', None)

    if is_json_req:
        return jsonify({"success": True, "message": "Password reset successfully.", "next": url_for('api.signin')}), 200
    flash("Password reset successfully. Please sign in with your new password.", "success")
    return redirect(url_for('api.signin'))


@api_bp.route('/signin', methods=['GET', 'POST'])
def signin():
    """Sign in existing user and redirect to Profile page."""
    if 'user_id' in session:
        user = get_user_by_id(session['user_id'])
        if user and user.get('is_admin'):
            return redirect(url_for('api.admin_dashboard'))
        return redirect('/profile')

    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')

        user = get_user_by_email(email)
        if user and check_password_hash(user.get('password_hash', ''), password):
            u_id = str(user.get('id') or user.get('_id', ''))
            u_name = user.get('name', 'User')
            session['user_id'] = u_id
            session['user_name'] = u_name
            session['is_admin'] = user.get('is_admin', 0)
            update_user_last_login(u_id)

            # Transfer pending session purchase data to authenticated user account
            pending_prod = session.get('pending_product') or session.get('pending_purchase') or session.get('pending_selected_product')
            if pending_prod:
                save_user_selected_product(
                    u_id, None, pending_prod.get('product_name', ''),
                    pending_prod.get('platform', ''), pending_prod.get('price', ''),
                    pending_prod.get('rating', ''), pending_prod.get('reviews', ''),
                    pending_prod.get('specifications', ''), pending_prod.get('image_url', ''),
                    pending_prod.get('product_url', '')
                )

            flash(f"Welcome back, {u_name}! 👋", "success")
            if user.get('is_admin'):
                return redirect(url_for('api.admin_dashboard'))
            return redirect(url_for('api.profile'))
        else:
            error = "Invalid email or password."
            return render_template('signin.html', error=error, email=email)

    return render_template('signin.html')


@api_bp.route('/logout', methods=['GET'])
def logout():
    """Log out current user."""
    session.clear()
    flash("You have been logged out successfully.", "info")
    return redirect('/')


@api_bp.route('/my-searches', methods=['GET'])
@login_required
def my_searches():
    """Display logged-in user's search history."""
    history = get_user_search_history(session['user_id'])
    return render_template('my_searches.html', history=history)


# ── SmartBuy Inbox Routes ──
@api_bp.route('/inbox', methods=['GET'])
@login_required
def inbox():
    """Display private SmartBuy Inbox for logged-in user."""
    messages = get_user_inbox_messages(session['user_id'])
    return render_template('inbox.html', inbox_messages=messages)


@api_bp.route('/inbox/read/<message_id>', methods=['POST'])
@login_required
def read_inbox_message(message_id):
    """Mark an inbox message as read with strict 403 ownership verification."""
    msg = get_inbox_message_by_id(message_id, session['user_id'])
    if not msg:
        return "403 Unauthorized Access", 403

    mark_inbox_message_read(message_id, session['user_id'])
    return redirect(url_for('api.inbox'))


@api_bp.route('/inbox/delete/<message_id>', methods=['POST'])
@login_required
def remove_inbox_message(message_id):
    """Delete an inbox message with strict 403 ownership verification."""
    msg = get_inbox_message_by_id(message_id, session['user_id'])
    if not msg:
        return "403 Unauthorized Access", 403

    delete_inbox_message(message_id, session['user_id'])
    flash("Notification deleted.", "info")
    return redirect(url_for('api.inbox'))


# ── Feedback & Rating Routes ──
@api_bp.route('/my-feedback', methods=['GET'])
@api_bp.route('/profile/feedback', methods=['GET'])
@login_required
def my_feedback():
    """Display or retrieve logged-in user's submitted feedback list."""
    feedbacks = get_user_feedback_list(session['user_id'])
    if request.is_json or request.headers.get('Accept') == 'application/json':
        return jsonify({"status": "success", "feedbacks": feedbacks})
    return render_template('my_feedback.html', feedbacks=feedbacks)


@api_bp.route('/feedback/new', methods=['POST'])
@api_bp.route('/profile/feedback', methods=['POST'])
@login_required
def create_feedback():
    """Submit new feedback with mandatory rating (1-5), title, and message validation."""
    title = request.form.get('title', '').strip() or (request.json.get('title', '').strip() if request.is_json else '')
    message = request.form.get('message', '').strip() or (request.json.get('message', '').strip() if request.is_json else '')
    rating_raw = request.form.get('rating', '') or (request.json.get('rating', '') if request.is_json else '')
    redirect_target = request.form.get('redirect_to', '')

    try:
        rating = int(rating_raw)
        if rating < 1 or rating > 5:
            raise ValueError()
    except (ValueError, TypeError):
        msg = "Rating is required and must be between 1 and 5 stars."
        if request.is_json:
            return jsonify({"status": "error", "message": msg}), 400
        flash(msg, "danger")
        return redirect(redirect_target if (redirect_target and redirect_target.startswith('/')) else url_for('api.profile'))

    if not title or not message:
        msg = "Feedback Title and Message are required."
        if request.is_json:
            return jsonify({"status": "error", "message": msg}), 400
        flash(msg, "danger")
        return redirect(redirect_target if (redirect_target and redirect_target.startswith('/')) else url_for('api.profile'))

    fb_id = create_user_feedback(session['user_id'], title, message, rating)
    if fb_id:
        user = get_user_by_id(session['user_id'])
        user_name = user.get('name', 'User') if isinstance(user, dict) else 'User'
        user_email = user.get('email', '') if isinstance(user, dict) else ''
        create_admin_inbox_notification(
            user_id=session['user_id'],
            user_name=user_name,
            email=user_email,
            event_type='FEEDBACK_SUBMITTED',
            title='New User Feedback',
            message=f"{user_name} submitted rating {rating}/5 stars: '{title}'"
        )
        msg = "Rating and feedback saved successfully."
        if request.is_json:
            return jsonify({"status": "success", "message": msg, "feedback_id": fb_id}), 201
        flash(msg, "success")
    else:
        msg = "Failed to save feedback. Please try again."
        if request.is_json:
            return jsonify({"status": "error", "message": msg}), 500
        flash(msg, "danger")

    return redirect(redirect_target if (redirect_target and redirect_target.startswith('/')) else url_for('api.profile'))


@api_bp.route('/feedback/edit/<feedback_id>', methods=['POST'])
@api_bp.route('/profile/feedback/<feedback_id>', methods=['PUT', 'POST'])
@login_required
def edit_feedback(feedback_id):
    """Edit an existing feedback entry owned by current user with ownership check."""
    # Check ownership first (Section 12 requirement)
    existing_fb = get_feedback_by_id(feedback_id, session['user_id'])
    if not existing_fb:
        msg = "Feedback not found or access denied."
        if request.is_json:
            return jsonify({"status": "error", "message": msg}), 403
        flash(msg, "danger")
        return redirect(url_for('api.profile'))

    title = request.form.get('title', '').strip() or (request.json.get('title', '').strip() if request.is_json else '')
    message = request.form.get('message', '').strip() or (request.json.get('message', '').strip() if request.is_json else '')
    rating_raw = request.form.get('rating', '5') or (request.json.get('rating', '5') if request.is_json else '')

    try:
        rating = int(rating_raw)
        if rating < 1 or rating > 5:
            raise ValueError()
    except (ValueError, TypeError):
        msg = "Rating must be between 1 and 5 stars."
        if request.is_json:
            return jsonify({"status": "error", "message": msg}), 400
        flash(msg, "danger")
        return redirect(url_for('api.profile'))

    if not title or not message:
        msg = "Feedback title and message are required."
        if request.is_json:
            return jsonify({"status": "error", "message": msg}), 400
        flash(msg, "danger")
        return redirect(url_for('api.profile'))

    updated = update_user_feedback(feedback_id, session['user_id'], title, message, rating)
    if updated:
        msg = "Rating and feedback updated successfully."
        if request.is_json:
            return jsonify({"status": "success", "message": msg}), 200
        flash(msg, "success")
    else:
        msg = "Unable to update feedback."
        if request.is_json:
            return jsonify({"status": "error", "message": msg}), 500
        flash(msg, "danger")
    return redirect(url_for('api.profile'))


@api_bp.route('/feedback/delete/<feedback_id>', methods=['POST'])
@api_bp.route('/profile/feedback/delete/<feedback_id>', methods=['POST'])
@api_bp.route('/profile/feedback/<feedback_id>', methods=['DELETE'])
@login_required
def delete_feedback(feedback_id):
    """Delete a feedback entry owned by current user with ownership check."""
    existing_fb = get_feedback_by_id(feedback_id, session['user_id'])
    if not existing_fb:
        msg = "Feedback not found or access denied."
        if request.is_json:
            return jsonify({"status": "error", "message": msg}), 403
        flash(msg, "danger")
        return redirect(url_for('api.profile'))

    deleted = delete_user_feedback(feedback_id, session['user_id'])
    if deleted:
        msg = "Feedback deleted successfully."
        if request.is_json:
            return jsonify({"status": "success", "message": msg}), 200
        flash(msg, "success")
    else:
        msg = "Unable to delete feedback."
        if request.is_json:
            return jsonify({"status": "error", "message": msg}), 500
        flash(msg, "danger")
    return redirect(url_for('api.profile'))


@api_bp.route('/api/select-product', methods=['POST', 'GET'])
@login_required
def select_product_api():
    """Endpoint for user selecting a specific platform deal from search activity or modal."""
    data = request.get_json(silent=True) or request.form.to_dict() or request.args.to_dict()
    search_id = data.get('search_id')
    platform = (data.get('platform') or 'Flipkart').strip()
    price = str(data.get('price') or '').strip()
    product_name = data.get('product_name', '').strip()
    product_url = data.get('product_url', '').strip()
    image_url = data.get('image_url', '').strip()
    query = data.get('query', '').strip()

    user_id = session.get('user_id')
    set_search_selected_platform(
        user_id=user_id,
        search_id=search_id,
        platform=platform,
        price=price,
        product_name=product_name or query or "Selected Product",
        product_url=product_url,
        image_url=image_url
    )

    selected_product = _format_selected_product({
        'product_name': product_name or query or "Selected Product",
        'platform': platform,
        'price': price,
        'product_url': product_url,
        'image_url': image_url,
        'search_query': query
    })
    session['pending_product'] = selected_product
    session['pending_purchase'] = selected_product
    session['pending_selected_product'] = selected_product

    if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({
            'status': 'success',
            'message': f"Selected {platform} deal at {selected_product.get('price')}",
            'selected_product': selected_product
        })

    flash(f"Successfully selected {platform} deal ({selected_product.get('price')})!", "success")
    return redirect(url_for('api.profile'))


@api_bp.route('/profile', methods=['GET'])
@login_required
def profile():
    """Display user profile page with restored selected product."""
    stats = get_user_stats(session['user_id'])
    
    # Restore pending or latest selected product
    selected_product = session.get('pending_product') or session.get('pending_purchase') or session.get('pending_selected_product')
    
    # If session product has no price or placeholder, refresh from latest database record
    p_check = str((selected_product or {}).get('price') or '').strip().lower()
    if not selected_product or not p_check or p_check in ('', 'none', 'n/a', 'price unavailable', 'not available', 'best deal available', 'best deal', 'best', '0', '0.0'):
        db_prod = get_latest_user_selected_product(session['user_id'])
        if db_prod:
            selected_product = db_prod

    if selected_product:
        selected_product = _format_selected_product(dict(selected_product))
        session['pending_product'] = selected_product
        session['pending_purchase'] = selected_product
        session['pending_selected_product'] = selected_product

    return render_template('profile.html', stats=stats, selected_product=selected_product, restored_comparison=selected_product)


@api_bp.route('/deal/continue', methods=['GET'])
@login_required
def continue_to_deal():
    """Continue to Buy button handler — redirects user to real scraped product URL and logs Deal Opened in Inbox."""
    selected_product = session.get('pending_product') or session.get('pending_purchase') or session.get('pending_selected_product')
    if not selected_product:
        db_prod = get_latest_user_selected_product(session['user_id'])
        if db_prod:
            selected_product = db_prod

    if selected_product and selected_product.get('product_url'):
        platform = selected_product.get('platform', 'the seller website')
        p_name = selected_product.get('product_name', 'Selected Product')
        p_price = selected_product.get('price', 'N/A')
        
        # Log Deal Opened entry in user's Inbox
        create_inbox_message(
            session['user_id'], 'deal_opened', '🔗 Deal Opened',
            f"You opened this product on {platform}.\n\nProduct:\n{p_name}\n\nPrice at selection:\n{p_price}"
        )
        target_url = normalize_external_url(str(selected_product.get('product_url') or ''), platform)
        return redirect(target_url)

    flash("No active selected product found.", "info")
    return redirect(url_for('api.profile'))


@api_bp.route('/profile/update-name', methods=['POST'])
@login_required
def profile_update_name():
    """Update logged-in user's name."""
    new_name = request.form.get('name', '').strip()
    if not new_name:
        flash("Full name cannot be empty.", "danger")
        return redirect(url_for('api.profile'))

    if update_user_name(session['user_id'], new_name):
        session['user_name'] = new_name
        flash("Profile name updated successfully!", "success")
    else:
        flash("Failed to update profile name.", "danger")
    return redirect(url_for('api.profile'))


@api_bp.route('/profile/change-password', methods=['POST'])
@login_required
def profile_change_password():
    """Change logged-in user's password."""
    current_password = request.form.get('current_password', '')
    new_password = request.form.get('new_password', '')
    confirm_password = request.form.get('confirm_password', '')

    user = get_user_by_id(session['user_id'])
    user_email = user.get('email', '') if isinstance(user, dict) else ''
    full_user = get_user_by_email(user_email) if user_email else None

    if not full_user or not check_password_hash(full_user.get('password_hash', ''), current_password):
        flash("Current password is incorrect.", "danger")
        return redirect(url_for('api.profile'))

    if len(new_password) < 6:
        flash("New password must be at least 6 characters long.", "danger")
        return redirect(url_for('api.profile'))

    if new_password != confirm_password:
        flash("New password and confirm password do not match.", "danger")
        return redirect(url_for('api.profile'))

    new_hash = generate_password_hash(new_password)
    if update_user_password(session['user_id'], new_hash):
        flash("Password changed successfully!", "success")
    else:
        flash("Failed to change password.", "danger")
    return redirect(url_for('api.profile'))


@api_bp.route('/search', methods=['GET'])
@api_bp.route('/search/results', methods=['GET'])
def search():
    """Main search route for side-by-side multi-platform comparison."""
    url_param = request.args.get('url', '').strip()
    raw_q = request.args.get('q', '').strip()

    # Automatically detect whether user entered a product URL in 'q' or 'url'
    if not url_param and raw_q:
        detected = detect_search_type(raw_q)
        if detected.get('type') == 'product_url':
            url_param = detected.get('url') or raw_q

    if url_param:
        if '/gececom' in url_param or 'gececom#product-' in url_param:
            pid_m = re.search(r'#product-([a-f0-9]+)', url_param, re.IGNORECASE) or re.search(r'products/([a-f0-9]+)', url_param, re.IGNORECASE)
            g_prod = get_gececom_product_by_id(pid_m.group(1)) if pid_m else None
            if g_prod:
                target_q = g_prod.get('product_name') or 'Vivo T4'
                return redirect(url_for('api.search', q=target_q))
            return redirect(url_for('api.gececom_storefront'))

        fresh = request.args.get('fresh', '0').lower() in ('1', 'true', 'yes')
        comp_result = compare_by_product_url(url_param, fresh=fresh)
        if not comp_result.get('success'):
            flash(comp_result.get('error', 'Unable to retrieve or compare product from the provided URL.'), 'danger')
            return redirect('/')

        src_prod = comp_result.get('source_product') or {}
        p_name = src_prod.get('title') or src_prod.get('product_name') or 'Product'

        log_search_query(p_name)
        user_id = session.get('user_id')
        if user_id:
            try:
                best_deal = comp_result.get('best_deal') or comp_result.get('overall_best')
                b_plat = ""
                b_price = None
                b_title = ""
                b_url = ""
                if isinstance(best_deal, dict):
                    b_plat = best_deal.get('platform') or ""
                    b_price = best_deal.get('price_num') or best_deal.get('price')
                    b_title = best_deal.get('title') or best_deal.get('product_name') or ""
                    b_url = best_deal.get('link') or best_deal.get('product_url') or ""
                elif src_prod:
                    b_plat = comp_result.get('source_platform_name') or comp_result.get('source_platform') or ""
                    b_price = src_prod.get('price_num') or src_prod.get('price')
                    b_title = p_name
                    b_url = url_param

                if not b_price:
                    matches = comp_result.get('matches', {})
                    for m_prod in matches.values():
                        if isinstance(m_prod, dict) and (m_prod.get('price_num') or m_prod.get('price')):
                            b_price = m_prod.get('price_num') or m_prod.get('price')
                            if not b_plat:
                                b_plat = m_prod.get('platform', '')
                            break

                if not b_price:
                    from database import CATALOG_DEALS
                    full_txt = f"{p_name} {url_param}".lower()
                    for k, d in CATALOG_DEALS.items():
                        if k in full_txt:
                            b_price = d['price']
                            if not b_plat:
                                b_plat = d['platform']
                            break

                best_q_url = comp_result.get('comparison_summary', {}).get('best_quality') if isinstance(comp_result.get('comparison_summary'), dict) else None
                q_score_url = best_q_url.get('quality_score') if best_q_url else None
                d_conf_url = best_q_url.get('data_confidence') if best_q_url else None

                log_user_search(
                    user_id, p_name, src_prod.get('category', 'General'),
                    num_results=3, platforms_found="Amazon, Flipkart, Meesho",
                    best_platform=b_plat, best_price=b_price,
                    best_product_title=b_title, best_product_url=b_url,
                    best_deal=best_deal,
                    quality_score=q_score_url, data_confidence=d_conf_url
                )
            except Exception:
                pass

        matches = comp_result.get('matches', {})
        matched_list = [p for p in matches.values() if p]

        platforms_payload = {
            "amazon": {
                "status": comp_result.get('platform_status', {}).get('Amazon', {}).get('status', 'success'),
                "products": [matches['amazon']] if matches.get('amazon') else []
            },
            "flipkart": {
                "status": comp_result.get('platform_status', {}).get('Flipkart', {}).get('status', 'success'),
                "products": [matches['flipkart']] if matches.get('flipkart') else []
            },
            "meesho": {
                "status": comp_result.get('platform_status', {}).get('Meesho', {}).get('status', 'success'),
                "products": [matches['meesho']] if matches.get('meesho') else []
            }
        }

        comparison = {
            "query": p_name,
            "matched_products": matched_list,
            "specifications": comp_result.get('specifications_matrix', []),
            "quality_comparison_table": comp_result.get('quality_comparison_table', []),
            "comparison_summary": comp_result.get('comparison_summary', {}),
            "best_deal": comp_result.get('best_deal'),
            "specification_table": {
                "has_match": bool(len(matched_list) >= 2),
                "rows": comp_result.get('specifications_matrix', []),
                "columns": ["specification", "amazon", "flipkart", "meesho"]
            },
            "amazon": matches.get('amazon'),
            "flipkart": matches.get('flipkart'),
            "meesho": matches.get('meesho'),
            "products": {
                "amazon": [matches['amazon']] if matches.get('amazon') else [],
                "flipkart": [matches['flipkart']] if matches.get('flipkart') else [],
                "meesho": [matches['meesho']] if matches.get('meesho') else []
            }
        }

        best_per_plat = {
            "Amazon": matches.get('amazon'),
            "Flipkart": matches.get('flipkart'),
            "Meesho": matches.get('meesho'),
        }

        top_verified_offers = comp_result.get('top_verified_offers', {})
        val_prods = comp_result.get('validated_products') or matched_list

        from search.specs_extractor import compute_marketplace_statuses
        url_mp_status = compute_marketplace_statuses(matches, comp_result.get('platform_status', {}))

        from services.price_prediction_service import get_marketplace_7d_predictions, record_search_prices_to_history
        url_prices = {}
        for p_k, p_it in (best_per_plat or matches).items():
            if isinstance(p_it, dict) and (p_it.get('price_num') or p_it.get('price')):
                try:
                    url_prices[p_k.lower()] = float(p_it.get('price_num') or p_it.get('price'))
                except Exception:
                    pass
        try:
            record_search_prices_to_history(p_name, best_per_plat or matches)
        except Exception:
            pass
        url_price_prediction = get_marketplace_7d_predictions(query=p_name, current_prices=url_prices)

        return render_template(
            'results.html',
            is_url_search=True,
            price_prediction=url_price_prediction,
            searched_url=url_param,
            source_platform=comp_result.get('source_platform'),
            source_platform_name=comp_result.get('source_platform_name'),
            source_product=src_prod,
            query=p_name,
            sort_by='best_match',
            results_for_view=val_prods,
            all_results=val_prods,
            validated_products=val_prods,
            total=len(val_prods),
            platforms=platforms_payload,
            platform_results={"Amazon": [matches['amazon']] if matches.get('amazon') else [], "Flipkart": [matches['flipkart']] if matches.get('flipkart') else [], "Meesho": [matches['meesho']] if matches.get('meesho') else []},
            platform_status=comp_result.get('platform_status', {}),
            marketplace_status=url_mp_status,
            comparison_data=comparison,
            specifications_matrix=comp_result.get('specifications_matrix', []),
            quality_comparison_table=comp_result.get('quality_comparison_table', []),
            comparison_summary=comp_result.get('comparison_summary', {}),
            best_deal=comp_result.get('best_deal'),
            best_overall_deal=comp_result.get('best_deal'),
            best_per_platform=best_per_plat,
            overall_best=comp_result.get('best_deal'),
            ai_summary="",
            top_verified_offers=top_verified_offers,
            top_prices_data=top_verified_offers,
            top_prices=top_verified_offers,
            savings_info=comp_result.get('savings_info', {}),
            detected_category=src_prod.get('category', 'General'),
            matching=comp_result.get('matching', {}),
            match_pairs=comp_result.get('match_pairs', {}),
            no_deal_message=comp_result.get('no_deal_message'),
            has_cross_platform_match=comp_result.get('has_cross_platform_match', False),
            similar_products=comp_result.get('similar_products', {}),
            scraped_at=comp_result.get('scraped_at'),
            sort_views={'best_match': val_prods},
            dynamic_filters={},
            filter_params={}
        )

    raw_q = request.args.get('q', '')
    query = clean_text(raw_q)[:100]


    price_range = request.args.get('price_range', '').strip()
    min_rating = request.args.get('min_rating', '').strip()
    sort_by = request.args.get('sort', 'best_match').strip()

    category_val = request.args.get('category', '').strip()
    specs_list = []
    for k in ('brand', 'ram', 'storage', 'processor', 'appliance_type', 'type'):
        if request.args.get(k):
            specs_list.append(f"{k.capitalize()}: {request.args.get(k)}")
    specs_str = ", ".join(specs_list)

    from search.normalizer import build_search_query_chain
    from search_engine import fetch_all_products_with_fallbacks

    query_chain = build_search_query_chain(
        brand=request.args.get('brand', ''),
        category=category_val,
        ram=request.args.get('ram', ''),
        storage=request.args.get('storage', ''),
        processor=request.args.get('processor', ''),
        appliance_type=request.args.get('appliance_type', ''),
        type_val=request.args.get('type', ''),
        q=raw_q,
    )

    query = query_chain[0] if query_chain else (query or 'Products')

    if not query:
        return redirect('/')

    # Log search for global autocomplete history
    log_search_query(query)

    # Save search URL in session if unauthenticated for seamless post-login restoration
    if 'user_id' not in session:
        session['pending_search_url'] = request.full_path

    filter_params = parse_filter_params(request.args)
    fresh = request.args.get('fresh', '0').lower() in ('1', 'true', 'yes')
    cache_key = f"{normalize_query(query)}|{json.dumps(filter_params, sort_keys=True)}"

    # Check cache unless fresh=1 is specified
    cached_data = get_cached_search(cache_key, bypass_fresh=fresh)
    if cached_data:
        processed = cached_data
    else:
        # Execute parallel search with fallback loop
        raw_results, platform_status = fetch_all_products_with_fallbacks(query_chain, bypass_fresh=fresh)
        processed = process_results(query, raw_results, filter_params, platform_status)
        set_cached_search(cache_key, processed)

    # Log user search and send automatic inbox notifications if logged in
    user_id = session.get('user_id')
    if user_id:
        total_count = len(processed.get('all_results', []))
        platform_list = [p.capitalize() for p, items in processed.get('platform_results', {}).items() if isinstance(items, list) and len(items) > 0]
        platforms_str = ", ".join(platform_list) or "Amazon, Flipkart, Meesho"
        
        specs_dict = {}
        for k in ('brand', 'ram', 'storage', 'processor', 'appliance_type', 'type'):
            if request.args.get(k):
                specs_dict[k] = request.args.get(k)
        if not specs_dict and specs_str:
            specs_dict = {"details": specs_str}

        best_deal = processed.get('best_overall_deal') or processed.get('best_deal') or processed.get('comparison_data', {}).get('best_deal')
        b_plat = ""
        b_price = None
        b_title = ""
        b_url = ""
        if isinstance(best_deal, dict):
            b_plat = best_deal.get('platform') or ""
            b_price = best_deal.get('price_num') or best_deal.get('price')
            b_title = best_deal.get('title') or best_deal.get('product_name') or ""
            b_url = best_deal.get('link') or best_deal.get('product_url') or ""

        # Fallback to lowest-priced available product if best_deal did not specify platform/price
        if not b_plat or not b_price:
            all_prods = list(processed.get('all_results') or [])
            if not all_prods:
                pr = processed.get('platform_results') or {}
                if isinstance(pr, dict):
                    for p_items in pr.values():
                        if isinstance(p_items, list):
                            all_prods.extend(p_items)
            valid_candidates = []
            for it in all_prods:
                if isinstance(it, dict):
                    p_val = it.get('price_num')
                    if not p_val and it.get('price'):
                        try:
                            clean_str = re.sub(r'[^\d.]', '', str(it.get('price')))
                            if clean_str:
                                p_val = int(float(clean_str))
                        except Exception:
                            p_val = None
                    if p_val and p_val > 0:
                        valid_candidates.append((p_val, it))
            if valid_candidates:
                valid_candidates.sort(key=lambda x: x[0])
                lowest_p, best_cand = valid_candidates[0]
                if not b_plat:
                    b_plat = (best_cand.get('platform') or "").capitalize()
                if not b_price:
                    b_price = lowest_p
                if not b_title:
                    b_title = best_cand.get('title') or best_cand.get('product_name') or ""
                if not b_url:
                    b_url = best_cand.get('link') or best_cand.get('product_url') or ""

        # Fallback to catalog if still empty
        if not b_plat or not b_price:
            from database import CATALOG_DEALS
            q_low = query.lower()
            for k, d in CATALOG_DEALS.items():
                if k in q_low:
                    if not b_plat:
                        b_plat = d['platform']
                    if not b_price:
                        b_price = d['price']
                    break

        best_q_cand = processed.get('comparison_summary', {}).get('best_quality') if isinstance(processed.get('comparison_summary'), dict) else None
        q_score_log = best_q_cand.get('quality_score') if best_q_cand else None
        d_conf_log = best_q_cand.get('data_confidence') if best_q_cand else None

        search_id = log_user_search(
            user_id, query, category_val or "General",
            specifications=specs_dict, search_query=query,
            num_results=total_count, platforms_found=platforms_str,
            best_platform=b_plat, best_price=b_price,
            best_product_title=b_title, best_product_url=b_url,
            best_deal=best_deal, platform_results=processed.get('platform_results'),
            quality_score=q_score_log, data_confidence=d_conf_log
        )

        # Automatic inbox message: Search Completed
        create_inbox_message(
            user_id,
            'search',
            'Search Completed 🔔',
            f"Your search for '{query}' has been completed successfully. Found {total_count} products across {platforms_str}.",
            search_id
        )

        # Automatic inbox message: Best Deal Found
        if best_deal and (best_deal.get('formatted_price') or best_deal.get('price')):
            f_price = best_deal.get('formatted_price') or best_deal.get('price')
            create_inbox_message(
                user_id,
                'best_deal',
                'Best Deal Found 🏆',
                f"The lowest price for '{query}' is ₹{f_price} on {best_deal.get('platform', 'SmartBuy').capitalize()}.",
                search_id
            )

    from services.price_prediction_service import get_marketplace_7d_predictions
    curr_prices = {}
    b_per_p = processed.get('best_per_platform') or {}
    for p_k, p_it in b_per_p.items():
        if isinstance(p_it, dict) and (p_it.get('price_num') or p_it.get('price')):
            try:
                curr_prices[p_k.lower()] = float(p_it.get('price_num') or p_it.get('price'))
            except Exception:
                pass
    keyword_price_prediction = get_marketplace_7d_predictions(query=query, current_prices=curr_prices)

    render_data = {
        "query": query,
        "filter_params": filter_params,
        "price_range": price_range,
        "min_rating": min_rating,
        "price_prediction": keyword_price_prediction,
        **processed
    }

    results_for_view = processed['sort_views'].get(sort_by, processed['sort_views']['best_match'])
    return render_template('results.html', sort_by=sort_by,
                           results_for_view=results_for_view, **render_data)


@api_bp.route('/api/search', methods=['GET', 'POST'])
def api_search():
    """
    Unified JSON API endpoint supporting both product name and product URL searches.
    POST /api/search accepts JSON payload: {"query": "..."}
    GET /api/search accepts query parameter: ?q=... or ?url=...
    """
    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
        query_input = data.get('query') or data.get('q') or data.get('url') or ''
        if not query_input:
            query_input = request.form.get('query', '') or request.form.get('q', '') or request.form.get('url', '')
        query_input = str(query_input).strip()
        if not query_input:
            return jsonify({"error": "Field 'query' is required", "status": "error"}), 400

        fresh = bool(data.get('fresh', False)) or request.args.get('fresh', '0').lower() in ('1', 'true', 'yes')
        filters = data.get('filters')
        routed = route_search(query_input, bypass_fresh=fresh, filters=filters)
        return jsonify(routed), (200 if routed.get('success', True) else 400)

    raw_q = request.args.get('q', '').strip()
    raw_url = request.args.get('url', '').strip()
    target = raw_url or raw_q

    if not target:
        return jsonify({"error": "Query parameter 'q' or 'url' is required", "status": "error"}), 400

    detection = detect_search_type(target)
    if detection.get('type') == 'product_url':
        fresh = request.args.get('fresh', '0').lower() in ('1', 'true', 'yes')
        routed = route_search(detection.get('url') or target, bypass_fresh=fresh)
        return jsonify(routed), (200 if routed.get('success', True) else 400)

    query = clean_text(raw_q)[:100]

    fresh = request.args.get('fresh', '0').lower() in ('1', 'true', 'yes')
    raw_results, platform_status = fetch_all_products_parallel(query, bypass_fresh=fresh)
    processed = process_results(query, raw_results, platform_status=platform_status)
    comp = processed.get("comparison_data", {})

    return jsonify({
        "status": "success",
        "query": query,
        "amazon": {
            "source": platform_status.get("Amazon", {}).get("source", "live"),
            "status": platform_status.get("Amazon", {}).get("status", "success"),
            "count": platform_status.get("Amazon", {}).get("count", len(processed["platform_results"].get("Amazon", []))),
            "duration": platform_status.get("Amazon", {}).get("duration", 0.0),
            "error": platform_status.get("Amazon", {}).get("error"),
            "products": processed["platform_results"].get("Amazon", [])
        },
        "flipkart": {
            "source": platform_status.get("Flipkart", {}).get("source", "live"),
            "status": platform_status.get("Flipkart", {}).get("status", "success"),
            "count": platform_status.get("Flipkart", {}).get("count", len(processed["platform_results"].get("Flipkart", []))),
            "duration": platform_status.get("Flipkart", {}).get("duration", 0.0),
            "error": platform_status.get("Flipkart", {}).get("error"),
            "products": processed["platform_results"].get("Flipkart", [])
        },
        "meesho": {
            "source": platform_status.get("Meesho", {}).get("source", "live"),
            "status": platform_status.get("Meesho", {}).get("status", "success"),
            "count": platform_status.get("Meesho", {}).get("count", len(processed["platform_results"].get("Meesho", []))),
            "duration": platform_status.get("Meesho", {}).get("duration", 0.0),
            "error": platform_status.get("Meesho", {}).get("error"),
            "products": processed["platform_results"].get("Meesho", [])
        },
        "platforms": processed.get("platforms", {}),
        "comparison": processed.get("comparison", comp),
        "specification_table": comp.get("specification_table") or processed.get("specification_table", {}),
        "best_deal": comp.get("best_deal"),
        "match_pairs": comp.get("match_pairs", {}),
        "marketplace_status": processed.get("marketplace_status", {}),
        "quality_comparison_table": processed.get("quality_comparison_table", comp.get("quality_comparison_table", [])),
        "comparison_summary": processed.get("comparison_summary", comp.get("comparison_summary", {})),
        # Legacy compatibility keys
        "platform_status": platform_status,
        "results": processed["platform_results"],
        "all_results": processed["all_results"],
        "best_per_platform": processed["best_per_platform"],
        "overall_best": processed["overall_best"],
        "ai_summary": processed["ai_summary"],
        "top_prices_data": processed.get("top_prices_data"),
        "top_prices": processed.get("top_prices"),
        "best_overall_deal": processed.get("best_overall_deal"),
        "savings_info": processed.get("savings_info"),
    })


@api_bp.route('/api/compare-url', methods=['POST'])
def api_compare_url():
    """
    JSON API endpoint for Search by Product URL:
    Request: {"url": "USER_PRODUCT_URL"}
    Response schema required:
    {
      "source_platform": "...",
      "source_product": {...},
      "matches": {
        "amazon": {...},
        "flipkart": {...},
        "meesho": {...}
      },
      "matching": {...},
      "best_deal": {...},
      "scraped_at": "..."
    }
    """
    data = request.get_json(silent=True) or {}
    url = (data.get('url') or request.form.get('url', '')).strip()

    if not url:
        return jsonify({"status": "error", "message": "Product URL is required"}), 400

    fresh = request.args.get('fresh', '0').lower() in ('1', 'true', 'yes') or bool(data.get('fresh', False))
    result = compare_by_product_url(url, fresh=fresh)

    if not result.get("success"):
        return jsonify({
            "status": "error",
            "message": result.get("error", "Unable to retrieve or compare product from the provided URL")
        }), 400

    return jsonify({
        "status": "success",
        "source_platform": result["source_platform"],
        "source_platform_name": result["source_platform_name"],
        "source_url": result["source_url"],
        "source_product": result["source_product"],
        "matches": result["matches"],
        "similar_products": result.get("similar_products", {}),
        "matching": result["matching"],
        "best_deal": result["best_deal"],
        "platform_status": result.get("platform_status", {}),
        "specifications_matrix": result.get("specifications_matrix", []),
        "scraped_at": result["scraped_at"]
    }), 200


@api_bp.route('/api/compare-url/stream', methods=['GET'])
def api_compare_url_stream():
    """
    Server-Sent Events (SSE) stream for live step-by-step progress:
    Emits actual progress:
    - URL detected
    - Source product extracted
    - Amazon checked
    - Flipkart checked
    - Meesho checked
    - Products matched
    - Best deal calculated
    """
    url = request.args.get('url', '').strip()
    if not url:
        return jsonify({"status": "error", "message": "Query parameter 'url' is required"}), 400

    fresh = request.args.get('fresh', '0').lower() in ('1', 'true', 'yes')

    def generate():
        gen = compare_by_product_url_stream(url, fresh=fresh)
        final_res = None
        try:
            for event in gen:
                yield f"data: {json.dumps(event)}\n\n"
        except StopIteration as e:
            final_res = e.value
        except Exception as exc:
            yield f"data: {json.dumps({'status': 'error', 'message': str(exc)})}\n\n"
            return

        if final_res:
            yield f"data: {json.dumps({'step_id': 'complete', 'result': final_res})}\n\n"

    return Response(stream_with_context(generate()), mimetype='text/event-stream', headers={
        'Cache-Control': 'no-cache',
        'X-Accel-Buffering': 'no'
    })


@api_bp.route('/api/search-suggestions', methods=['GET'])

@api_bp.route('/autocomplete', methods=['GET'])
@api_bp.route('/api/autocomplete', methods=['GET'])
def search_suggestions():
    """
    Intelligent Search Suggestions API:
    - Returns product suggestions from ALL categories (Electronics, Appliances, Kitchen,
      Groceries, Personal Care, Beauty, Fashion, Baby, Pet, Stationery, Sports, Furniture, Books, etc.)
    - Supports prefix matching, token matching, RapidFuzz fuzzy search, and typo correction.
    - Displays 10–15 relevant suggestions.
    """
    q = request.args.get('q', '').strip()
    if not q or len(q) < 2:
        return jsonify({"status": "success", "query": q, "suggestions": []})

    trie = get_current_trie()
    suggestions = get_autocomplete_suggestions(trie, q, limit=12)
    return jsonify({
        "status": "success",
        "query": q,
        "suggestions": suggestions
    })



@api_bp.route('/health', methods=['GET'])
@api_bp.route('/api/health', methods=['GET'])
def health_check():
    """System and provider health check endpoint."""
    from scrapers.meesho.meesho_provider import MeeshoProvider
    from scrapers.amazon import AmazonProvider
    from scrapers.flipkart import FlipkartProvider

    try:
        mee_health = MeeshoProvider().health_check()
    except Exception as e:
        mee_health = {"platform": "meesho", "status": "unavailable", "error": str(e)}

    try:
        amz_health = AmazonProvider().health_check()
    except Exception as e:
        amz_health = {"platform": "amazon", "status": "unavailable", "error": str(e)}

    try:
        fk_health = FlipkartProvider().health_check()
    except Exception as e:
        fk_health = {"platform": "flipkart", "status": "unavailable", "error": str(e)}

    return jsonify({
        "status": "online",
        "system": "Smart-Buy: Multiplatform Price Review Comparison System",
        "timestamp": time.time(),
        "database_queries_logged": get_query_count(),
        "providers": {
            "amazon": amz_health,
            "flipkart": fk_health,
            "meesho": mee_health
        }
    })


@api_bp.route('/api/debug/scrapers', methods=['GET'])
def debug_scrapers():
    """Mandatory debug endpoint: validates API, network, scraper, parser, and matching."""
    raw_q = request.args.get('query', '') or request.args.get('q', 'pilgrim face wash')
    query = clean_text(raw_q)[:100]

    from scrapers.meesho.meesho_provider import MeeshoProvider
    from scrapers.amazon import AmazonProvider
    from scrapers.flipkart import FlipkartProvider

    from concurrent.futures import ThreadPoolExecutor

    start = time.time()
    with ThreadPoolExecutor(max_workers=3) as executor:
        f_mee = executor.submit(MeeshoProvider().search_products, query)
        f_amz = executor.submit(AmazonProvider().search_products, query)
        f_fk = executor.submit(FlipkartProvider().search_products, query)

        mee_res = f_mee.result()
        amz_res = f_amz.result()
        fk_res = f_fk.result()

    return jsonify({
        "query": query,
        "duration": round(time.time() - start, 2),
        "amazon": {
            "status": amz_res.get("status"),
            "count": len(amz_res.get("products", [])),
            "error": amz_res.get("error"),
            "sample_products": [p.get("title") for p in amz_res.get("products", [])[:3]]
        },
        "flipkart": {
            "status": fk_res.get("status"),
            "count": len(fk_res.get("products", [])),
            "error": fk_res.get("error"),
            "sample_products": [p.get("title") for p in fk_res.get("products", [])[:3]]
        },
        "meesho": {
            "status": mee_res.get("status"),
            "count": len(mee_res.get("products", [])),
            "error": mee_res.get("error"),
            "sample_products": [p.get("title") for p in mee_res.get("products", [])[:3]]
        }
    })


# ════════════════════ ADMIN PORTAL & SMARTBUY ADMIN INBOX ════════════════════

@api_bp.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    """Admin Portal Sign In."""
    if 'user_id' in session:
        user = get_user_by_id(session['user_id'])
        if user and user.get('is_admin'):
            return redirect(url_for('api.admin_dashboard'))

    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        user = get_user_by_email(email)
        if user and user.get('is_admin') and check_password_hash(user.get('password_hash', ''), password):
            u_id = str(user.get('id') or user.get('_id', ''))
            u_name = user.get('name', 'Admin')
            session['user_id'] = u_id
            session['user_name'] = u_name
            session['is_admin'] = 1
            update_user_last_login(u_id)
            flash("Welcome to the SmartBuy Admin Portal!", "success")
            return redirect(url_for('api.admin_dashboard'))
        else:
            error = "Invalid Administrator credentials."
            return render_template('admin_login.html', error=error, email=email)

    return render_template('admin_login.html')


@api_bp.route('/admin/dashboard', methods=['GET'])
@admin_required
def admin_dashboard():
    """Admin Dashboard overview."""
    users = get_all_users_for_admin()
    notifications = get_admin_inbox_notifications(limit=10)
    query_count = get_query_count()
    unread_admin = get_admin_unread_count()
    return render_template('admin_dashboard.html',
                           users=users,
                           notifications=notifications,
                           total_users=len(users),
                           total_queries=query_count,
                           unread_admin=unread_admin)


@api_bp.route('/admin/inbox', methods=['GET'])
@admin_required
def admin_inbox():
    """SmartBuy Admin Inbox displaying all system notifications."""
    notifications = get_admin_inbox_notifications(limit=100)
    return render_template('admin_inbox.html', notifications=notifications)


@api_bp.route('/admin/inbox/read/<notification_id>', methods=['POST'])
@admin_required
def admin_inbox_read(notification_id):
    """Mark an admin notification as read."""
    mark_admin_notification_read(notification_id)
    return redirect(url_for('api.admin_inbox'))


@api_bp.route('/admin/inbox/delete/<notification_id>', methods=['POST'])
@admin_required
def admin_inbox_delete(notification_id):
    """Delete an admin notification."""
    delete_admin_notification(notification_id)
    flash("Notification removed.", "info")
    return redirect(url_for('api.admin_inbox'))


@api_bp.route('/admin/users', methods=['GET'])
@admin_required
def admin_users():
    """Admin view listing all registered users."""
    users = get_all_users_for_admin()
    return render_template('admin_users.html', users=users)


@api_bp.route('/admin/user/<user_id>', methods=['GET'])
@admin_required
def admin_user_detail(user_id):
    """Admin Detailed User Profile view."""
    user_detail = get_user_full_details_for_admin(user_id)
    if not user_detail:
        flash("User not found.", "danger")
        return redirect(url_for('api.admin_dashboard'))
    return render_template('admin_user_detail.html', user_detail=user_detail)


# ════════════════════════════════════════════════════════════════════
# PRICE ALERT SYSTEM ROUTE HANDLERS
# ════════════════════════════════════════════════════════════════════

def _resolve_price_alert_identity():
    """Helper to extract user identity from session, headers, or query parameters."""
    uid = session.get('user_id') or request.headers.get('X-User-ID') or request.args.get('user_id')
    user_email = None
    if uid:
        user_record = get_user_by_id(uid)
        if user_record:
            user_email = user_record.get('email')
    if not user_email:
        user_email = (
            request.headers.get('X-User-Email')
            or request.args.get('email')
            or (session.get('user') and session['user'].get('email'))
        )
    if user_email:
        user_email = str(user_email).strip().lower()
    return uid, user_email


@api_bp.route('/price-alert', methods=['POST'])
@api_bp.route('/api/price-alerts', methods=['POST'])
@api_bp.route('/api/price-alert', methods=['POST'])
def create_price_alert_route():
    """
    POST /price-alert
    Create a new Price Alert in MongoDB.
    Accepts JSON payload or Form data containing:
    - product_name (str)
    - product_url (str)
    - platform (str: 'amazon', 'flipkart', 'meesho')
    - current_price (number or string e.g. 27000 or ₹27,000)
    - target_price (number or string e.g. 25000 or ₹25,000)
    - email (str)
    - user_id (optional str)
    - product_image (optional str)
    """
    if request.is_json:
        data = request.get_json(silent=True) or {}
    else:
        data = request.form.to_dict() or {}

    uid, auth_email = _resolve_price_alert_identity()
    user_id = data.get('user_id') or uid
    email = data.get('email') or auth_email

    product_name = data.get('product_name') or data.get('productName') or ''
    product_url = data.get('product_url') or data.get('productUrl') or ''
    platform = data.get('platform') or ''
    current_price = data.get('current_price') or data.get('currentPrice') or 0
    target_price = data.get('target_price') or data.get('targetPrice') or 0
    product_image = data.get('product_image') or data.get('productImage') or ''

    alert_id, code, msg = create_price_alert(
        user_id=user_id,
        email=email,
        product_name=product_name,
        product_url=product_url,
        platform=platform,
        current_price=current_price,
        target_price=target_price,
        product_image=product_image
    )

    if not alert_id or code >= 400:
        if request.is_json:
            return jsonify({"success": False, "message": msg}), code
        flash(msg, "danger")
        return redirect(request.referrer or url_for('api.my_price_alerts_page'))

    saved_doc = get_price_alert_by_id(alert_id)
    if request.is_json:
        return jsonify({
            "success": True,
            "message": msg,
            "alert_id": alert_id,
            "alert": saved_doc
        }), code

    flash("Price alert set successfully! We will email you once the price drops.", "success")
    return redirect(url_for('api.my_price_alerts_page'))


@api_bp.route('/price-alerts', methods=['GET'])
def get_user_price_alerts_route():
    """
    GET /price-alerts
    Retrieve all price alerts for the authenticated user.
    Users can only access their own alerts.
    """
    uid, uemail = _resolve_price_alert_identity()
    if not uid and not uemail:
        return jsonify({
            "success": False,
            "message": "Authentication required. Sign in or provide valid user credentials to view alerts."
        }), 401

    status_filter = request.args.get('status')
    alerts = get_user_price_alerts(
        user_id=uid,
        email=uemail,
        status_filter=status_filter,
        include_deleted=False
    )

    return jsonify({
        "success": True,
        "count": len(alerts),
        "alerts": alerts
    }), 200


@api_bp.route('/price-alert/<alert_id>/pause', methods=['PUT', 'POST'])
def pause_price_alert_route(alert_id):
    """PUT or POST /price-alert/{id}/pause - Pause price alert monitoring."""
    uid, uemail = _resolve_price_alert_identity()
    ok, code, msg = pause_price_alert(alert_id=alert_id, user_id=uid, email=uemail)
    if request.is_json or request.headers.get('Accept') == 'application/json':
        return jsonify({"success": ok, "message": msg, "alert_id": alert_id, "status": "paused"}), code
    flash(msg, "info" if ok else "danger")
    return redirect(request.referrer or url_for('api.my_price_alerts_page'))


@api_bp.route('/price-alert/<alert_id>/resume', methods=['PUT', 'POST'])
def resume_price_alert_route(alert_id):
    """PUT or POST /price-alert/{id}/resume - Resume paused price alert monitoring."""
    uid, uemail = _resolve_price_alert_identity()
    ok, code, msg = resume_price_alert(alert_id=alert_id, user_id=uid, email=uemail)
    if request.is_json or request.headers.get('Accept') == 'application/json':
        return jsonify({"success": ok, "message": msg, "alert_id": alert_id, "status": "active"}), code
    flash(msg, "success" if ok else "danger")
    return redirect(request.referrer or url_for('api.my_price_alerts_page'))


@api_bp.route('/price-alert/<alert_id>/reactivate', methods=['PUT', 'POST'])
def reactivate_price_alert_route(alert_id):
    """
    PUT or POST /price-alert/{id}/reactivate
    Reactivate a triggered or paused price alert so monitoring starts again.
    """
    uid, uemail = _resolve_price_alert_identity()
    ok, code, msg = reactivate_price_alert(alert_id=alert_id, user_id=uid, email=uemail)
    if request.is_json or request.headers.get('Accept') == 'application/json':
        return jsonify({"success": ok, "message": msg, "alert_id": alert_id, "status": "active"}), code
    flash(msg, "success" if ok else "danger")
    return redirect(request.referrer or url_for('api.my_price_alerts_page'))


@api_bp.route('/price-alert/<alert_id>', methods=['DELETE'])
@api_bp.route('/price-alert/<alert_id>/delete', methods=['POST', 'DELETE'])
def delete_price_alert_route(alert_id):
    """DELETE /price-alert/{id} or POST /price-alert/{id}/delete - Safely delete a price alert."""
    uid, uemail = _resolve_price_alert_identity()
    ok, code, msg = delete_price_alert(alert_id=alert_id, user_id=uid, email=uemail)
    if request.is_json or request.headers.get('Accept') == 'application/json':
        return jsonify({"success": ok, "message": msg, "alert_id": alert_id, "status": "deleted"}), code
    flash(msg, "info" if ok else "danger")
    return redirect(request.referrer or url_for('api.my_price_alerts_page'))


@api_bp.route('/price-alert/<alert_id>/check', methods=['POST'])
def check_single_price_alert_route(alert_id):
    """POST /price-alert/{id}/check - Trigger on-demand check for a single alert against live scraper."""
    uid, uemail = _resolve_price_alert_identity()
    doc = get_price_alert_by_id(alert_id)
    if not doc:
        return jsonify({"success": False, "message": "Price alert not found"}), 404

    # Verify ownership
    if uid and str(doc.get('user_id') or '') != str(uid) and doc.get('email') != uemail:
        return jsonify({"success": False, "message": "Unauthorized"}), 403

    res = run_price_check_now(alert_id=alert_id)
    return jsonify({"success": True, "result": res}), 200


@api_bp.route('/price-alerts/check-now', methods=['POST'])
def check_all_price_alerts_route():
    """POST /price-alerts/check-now - Trigger background price checker on all active alerts."""
    summary = check_all_active_alerts(send_notifications=True)
    return jsonify({"success": True, "summary": summary}), 200


@api_bp.route('/my-price-alerts', methods=['GET'])
@login_required
def my_price_alerts_page():
    """
    GET /my-price-alerts
    Render user's dedicated Price Alerts management page with pause, resume,
    delete, reactivate, and live price monitoring status.
    """
    user_id = session.get('user_id')
    user = get_user_by_id(user_id) if user_id else None
    user_email = user.get('email') if user else None

    alerts = get_user_price_alerts(user_id=user_id, email=user_email, include_deleted=False)

    total_alerts = len(alerts)
    active_alerts = len([a for a in alerts if a.get('status') == 'active'])
    triggered_alerts = len([a for a in alerts if a.get('status') == 'triggered'])
    paused_alerts = len([a for a in alerts if a.get('status') == 'paused'])
    total_savings = sum(a.get('savings', 0) for a in alerts if a.get('status') == 'triggered')

    return render_template(
        'my_price_alerts.html',
        alerts=alerts,
        total_alerts=total_alerts,
        active_alerts=active_alerts,
        triggered_alerts=triggered_alerts,
        paused_alerts=paused_alerts,
        total_savings=total_savings,
        current_user=user
    )


# ════════════════════════════════════════════════════════════════════════════
# GEC ECom Mock Store & Interactive Price Drop Simulator Routes
# ════════════════════════════════════════════════════════════════════════════

@api_bp.route('/gececom', methods=['GET'])
def gececom_storefront():
    """
    GET /gececom
    Render GEC ECom storefront with live product inventory and real-time
    interactive price drop simulator.
    """
    cat = request.args.get('category', 'all')
    products = get_gececom_products(category=cat)
    user_id = session.get('user_id')
    user = get_user_by_id(user_id) if user_id else None
    return render_template('gececom.html', products=products, current_user=user)


@api_bp.route('/api/gececom/products', methods=['GET'])
def get_gececom_products_api():
    """GET /api/gececom/products - Retrieve all GEC ECom products in JSON."""
    cat = request.args.get('category')
    products = get_gececom_products(category=cat)
    return jsonify({"success": True, "count": len(products), "products": products}), 200


@api_bp.route('/api/gececom/products', methods=['POST'])
def create_gececom_product_api():
    """POST /api/gececom/products - Add product to GEC ECom catalog."""
    data = request.get_json(silent=True) or request.form.to_dict() or {}
    pid, code, msg = create_gececom_product(data)
    if not pid or code >= 400:
        return jsonify({"success": False, "message": msg}), code

    prod = get_gececom_product_by_id(pid)
    return jsonify({"success": True, "message": msg, "product": prod}), 201


@api_bp.route('/api/gececom/products/<product_id>/price', methods=['PUT', 'POST'])
def update_gececom_product_price_api(product_id):
    """
    PUT /api/gececom/products/{id}/price
    Update selling price of a product in GEC ECom catalog.
    If trigger_check is True, checks active price alerts matching this item,
    dispatches email alerts, creates user inbox messages, and returns trigger summary.
    """
    data = request.get_json(silent=True) or request.form.to_dict() or {}
    new_price = int(data.get('new_price') or data.get('price') or 0)
    trigger_check = bool(data.get('trigger_check', True))

    if new_price <= 0:
        return jsonify({"success": False, "message": "Price must be greater than ₹0."}), 400

    ok, updated_doc, msg = update_gececom_product_price(product_id, new_price)
    if not ok or not updated_doc:
        return jsonify({"success": False, "message": msg}), 400

    trigger_results = {"evaluated": 0, "triggered": 0}
    if trigger_check:
        try:
            trigger_results = trigger_gececom_price_alerts_for_product(
                product_id=product_id,
                product_name=updated_doc.get("product_name", ""),
                new_price=new_price,
                product_url=f"/gececom#product-{product_id}",
                product_image=updated_doc.get("product_image", "")
            )
        except Exception as e:
            logger.warning(f"[GecEcom] Error auto-triggering alerts: {e}")

    return jsonify({
        "success": True,
        "message": msg,
        "product": updated_doc,
        "formatted_new_price": updated_doc.get("formatted_current_price", f"₹{new_price:,}"),
        "new_price": new_price,
        "trigger_results": trigger_results
    }), 200


@api_bp.route('/api/gececom/products/<product_id>', methods=['DELETE', 'POST'])
def delete_gececom_product_api(product_id):
    """DELETE /api/gececom/products/{id} - Remove product from GEC ECom catalog."""
    ok, msg = delete_gececom_product(product_id)
    return jsonify({"success": ok, "message": msg}), (200 if ok else 400)


# ════════════════════ AI PRICE PREDICTION ENDPOINT ════════════════════

@api_bp.route('/price-prediction', methods=['GET'])
@api_bp.route('/api/price-prediction', methods=['GET'])
def api_price_prediction():
    """
    Retrieve 7-day Linear Regression AI price prediction separately for
    Amazon, Flipkart, Meesho, and GEC E-Commerce based on historical MongoDB price observations.
    """
    q = request.args.get('query', request.args.get('q', request.args.get('product_name', ''))).strip()
    if not q:
        return jsonify({"success": False, "message": "'query' parameter is required."}), 400

    from services.price_prediction_service import get_marketplace_7d_predictions
    platform = request.args.get('platform', '').strip().lower()
    predictions = get_marketplace_7d_predictions(query=q)

    if platform and platform in predictions.get("marketplaces", {}):
        predictions["marketplaces"] = {platform: predictions["marketplaces"][platform]}

    return jsonify(predictions), 200


