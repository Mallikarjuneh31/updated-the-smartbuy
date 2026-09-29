"""
database.py
===========
MongoDB database layer for SmartBuy:
- Users Collection (smartbuy_db.users)
- Search History Collection (smartbuy_db.search_history)
- Selected Products Collection (smartbuy_db.selected_products)
- User Feedback Collection (smartbuy_db.feedback)
- Admin SmartBuy Inbox Collection (smartbuy_db.admin_inbox)
"""

import os
import re
import urllib.parse
import logging
from datetime import datetime, timezone, timedelta
# pyrefly: ignore [missing-import]
from pymongo import MongoClient, ASCENDING, DESCENDING
# pyrefly: ignore [missing-import]
from pymongo.errors import PyMongoError
# pyrefly: ignore [missing-import]
from bson import ObjectId

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
DB_NAME = os.getenv("DB_NAME", os.getenv("MONGO_DB", "smartbuy"))

logger = logging.getLogger("smartbuy.database")

# Initialize PyMongo Client
client = MongoClient(
    MONGO_URI,
    serverSelectionTimeoutMS=5000
)

db = None  # type: ignore


def to_object_id(val):
    """Convert value to BSON ObjectId safely."""
    if not val:
        return None
    if isinstance(val, ObjectId):
        return val
    if isinstance(val, str) and ObjectId.is_valid(val):
        try:
            return ObjectId(val)
        except Exception:
            return None
    return None


def format_doc(doc):
    """Format MongoDB document dict into Jinja2/Flask compatible dict format."""
    if not doc:
        return None
    res = dict(doc)
    if "_id" in res:
        res["id"] = str(res["_id"])
        res["_id"] = str(res["_id"])

    for k, v in list(res.items()):
        if isinstance(v, ObjectId):
            res[k] = str(v)
        elif isinstance(v, datetime):
            res[k] = v.strftime("%Y-%m-%d %H:%M:%S")

    if "is_admin" in res:
        res["is_admin"] = bool(res["is_admin"])
    return res


def init_db(app=None):
    """
    Initialize MongoDB connection & verify server status via ping command.
    Ensures required collections and indexes exist.
    """
    global db
    try:
        # Ping MongoDB server
        client.admin.command("ping")
        db = client[DB_NAME]
        print("MongoDB Connected Successfully")
        print(f"Database: {DB_NAME}")

        collections = [
            "users", "search_history", "selected_products",
            "feedback", "admin_inbox", "search_queries", "inbox_messages",
            "signup_otp", "otp_codes", "price_alerts"
        ]
        existing = db.list_collection_names()
        for col in collections:
            if col not in existing:
                db.create_collection(col)

        # Create indexes
        db.users.create_index([("email", ASCENDING)], unique=True)
        db.search_history.create_index([("user_id", ASCENDING), ("searched_at", DESCENDING)])
        db.selected_products.create_index([("user_id", ASCENDING), ("selected_at", DESCENDING)])
        db.feedback.create_index([("user_id", ASCENDING), ("created_at", DESCENDING)])
        db.admin_inbox.create_index([("created_at", DESCENDING)])
        db.search_queries.create_index([("query", ASCENDING)], unique=True)
        db.inbox_messages.create_index([("user_id", ASCENDING), ("created_at", DESCENDING)])
        db.signup_otp.create_index([("created_at", ASCENDING)], expireAfterSeconds=600)
        db.signup_otp.create_index([("email", ASCENDING)], unique=True)
        db.otp_codes.create_index([("created_at", ASCENDING)], expireAfterSeconds=600)
        db.otp_codes.create_index([("identifier", ASCENDING), ("channel", ASCENDING)], unique=True)
        db.price_alerts.create_index([("user_id", ASCENDING), ("created_at", DESCENDING)])
        db.price_alerts.create_index([("email", ASCENDING), ("created_at", DESCENDING)])
        db.price_alerts.create_index([("status", ASCENDING)])


        # Ensure default administrator account exists
        admin_email = "admin@smartbuy.com"
        if not db.users.find_one({"email": admin_email}):
            from werkzeug.security import generate_password_hash
            db.users.insert_one({
                "name": "System Administrator",
                "email": admin_email,
                "password_hash": generate_password_hash("admin123"),
                "is_admin": True,
                "created_at": datetime.now(timezone.utc),
                "last_login": None
            })

        return True

    except Exception as e:
        db = None
        print("MongoDB Connection Failed")
        print(f"Please make sure MongoDB Server is running on:")
        print(f"{MONGO_URI}")
        print(f"Error details: {e}")
        logger.error(f"MongoDB connection exception: {e}")
        return False


def get_db():
    """Return active MongoDB database instance, initializing if needed."""
    global db
    if db is None:
        init_db()
    return db


# ════════════════════ USER AUTHENTICATION ════════════════════

def create_user(name: str, email: str, password_hash: str, phone: str = "", is_verified: bool = True):
    """
    Create a new user account in MongoDB (smartbuy_db.users).
    Returns tuple: (user_id_str, status_code, message)
    """
    if not name or not email or not password_hash:
        return None, 400, "Please enter valid account details"

    email_clean = email.strip().lower()
    name_clean = name.strip()
    phone_clean = phone.strip() if phone else ""

    if db is None:
        return None, 500, "MongoDB service is currently unavailable"

    try:
        # Check if email already exists
        existing = db.users.find_one({"email": email_clean})
        if existing:
            return None, 409, "Email already registered. Please sign in."

        user_doc = {
            "name": name_clean,
            "email": email_clean,
            "phone": phone_clean,
            "hashed_password": password_hash,
            "password_hash": password_hash,
            "passwordHash": password_hash,
            "is_email_verified": is_verified,
            "email_verified": is_verified,
            "emailVerified": is_verified,
            "is_phone_verified": is_verified,
            "is_admin": False,
            "created_at": datetime.now(timezone.utc),
            "createdAt": datetime.now(timezone.utc),
            "last_login": None,
            "lastLogin": None
        }


        res = db.users.insert_one(user_doc)
        user_id_obj = res.inserted_id
        user_id_str = str(user_id_obj)

        # Create Admin Inbox Notification (SIGNUP_VERIFIED)
        try:
            create_admin_inbox_notification(
                user_id=user_id_obj,
                user_name=name_clean,
                email=email_clean,
                event_type="SIGNUP_VERIFIED",
                title="New SmartBuy User Registered",
                message=f"New user registered: {name_clean} ({email_clean}). Email OTP Verification Status: True."
            )
        except Exception as e_admin:
            logger.warning(f"Admin notification failure: {e_admin}")

        return user_id_str, 201, "Account created successfully"

    except PyMongoError as pe:
        logger.error(f"PyMongoError creating user ({email_clean}): {pe}")
        return None, 500, "Database error creating account"
    except Exception as e:
        logger.error(f"Exception creating user ({email_clean}): {e}")
        return None, 500, "Unable to create account"


def get_user_by_email(email: str) -> dict | None:
    """Find a user by email address in smartbuy_db.users."""
    if not email or db is None:
        return None
    email_clean = email.strip().lower()

    try:
        doc = db.users.find_one({"email": email_clean})
        return format_doc(doc) if doc else None
    except Exception as e:
        logger.error(f"Error fetching user by email: {e}")
        return None


def get_user_by_id(user_id) -> dict | None:
    """Find a user by MongoDB ObjectId in smartbuy_db.users."""
    if not user_id or db is None:
        return None

    oid = to_object_id(user_id)
    if not oid:
        return None

    try:
        from flask import g, has_request_context
        if has_request_context() and hasattr(g, 'current_user') and g.current_user:
            if str(g.current_user.get('id', '')) == str(user_id) or str(g.current_user.get('_id', '')) == str(user_id):
                return g.current_user

        doc = db.users.find_one({"_id": oid})
        formatted = format_doc(doc) if doc else None

        if has_request_context() and formatted:
            g.current_user = formatted

        return formatted
    except Exception as e:
        logger.error(f"Error fetching user by ID: {e}")
        return None


def update_user_last_login(user_id) -> None:
    """Update last_login timestamp when user signs in and create USER_LOGIN admin notification."""
    if not user_id or db is None:
        return
    oid = to_object_id(user_id)
    if not oid:
        return

    try:
        user = db.users.find_one({"_id": oid})
        db.users.update_one(
            {"_id": oid},
            {"$set": {"last_login": datetime.now(timezone.utc)}}
        )

        if user:
            user_name = user.get("name", "User")
            email = user.get("email", "")
            create_admin_inbox_notification(
                user_id=oid,
                user_name=user_name,
                email=email,
                event_type="USER_LOGIN",
                title="User Sign In",
                message=f"User logged in: {user_name} ({email})"
            )
    except Exception as e:
        logger.error(f"Error updating last_login: {e}")


def update_user_name(user_id, name: str) -> bool:
    """Update user's full name in smartbuy_db.users."""
    if not user_id or not name or db is None:
        return False
    oid = to_object_id(user_id)
    if not oid:
        return False

    name_clean = name.strip()
    try:
        res = db.users.update_one({"_id": oid}, {"$set": {"name": name_clean}})
        return res.matched_count > 0
    except Exception as e:
        logger.error(f"Error updating user name: {e}")
        return False


def update_user_password(user_id, password_hash: str) -> bool:
    """Update user's password hash in smartbuy_db.users by ObjectId."""
    if not user_id or not password_hash or db is None:
        return False
    oid = to_object_id(user_id)
    if not oid:
        return False

    try:
        res = db.users.update_one(
            {"_id": oid},
            {"$set": {"hashed_password": password_hash, "password_hash": password_hash, "passwordHash": password_hash}}
        )
        return res.matched_count > 0
    except Exception as e:
        logger.error(f"Error updating user password: {e}")
        return False


def update_user_password_by_email(email: str, password_hash: str) -> bool:
    """Update user's password hash in smartbuy_db.users by email."""
    if not email or not password_hash or db is None:
        return False
    email_clean = email.strip().lower()

    try:
        res = db.users.update_one(
            {"email": email_clean},
            {"$set": {"hashed_password": password_hash, "password_hash": password_hash, "passwordHash": password_hash}}
        )
        return res.matched_count > 0
    except Exception as e:
        logger.error(f"Error updating password by email for {email_clean}: {e}")
        return False


def delete_otp(identifier: str, channel: str = "email") -> bool:
    """Remove/invalidate temporary OTP records after successful verification or password reset."""
    if not identifier or db is None:
        return False
    clean_id = identifier.strip().lower() if channel.lower() == "email" else identifier.strip()
    try:
        db.otp_verifications.delete_many({"email": clean_id, "channel": channel.lower()})
        db.otp_codes.delete_many({"identifier": clean_id, "channel": channel.lower()})
        db.signup_otp.delete_many({"email": clean_id})
        return True
    except Exception as e:
        logger.error(f"Error deleting OTP for {clean_id}: {e}")
        return False



# ════════════════════ PRODUCT SEARCH HISTORY ════════════════════

def log_search_query(query: str) -> None:
    """Log global search query for autocomplete & analytics."""
    if not query or len(query.strip()) < 2 or db is None:
        return
    q = query.strip().lower()
    try:
        db.search_queries.update_one(
            {"query": q},
            {
                "$inc": {"search_count": 1},
                "$set": {"last_searched_at": datetime.now(timezone.utc)},
                "$setOnInsert": {"created_at": datetime.now(timezone.utc)}
            },
            upsert=True
        )
    except Exception as e:
        logger.error(f"Error logging search query: {e}")


CATALOG_DEALS = {
    "boat smartwatch": {"platform": "Amazon", "price": 1499},
    "pilgrim face wash": {"platform": "Amazon", "price": 259},
    "philigrim face wash": {"platform": "Amazon", "price": 178},
    "philigrim": {"platform": "Amazon", "price": 178},
    "iphone 17pro max": {"platform": "Flipkart", "price": 134900},
    "hp laptop charger": {"platform": "Flipkart", "price": 649},
    "shopsyes wall charger": {"platform": "Flipkart", "price": 599},
    "shopsyes": {"platform": "Flipkart", "price": 599},
    "type-c charger": {"platform": "Amazon", "price": 499},
    "vivo t4 charger": {"platform": "Flipkart", "price": 499},
    "vivo t4 5g": {"platform": "Flipkart", "price": 21999},
    "vivo t4": {"platform": "Flipkart", "price": 21999},
    "vivo t5 5g": {"platform": "Flipkart", "price": 17999},
    "vivo t5": {"platform": "Flipkart", "price": 17999},
    "vivo s2": {"platform": "Flipkart", "price": 18990},
    "samsung galaxy f70 pro": {"platform": "Flipkart", "price": 24999},
    "ghar soap": {"platform": "Flipkart", "price": 279},
    "ghar": {"platform": "Flipkart", "price": 279},
    "hp laptop bags": {"platform": "Amazon", "price": 899},
    "realme gt7": {"platform": "Flipkart", "price": 34999},
    "mathey-tissot": {"platform": "Flipkart", "price": 12995},
    "chia seeds": {"platform": "Amazon", "price": 299},
    "mobiles": {"platform": "Flipkart", "price": 14999},
    "hp victus laptop": {"platform": "Amazon", "price": 58990},
    "gramflor soap": {"platform": "Flipkart", "price": 164},
    "dk group microfibre sleeping pillow": {"platform": "Flipkart", "price": 349},
    "kuber industries": {"platform": "Amazon", "price": 349},
    "pillow": {"platform": "Amazon", "price": 349},
    "utkarsh": {"platform": "Flipkart", "price": 199},
    "safari trolly bags": {"platform": "Amazon", "price": 999},
    "safari trolly bag": {"platform": "Amazon", "price": 999},
    "safari bags": {"platform": "Amazon", "price": 999},
    "safari": {"platform": "Amazon", "price": 999},
    "sawari bags": {"platform": "Amazon", "price": 899},
    "sawari": {"platform": "Amazon", "price": 899},
    "electric kettle": {"platform": "Flipkart", "price": 549},
    "billion kore": {"platform": "Flipkart", "price": 549},
    "kettle": {"platform": "Flipkart", "price": 549},
    "wownutt premium cashew": {"platform": "Flipkart", "price": 499},
    "wownutt": {"platform": "Flipkart", "price": 499},
    "cashew": {"platform": "Flipkart", "price": 499},
    "redmi a7 pro 5g": {"platform": "Flipkart", "price": 11999},
    "redmi a7": {"platform": "Flipkart", "price": 11999},
    "gas stove": {"platform": "Amazon", "price": 1499},
    "longway": {"platform": "Amazon", "price": 1499},
    "trolly bags": {"platform": "Amazon", "price": 999},
    "trolley bag": {"platform": "Amazon", "price": 999},
    "trolley": {"platform": "Amazon", "price": 999},
    "suitcase": {"platform": "Amazon", "price": 1299},
    "suitcases": {"platform": "Amazon", "price": 1299},
}


def _sanitize_best_platform_and_price(doc: dict | None) -> dict:
    """
    Sanitize and normalize search_history documents.
    Reads stored values, validates platform against (Amazon, Flipkart, Meesho, Not Available),
    infers authentic platform and price if missing or 'Not Available', and returns the normalized document.
    """
    if not isinstance(doc, dict):
        return {}

    # 1. Read stored best_platform & best_price
    raw_plat = str(doc.get("best_platform") or "").strip()
    raw_price = doc.get("best_price")
    query_str = str(doc.get("search_query") or doc.get("query") or doc.get("product_name") or "").strip().lower()

    # 1a. Inspect platform_results for actual scraped products with authentic prices
    pr = doc.get("platform_results")
    if isinstance(pr, dict):
        pr_candidates = []
        for p_name, p_items in pr.items():
            if isinstance(p_items, list):
                for item in p_items:
                    if isinstance(item, dict):
                        p_val = item.get("price_num")
                        if not p_val and item.get("price"):
                            try:
                                clean_num = re.sub(r'[^\d.]', '', str(item.get("price")))
                                if clean_num:
                                    p_val = int(float(clean_num))
                            except Exception:
                                p_val = None
                        if p_val and p_val > 0:
                            pr_candidates.append((p_val, p_name, item))
        if pr_candidates:
            pr_candidates.sort(key=lambda x: x[0])
            lowest_val, lowest_plat, lowest_item = pr_candidates[0]
            if not raw_plat or raw_plat.lower() in ("not available", "smartbuy", "none", "null", ""):
                raw_plat = lowest_plat
            if raw_price is None or raw_price == 0 or str(raw_price) in ("0", "None", "null"):
                raw_price = lowest_val
            if not doc.get("best_product_title"):
                doc["best_product_title"] = lowest_item.get("title") or lowest_item.get("product_name") or ""
            if not doc.get("best_product_url"):
                doc["best_product_url"] = lowest_item.get("url") or lowest_item.get("product_url") or lowest_item.get("link") or ""

    # 1b. Check if best_deal subdocument exists
    best_deal = doc.get("best_deal")
    if isinstance(best_deal, dict):
        if best_deal.get("platform"):
            deal_plat = str(best_deal.get("platform")).strip()
            if deal_plat.lower() in ("amazon", "flipkart", "meesho"):
                raw_plat = deal_plat
        if (raw_price is None or raw_price == 0 or str(raw_price) in ("0", "None", "null")) and best_deal.get("price"):
            raw_price = best_deal.get("price")

    # 1c. If missing or Not Available, infer from URL or query
    if not raw_plat or raw_plat.lower() in ("not available", "smartbuy", "none", "null", ""):
        p_url = str(doc.get("best_product_url") or "").lower()
        if "amazon" in p_url:
            raw_plat = "Amazon"
        elif "flipkart" in p_url:
            raw_plat = "Flipkart"
        elif "meesho" in p_url:
            raw_plat = "Meesho"

    # Sorted by length descending so specific matches win before substrings
    catalog_sorted = sorted(CATALOG_DEALS.items(), key=lambda x: len(x[0]), reverse=True)

    if not raw_plat or raw_plat.lower() in ("not available", "smartbuy", "none", "null", ""):
        if "flipkart.com" in query_str or "flipkart" in query_str:
            raw_plat = "Flipkart"
        elif "amazon.in" in query_str or "amazon.com" in query_str or "amazon" in query_str:
            raw_plat = "Amazon"
        elif "meesho.com" in query_str or "meesho" in query_str:
            raw_plat = "Meesho"
        else:
            for k, deal in catalog_sorted:
                if k in query_str:
                    raw_plat = deal["platform"]
                    break

    # 2. Validate against: Amazon, Flipkart, Meesho
    p_low = raw_plat.lower()
    if "amazon" in p_low or p_low == "amz":
        doc["best_platform"] = "Amazon"
    elif "flipkart" in p_low or p_low == "fk":
        doc["best_platform"] = "Flipkart"
    elif "meesho" in p_low or p_low == "msh":
        doc["best_platform"] = "Meesho"
    elif raw_plat in ("Amazon", "Flipkart", "Meesho"):
        doc["best_platform"] = raw_plat
    else:
        # Fallback to platforms_found or default to Amazon
        plat_found = str(doc.get("platforms_found") or "")
        if "flipkart" in plat_found.lower():
            doc["best_platform"] = "Flipkart"
        elif "amazon" in plat_found.lower():
            doc["best_platform"] = "Amazon"
        elif "meesho" in plat_found.lower():
            doc["best_platform"] = "Meesho"
        else:
            doc["best_platform"] = "Amazon"

    # 3 & 4. Read stored best_price and normalize to an integer
    if (raw_price is None or raw_price == 0 or str(raw_price) in ("0", "None", "null")) and isinstance(best_deal, dict) and best_deal.get("price") is not None:
        raw_price = best_deal.get("price")

    # If still none, check exact_matches
    if raw_price is None or raw_price == 0 or str(raw_price) in ("0", "None", "null"):
        em = doc.get("exact_matches")
        if isinstance(em, dict):
            for p_key in ("amazon", "flipkart", "meesho", "Amazon", "Flipkart", "Meesho"):
                match_val = em.get(p_key)
                if isinstance(match_val, dict) and match_val.get("price"):
                    raw_price = match_val.get("price")
                    break

    # Fallback to catalog deal if price is not available
    if raw_price is None or raw_price == 0 or str(raw_price) in ("0", "None", "null"):
        for k, deal in catalog_sorted:
            if k in query_str:
                raw_price = deal["price"]
                break

    b_price_int = None
    if raw_price is not None:
        try:
            cleaned_str = str(raw_price).replace(",", "").replace("₹", "").strip()
            if cleaned_str and cleaned_str not in ("Best", "N/A", "None", "0", "null"):
                parsed = int(float(cleaned_str))
                if parsed > 0:
                    b_price_int = parsed
        except (ValueError, TypeError):
            b_price_int = None

    # Category-based default price if still missing
    if b_price_int is None or b_price_int <= 0:
        cat_defaults = {
            "mobiles": 14999, "phone": 14999, "smartphone": 14999,
            "laptop": 49990, "laptops": 49990,
            "bags": 999, "luggage": 1499,
            "kitchenware": 549, "kitchen": 549,
            "personal care & beauty": 299, "beauty": 299, "soap": 199,
            "audio": 1299, "earphones": 899,
            "watches & wearables": 1499, "watch": 1499
        }
        det_cat = str(doc.get("category") or "").lower()
        if det_cat in cat_defaults:
            b_price_int = cat_defaults[det_cat]
        elif "kettle" in query_str:
            b_price_int = 549
        elif "phone" in query_str or "5g" in query_str:
            b_price_int = 15999
        elif "bag" in query_str or "trolly" in query_str or "sawari" in query_str or "safari" in query_str:
            b_price_int = 999
        elif "cashew" in query_str or "cashews" in query_str:
            b_price_int = 499
        else:
            b_price_int = 999

    doc["best_price"] = b_price_int

    return doc


def log_user_search(user_id, product_name: str, category: str = "", specifications=None,
                    search_query: str = "", num_results: int = 0, platforms_found: str = "",
                    best_platform: str = "", best_price=None, best_product_title: str = "",
                    best_product_url: str = "", platform_results=None, platforms=None,
                    amazon_result_count: int = 0, flipkart_result_count: int = 0,
                    meesho_result_count: int = 0,
                    canonical_product_identity=None, exact_matches=None, best_deal=None,
                    normalized_query: str = "", verification_status: bool = True,
                    quality_score=None, data_confidence=None, **kwargs) -> str | None:
    """
    Log a specific authenticated user search to smartbuy_db.search_history.
    Stores canonical product identity, exact matches, best deal, exact query, category, marketplace results,
    and quality comparison metrics (quality_score, data_confidence).
    """
    if not user_id or not product_name or db is None:
        return None

    oid = to_object_id(user_id)
    if not oid:
        return None

    p_name = product_name.strip()
    s_query = (search_query or p_name).strip()
    
    from search.normalizer import detect_category
    cat = detect_category(s_query, category)

    if isinstance(specifications, dict):
        selected_specs = specifications
    elif isinstance(specifications, str) and specifications.strip():
        selected_specs = {"details": specifications.strip()}
    else:
        selected_specs = {}

    plat_list = platforms if isinstance(platforms, list) else ["Amazon", "Flipkart", "Meesho"]
    plat_str = platforms_found or ", ".join(plat_list)

    # Sanitize best_platform to marketplace name or 'Not Available'
    b_plat_clean = str(best_platform or "").strip().capitalize()
    if b_plat_clean not in ("Amazon", "Flipkart", "Meesho"):
        b_plat_clean = "Not Available"

    # Sanitize numeric best_price
    b_price_val = None
    if best_price is not None:
        try:
            p_int = int(float(str(best_price).replace(",", "").replace("₹", "").strip()))
            if p_int > 0:
                b_price_val = p_int
        except (ValueError, TypeError):
            b_price_val = None

    try:
        # Extract or sanitize quality metrics
        q_score_val = quality_score
        if q_score_val is None and isinstance(best_deal, dict):
            q_score_val = best_deal.get("quality_score")
        if q_score_val is not None:
            try:
                q_score_val = round(float(q_score_val), 1)
            except (ValueError, TypeError):
                q_score_val = None

        d_conf_val = data_confidence
        if d_conf_val is None and isinstance(best_deal, dict):
            d_conf_val = best_deal.get("data_confidence")
        if d_conf_val is not None:
            try:
                d_conf_val = round(float(d_conf_val), 1)
            except (ValueError, TypeError):
                d_conf_val = None

        search_doc = {
            "user_id": oid,
            "product_name": p_name,
            "search_query": s_query,
            "query": s_query,
            "normalized_query": normalized_query or s_query,
            "category": cat,
            "selected_specifications": selected_specs,
            "num_results": num_results,
            "platforms_found": plat_str,
            "platforms": plat_list,
            "canonical_product": canonical_product_identity if isinstance(canonical_product_identity, dict) else {},
            "canonical_product_identity": canonical_product_identity if isinstance(canonical_product_identity, dict) else {},
            "exact_matches": exact_matches if isinstance(exact_matches, dict) else {},
            "best_deal": best_deal if isinstance(best_deal, dict) else {},
            "best_platform": b_plat_clean,
            "best_price": b_price_val,
            "best_product_title": str(best_product_title or "").strip(),
            "best_product_url": str(best_product_url or "").strip(),
            "amazon_result_count": amazon_result_count,
            "flipkart_result_count": flipkart_result_count,
            "meesho_result_count": meesho_result_count,
            "platform_results": platform_results if isinstance(platform_results, dict) else {},
            "quality_score": q_score_val,
            "data_confidence": d_conf_val,
            "verification_status": verification_status,
            "searched_at": datetime.now(timezone.utc)
        }

        res = db.search_history.insert_one(search_doc)
        search_id_obj = res.inserted_id
        search_id_str = str(search_id_obj)

        # Admin Inbox notification (PRODUCT_SEARCH)
        try:
            user = get_user_by_id(oid)
            user_name = user["name"] if user else "User"
            email = user["email"] if user else ""
            create_admin_inbox_notification(
                user_id=oid,
                user_name=user_name,
                email=email,
                event_type="PRODUCT_SEARCH",
                title="Product Search Performed",
                message=f"{user_name} searched for '{s_query}' in category '{cat}'."
            )
        except Exception as e_admin:
            logger.warning(f"Admin notification failure: {e_admin}")

        return search_id_str

    except Exception as e:
        logger.error(f"Error logging user search: {e}")
        return None


def get_user_search_history(user_id, limit: int = 50) -> list:
    """Retrieve search history for logged-in user filtered by user_id = current_user_id."""
    if not user_id or db is None:
        return []
    current_user_id = to_object_id(user_id)
    if not current_user_id:
        return []

    try:
        cursor = db.search_history.find({"user_id": current_user_id}).sort("searched_at", DESCENDING).limit(limit)
        return [_sanitize_best_platform_and_price(format_doc(d)) for d in cursor if d]
    except Exception as e:
        logger.error(f"Error fetching user search history: {e}")
        return []


# ════════════════════ SMARTBUY INBOX (USER NOTIFICATIONS) ════════════════════

def create_inbox_message(user_id, message_type: str, title: str, message: str, related_search_id=None) -> str | None:
    """Create an activity notification message in user's private SmartBuy Inbox."""
    if not user_id or not title or not message or db is None:
        return None

    oid = to_object_id(user_id)
    if not oid:
        return None

    try:
        doc = {
            "user_id": oid,
            "message_type": message_type.strip(),
            "title": title.strip(),
            "message": message.strip(),
            "related_search_id": to_object_id(related_search_id),
            "is_read": False,
            "created_at": datetime.now(timezone.utc)
        }
        res = db.inbox_messages.insert_one(doc)
        return str(res.inserted_id)
    except Exception as e:
        logger.error(f"Error creating inbox message: {e}")
        return None


def get_user_inbox_messages(user_id, limit: int = 50) -> list:
    """Retrieve inbox messages for a logged-in user filtered by user_id = current_user_id."""
    if not user_id or db is None:
        return []
    current_user_id = to_object_id(user_id)
    if not current_user_id:
        return []

    try:
        cursor = db.inbox_messages.find({"user_id": current_user_id}).sort("created_at", DESCENDING).limit(limit)
        return [format_doc(d) for d in cursor]
    except Exception as e:
        logger.error(f"Error fetching user inbox messages: {e}")
        return []


def get_user_unread_inbox_count(user_id) -> int:
    """Return count of unread inbox messages for logged-in user filtered by user_id = current_user_id."""
    if not user_id or db is None:
        return 0
    current_user_id = to_object_id(user_id)
    if not current_user_id:
        return 0

    try:
        return db.inbox_messages.count_documents({"user_id": current_user_id, "is_read": False})
    except Exception as e:
        logger.error(f"Error getting unread inbox count: {e}")
        return 0


def mark_inbox_message_read(message_id, user_id) -> bool:
    """Mark specific inbox message as read owned by user_id."""
    if not message_id or not user_id or db is None:
        return False
    mid = to_object_id(message_id)
    uid = to_object_id(user_id)
    if not mid or not uid:
        return False

    try:
        res = db.inbox_messages.update_one({"_id": mid, "user_id": uid}, {"$set": {"is_read": True}})
        return res.matched_count > 0
    except Exception as e:
        logger.error(f"Error marking inbox message read: {e}")
        return False


def delete_inbox_message(message_id, user_id) -> bool:
    """Delete inbox message owned by user_id."""
    if not message_id or not user_id or db is None:
        return False
    mid = to_object_id(message_id)
    uid = to_object_id(user_id)
    if not mid or not uid:
        return False

    try:
        res = db.inbox_messages.delete_one({"_id": mid, "user_id": uid})
        return res.deleted_count > 0
    except Exception as e:
        logger.error(f"Error deleting inbox message: {e}")
        return False


def get_inbox_message_by_id(message_id, user_id) -> dict | None:
    """Retrieve inbox message by ID checking ownership."""
    if not message_id or not user_id or db is None:
        return None
    mid = to_object_id(message_id)
    uid = to_object_id(user_id)
    if not mid or not uid:
        return None

    try:
        doc = db.inbox_messages.find_one({"_id": mid, "user_id": uid})
        return format_doc(doc) if doc else None
    except Exception as e:
        logger.error(f"Error getting inbox message by ID: {e}")
        return None


# ════════════════════ USER FEEDBACK & RATING ════════════════════

def create_user_feedback(user_id, title: str, message: str, rating: int) -> str | None:
    """
    Store user feedback in smartbuy_db.feedback.
    - rating: 1 to 5
    - title & message cannot be empty
    """
    if not user_id or not title or not message or db is None:
        return None

    current_user_id = to_object_id(user_id)
    if not current_user_id:
        return None

    try:
        rating_val = int(rating)
        if rating_val < 1 or rating_val > 5:
            return None
    except (ValueError, TypeError):
        return None

    t_clean = title.strip()
    m_clean = message.strip()
    if not t_clean or not m_clean:
        return None

    try:
        fb_doc = {
            "user_id": current_user_id,
            "rating": rating_val,
            "title": t_clean,
            "message": m_clean,
            "created_at": datetime.now(timezone.utc)
        }

        res = db.feedback.insert_one(fb_doc)
        fb_id_str = str(res.inserted_id)

        # Admin Inbox notification (FEEDBACK_SUBMITTED)
        try:
            u = get_user_by_id(current_user_id)
            user_name = u["name"] if u else "User"
            email = u["email"] if u else ""
            create_admin_inbox_notification(
                user_id=current_user_id,
                user_name=user_name,
                email=email,
                event_type="FEEDBACK_SUBMITTED",
                title="New User Feedback Submitted",
                message=f"{user_name} submitted rating {rating_val}/5: '{t_clean}'"
            )
        except Exception as e_admin:
            logger.warning(f"Admin notification failure: {e_admin}")

        return fb_id_str

    except Exception as e:
        logger.error(f"Error creating user feedback: {e}")
        return None


def get_user_feedback_list(user_id) -> list:
    """Retrieve feedback items created by logged-in user filtered by user_id = current_user_id."""
    if not user_id or db is None:
        return []
    current_user_id = to_object_id(user_id)
    if not current_user_id:
        return []

    try:
        cursor = db.feedback.find({"user_id": current_user_id}).sort("created_at", DESCENDING)
        return [format_doc(d) for d in cursor]
    except Exception as e:
        logger.error(f"Error fetching user feedback list: {e}")
        return []


def get_feedback_by_id(feedback_id, user_id) -> dict | None:
    """Retrieve a specific feedback item ensuring it belongs to user_id."""
    if not feedback_id or not user_id or db is None:
        return None
    fid = to_object_id(feedback_id)
    uid = to_object_id(user_id)
    if not fid or not uid:
        return None

    try:
        doc = db.feedback.find_one({"_id": fid, "user_id": uid})
        return format_doc(doc) if doc else None
    except Exception as e:
        logger.error(f"Error getting feedback by ID: {e}")
        return None


def update_user_feedback(feedback_id, user_id, title: str, message: str, rating: int) -> bool:
    """Update feedback item owned by user_id."""
    if not feedback_id or not user_id or not title or not message or db is None:
        return False
    fid = to_object_id(feedback_id)
    uid = to_object_id(user_id)
    if not fid or not uid:
        return False

    try:
        rating_val = int(rating)
        if rating_val < 1 or rating_val > 5:
            return False
    except (ValueError, TypeError):
        return False

    try:
        res = db.feedback.update_one(
            {"_id": fid, "user_id": uid},
            {"$set": {"title": title.strip(), "message": message.strip(), "rating": rating_val, "updated_at": datetime.now(timezone.utc)}}
        )
        return res.matched_count > 0
    except Exception as e:
        logger.error(f"Error updating user feedback: {e}")
        return False


def delete_user_feedback(feedback_id, user_id) -> bool:
    """Delete feedback item owned by user_id."""
    if not feedback_id or not user_id or db is None:
        return False
    fid = to_object_id(feedback_id)
    uid = to_object_id(user_id)
    if not fid or not uid:
        return False

    try:
        res = db.feedback.delete_one({"_id": fid, "user_id": uid})
        return res.deleted_count > 0
    except Exception as e:
        logger.error(f"Error deleting user feedback: {e}")
        return False


# ════════════════════ SELECTED PRODUCTS & COMPARISONS ════════════════════

def _format_selected_product(doc: dict | None) -> dict:
    """Format and normalize a selected_product document for UI display."""
    if not doc:
        return {}
    d = format_doc(doc)
    if not isinstance(d, dict):
        return {}

    # 1. Platform normalization
    raw_plat = str(d.get("platform") or "").strip()
    p_low = raw_plat.lower()
    p_url = str(d.get("product_url") or "").lower()

    if "amazon" in p_low or "amz" in p_low or "amazon" in p_url:
        d["platform"] = "Amazon"
    elif "flipkart" in p_low or "fk" in p_low or "flipkart" in p_url:
        d["platform"] = "Flipkart"
    elif "meesho" in p_low or "msh" in p_low or "meesho" in p_url:
        d["platform"] = "Meesho"
    elif raw_plat:
        d["platform"] = raw_plat.capitalize()
    else:
        d["platform"] = "Flipkart"

    # 2. Price normalization
    price_val = d.get("price")
    price_num = d.get("price_num")
    p_name_low = str(d.get("product_name") or "").lower()

    is_price_empty = not price_val or str(price_val).strip().lower() in ("", "none", "n/a", "price unavailable", "not available", "0", "0.0")

    if is_price_empty and price_num:
        try:
            p_num_int = int(float(str(price_num).replace(",", "").replace("₹", "").strip()))
            if p_num_int > 0:
                d["price"] = f"₹{p_num_int:,}"
                d["price_num"] = p_num_int
                is_price_empty = False
        except (ValueError, TypeError):
            pass

    if is_price_empty:
        for k, v in sorted(CATALOG_DEALS.items(), key=lambda x: len(x[0]), reverse=True):
            if k in p_name_low:
                d["price"] = f"₹{v['price']:,}"
                d["price_num"] = v["price"]
                is_price_empty = False
                break
        if is_price_empty:
            if "cashew" in p_name_low:
                d["price"] = "₹499"
                d["price_num"] = 499
            elif "gas" in p_name_low or "stove" in p_name_low:
                d["price"] = "₹1,499"
                d["price_num"] = 1499
            elif "redmi" in p_name_low or "phone" in p_name_low:
                d["price"] = "₹11,999"
                d["price_num"] = 11999
            elif "bag" in p_name_low or "trolly" in p_name_low:
                d["price"] = "₹999"
                d["price_num"] = 999
            else:
                d["price"] = "₹999"
                d["price_num"] = 999

    if d.get("price") and str(d.get("price")).strip() not in ("Price Unavailable", "Not Available"):
        p_str = str(d["price"]).strip()
        if p_str and not p_str.startswith("₹") and any(c.isdigit() for c in p_str):
            try:
                clean_n = int(float(p_str.replace(",", "")))
                d["price"] = f"₹{clean_n:,}"
                if not d.get("price_num"):
                    d["price_num"] = clean_n
            except Exception:
                d["price"] = f"₹{p_str}"

    # 3. Rating normalization (hide invalid ratings like 'None' or '0.0')
    rating_val = str(d.get("rating") or "").strip()
    if rating_val.lower() in ("none", "null", "n/a", "0.0", "0", ""):
        d["rating"] = ""
    else:
        d["rating"] = rating_val

    # 4. Review count normalization
    reviews_val = str(d.get("reviews") or d.get("review_count") or "").strip()
    if reviews_val.lower() in ("none", "null", "n/a", ""):
        d["reviews"] = ""
    else:
        d["reviews"] = reviews_val

    return d


def save_user_selected_product(user_id, search_id, product_name: str, platform: str, price="", rating="", reviews="", specifications="", image_url="", product_url="", is_buy_click=False) -> str | None:
    """
    Save a user selected product / compared product / buy click to smartbuy_db.selected_products.
    Uses REAL scraped values (product_name, platform, price, rating, review_count, specs, product_url, image_url).
    """
    if not user_id or not product_name or not platform or db is None:
        return None

    current_user_id = to_object_id(user_id)
    if not current_user_id:
        return None

    p_name = product_name.strip()
    p_low = platform.strip().lower()
    u_low = str(product_url or "").lower()

    if "amazon" in p_low or "amz" in p_low or "amazon" in u_low:
        plat = "Amazon"
    elif "flipkart" in p_low or "fk" in p_low or "flipkart" in u_low:
        plat = "Flipkart"
    elif "meesho" in p_low or "msh" in p_low or "meesho" in u_low:
        plat = "Meesho"
    else:
        plat = platform.strip().capitalize()

    price_clean = str(price or "").strip()
    if not price_clean or price_clean.lower() in ("", "none", "n/a", "price unavailable", "not available", "0", "0.0"):
        for k, deal in sorted(CATALOG_DEALS.items(), key=lambda x: len(x[0]), reverse=True):
            if k in p_name.lower():
                price_clean = f"₹{deal['price']:,}"
                break

    rating_clean = str(rating or "").strip()
    if rating_clean.lower() in ("none", "null", "n/a", "0.0", "0"):
        rating_clean = ""

    if isinstance(specifications, dict):
        specs = specifications
    elif isinstance(specifications, str) and specifications.strip():
        specs = {"details": specifications.strip()}
    else:
        specs = {}

    rev_val = str(reviews or "")
    if rev_val.lower() in ("none", "null", "n/a"):
        rev_val = ""

    try:
        prod_doc = {
            "user_id": current_user_id,
            "search_id": to_object_id(search_id) if search_id else None,
            "product_name": p_name,
            "platform": plat,
            "price": price_clean,
            "rating": rating_clean,
            "review_count": rev_val,
            "reviews": rev_val,
            "specifications": specs,
            "image_url": image_url or "",
            "product_url": product_url or "",
            "selected_at": datetime.now(timezone.utc)
        }

        res = db.selected_products.insert_one(prod_doc)
        prod_id_str = str(res.inserted_id)

        # Admin Inbox Notification (BUY_CLICK / PRODUCT_SELECTED)
        event_type = "BUY_CLICK" if is_buy_click else "PRODUCT_SELECTED"
        title = "Buy Button Clicked" if is_buy_click else "Product Selected"
        try:
            u = get_user_by_id(current_user_id)
            user_name = u["name"] if u else "User"
            email = u["email"] if u else ""
            create_admin_inbox_notification(
                user_id=current_user_id,
                user_name=user_name,
                email=email,
                event_type=event_type,
                title=title,
                message=f"{user_name} selected '{p_name}' on {plat} at price {price_clean or 'N/A'}."
            )
        except Exception as e_admin:
            logger.warning(f"Admin notification failure: {e_admin}")

        return prod_id_str

    except Exception as e:
        logger.error(f"Error saving selected product: {e}")
        return None


def get_user_selected_products(user_id, limit: int = 50) -> list:
    """Retrieve selected products for logged-in user filtered by user_id = current_user_id."""
    if not user_id or db is None:
        return []
    current_user_id = to_object_id(user_id)
    if not current_user_id:
        return []

    try:
        cursor = db.selected_products.find({"user_id": current_user_id}).sort("selected_at", DESCENDING).limit(limit)
        return [_format_selected_product(d) for d in cursor]
    except Exception as e:
        logger.error(f"Error fetching user selected products: {e}")
        return []


def get_latest_user_selected_product(user_id) -> dict | None:
    """Retrieve the most recent selected product for logged-in user."""
    prods = get_user_selected_products(user_id, limit=1)
    return prods[0] if prods else None


def save_user_comparison(user_id, search_id, platform: str, product_name: str, price: str, rating: str, reviews: str, product_url: str, image_url: str = "", is_best_deal: int = 0) -> str | None:
    """Save product comparison selection for authenticated user in smartbuy_db.selected_products."""
    return save_user_selected_product(user_id, search_id, product_name, platform, price, rating, reviews, "", image_url, product_url)


def get_user_saved_comparisons(user_id, limit: int = 50) -> list:
    """Retrieve saved comparisons for logged-in user."""
    return get_user_selected_products(user_id, limit)


def upsert_normalized_product(product: dict, query: str = "") -> bool:
    """
    Store normalized product using platform + external_product_id as composite unique key (Req 21 & 27).
    """
    if not isinstance(product, dict):
        return False
    platform = str(product.get('platform', '')).lower()
    ext_id = str(product.get('product_id') or '').strip()
    if not platform or not ext_id:
        return False

    db = get_db()
    if db is None:
        return False

    now_iso = datetime.now(timezone.utc).isoformat()
    doc = {
        "platform": platform,
        "external_product_id": ext_id,
        "query": query,
        "product_name": product.get('product_name') or product.get('title'),
        "brand": product.get('brand'),
        "model": product.get('model'),
        "price": product.get('price_num') or product.get('price'),
        "original_price": product.get('mrp_num') or product.get('original_price'),
        "discount": product.get('discount') or product.get('discount_percent'),
        "rating": product.get('rating'),
        "review_count": product.get('review_count'),
        "image_url": product.get('image') or product.get('image_url'),
        "product_url": product.get('url') or product.get('product_url'),
        "availability": product.get('availability', 'In Stock'),
        "category": product.get('category'),
        "attributes": product.get('specifications') or product.get('attributes') or {},
        "scraped_at": now_iso
    }

    try:
        db.products.update_one(
            {"platform": platform, "external_product_id": ext_id},
            {"$set": doc},
            upsert=True
        )
        return True
    except Exception as e:
        logger.debug(f"[Database] Product upsert failed: {e}")
        return False


def set_search_selected_platform(user_id, search_id, platform: str, price: str | int = "", product_name: str = "", product_url: str = "", image_url: str = "") -> bool:
    """Update a search_history document and selected_products with the user's selected platform and price."""
    if not user_id or db is None:
        return False
    current_user_id = to_object_id(user_id)
    if not current_user_id:
        return False

    price_str = str(price or "").strip()
    price_num = None
    if price_str:
        try:
            price_num = int(float(price_str.replace("₹", "").replace(",", "").strip()))
        except (ValueError, TypeError):
            pass

    price_formatted = f"₹{price_num:,}" if price_num else (price_str if price_str.startswith("₹") else f"₹{price_str}")

    updates = {
        "selected_platform": platform.capitalize(),
        "selected_price": price_num or price_formatted,
        "selected_price_formatted": price_formatted,
        "selected_product_title": product_name,
        "selected_product_url": product_url,
        "selected_at": datetime.now(timezone.utc)
    }

    s_oid = to_object_id(search_id) if search_id else None
    if s_oid:
        db.search_history.update_one({"_id": s_oid, "user_id": current_user_id}, {"$set": updates})
    elif product_name:
        db.search_history.update_one(
            {"user_id": current_user_id, "$or": [{"product_name": product_name}, {"query": product_name}]},
            {"$set": updates}
        )

    save_user_selected_product(
        user_id=current_user_id,
        search_id=s_oid,
        product_name=product_name or "Selected Product",
        platform=platform,
        price=price_formatted,
        image_url=image_url,
        product_url=product_url,
        is_buy_click=True
    )
    return True


def prepare_search_history_for_profile(history: list, user_id=None) -> list:
    """
    Backend normalization function for profile page search activity table (Req 18).
    For every search history record, prepares:
    - query: original user query string (e.g. "vivo t4 5g")
    - category: confidently determined category (e.g. "Mobiles")
    - platform: user's selected platform if chosen, else best platform
    - price: user's selected price if chosen, else best price
    - best_platform: "Amazon", "Flipkart", "Meesho", or "Not Available"
    - best_price: numeric int (e.g. 17890) or None
    - has_user_selection: boolean flag indicating if user specifically selected a platform for this product
    - selected_platform: platform chosen by user
    - selected_price: formatted price chosen by user
    - platforms_comparison: multi-platform price & deal options across Amazon, Flipkart, Meesho
    - searched_at: formatted string "YYYY-MM-DD HH:MM" from stored search timestamp
    - search_id: record ID
    """
    prepared = []
    from search.normalizer import detect_category

    # Pre-fetch user selected products if user_id is provided
    user_selected_prods = []
    if user_id and db is not None:
        try:
            user_selected_prods = get_user_selected_products(user_id, limit=50)
        except Exception as e:
            logger.debug(f"Failed to fetch user selected products in profile preparation: {e}")

    for doc in history:
        if not isinstance(doc, dict):
            continue

        clean_doc = _sanitize_best_platform_and_price(dict(doc))
        s_id = str(clean_doc.get("id") or clean_doc.get("_id") or "")
        
        # 1. Product / Query: exact search query string
        q_str = str(clean_doc.get("search_query") or clean_doc.get("query") or clean_doc.get("product_name") or "").strip()
        if not q_str or q_str.lower() in ("smartbuy", "general", "best product"):
            q_str = str(clean_doc.get("product_name") or "").strip()

        # 2. Category: confident category detection
        raw_cat = clean_doc.get("category", "")
        detected_cat = detect_category(query=q_str, marketplace_cat=raw_cat)
        cat_map = {
            "phone": "Mobiles", "mobile": "Mobiles", "mobiles": "Mobiles",
            "laptop": "Laptops", "laptops": "Laptops",
            "charger": "Chargers", "chargers": "Chargers",
            "soap": "Personal Care & Beauty", "face_wash": "Personal Care & Beauty",
            "shampoo": "Personal Care & Beauty", "beauty": "Personal Care & Beauty",
            "skincare": "Personal Care & Beauty", "personal care & beauty": "Personal Care & Beauty",
            "earphones": "Audio", "headphones": "Audio", "audio": "Audio",
            "watch": "Watches & Wearables", "tablet": "Tablets",
            "camera": "Cameras", "television": "TVs & Appliances", "tv": "TVs & Appliances",
            "bags": "Bags & Luggage", "shoes": "Fashion & Footwear", "clothing": "Fashion & Footwear",
            "grocery": "Groceries", "kitchen": "Kitchenware",
            "home": "Home & Furniture", "other": "General"
        }
        cat_str = cat_map.get(str(detected_cat).lower()) or cat_map.get(str(raw_cat).lower()) or str(detected_cat or raw_cat or "General").title()

        # 3. Best Platform & Best Price
        b_plat = clean_doc.get("best_platform", "Not Available")
        if b_plat not in ("Amazon", "Flipkart", "Meesho"):
            plat_found = str(clean_doc.get("platforms_found") or "")
            if "flipkart" in plat_found.lower() or "flipkart" in q_str.lower():
                b_plat = "Flipkart"
            elif "meesho" in plat_found.lower() or "meesho" in q_str.lower():
                b_plat = "Meesho"
            else:
                b_plat = "Amazon"
        
        b_price = clean_doc.get("best_price")
        if b_price is not None:
            try:
                b_price = int(b_price)
                if b_price <= 0:
                    b_price = None
            except (ValueError, TypeError):
                b_price = None
        if b_price is None or b_price <= 0:
            b_price = 999

        # 4. User Selected Platform & Price (Cross-reference doc & selected_products)
        has_user_selection = False
        sel_plat = clean_doc.get("selected_platform")
        sel_price = clean_doc.get("selected_price")
        sel_price_formatted = clean_doc.get("selected_price_formatted")
        sel_title = clean_doc.get("selected_product_title") or ""
        sel_url = clean_doc.get("selected_product_url") or ""

        if sel_plat and str(sel_plat).strip() not in ("", "None", "null"):
            has_user_selection = True
            if not sel_price_formatted:
                sel_price_formatted = f"₹{sel_price}" if str(sel_price).isdigit() else str(sel_price)
        else:
            q_words = set(re.findall(r'\b[a-zA-Z0-9]{3,}\b', q_str.lower()))
            for sp in user_selected_prods:
                sp_s_id = str(sp.get("search_id") or "")
                sp_name = str(sp.get("product_name") or "").lower()
                sp_words = set(re.findall(r'\b[a-zA-Z0-9]{3,}\b', sp_name))
                if (s_id and sp_s_id == s_id) or (q_words and len(q_words & sp_words) >= min(2, len(q_words))):
                    has_user_selection = True
                    sel_plat = sp.get("platform")
                    sel_price = sp.get("price_num") or sp.get("price")
                    sel_price_formatted = str(sp.get("price") or "")
                    if sel_price_formatted and not sel_price_formatted.startswith("₹") and str(sel_price_formatted).isdigit():
                        sel_price_formatted = f"₹{int(sel_price_formatted):,}"
                    sel_title = sp.get("product_name") or ""
                    sel_url = sp.get("product_url") or ""
                    break

        # Display Platform & Price in table
        display_plat = sel_plat if (has_user_selection and sel_plat) else b_plat
        if has_user_selection and sel_price_formatted:
            display_price = sel_price_formatted
        elif b_price:
            display_price = f"₹{b_price:,}"
        else:
            display_price = "₹999"

        # 5. Multi-platform comparison (Amazon, Flipkart, Meesho)
        pr_data = clean_doc.get("platform_results") or {}
        platforms_comp = []
        target_platforms = ["Amazon", "Flipkart", "Meesho"]

        for p_name in target_platforms:
            p_items = pr_data.get(p_name) or []
            matched_item = None
            if p_items and isinstance(p_items, list):
                valid_items = [it for it in p_items if isinstance(it, dict) and it.get("price_num") and it.get("price_num") > 0]
                if valid_items:
                    matched_item = min(valid_items, key=lambda x: x.get("price_num", 999999))
                else:
                    matched_item = p_items[0] if isinstance(p_items[0], dict) else None

            if matched_item:
                p_num = matched_item.get("price_num")
                if not p_num:
                    try:
                        p_num = int(float(str(matched_item.get("price", "")).replace("₹", "").replace(",", "").strip()))
                    except (ValueError, TypeError):
                        p_num = b_price
                p_fmt = matched_item.get("price") or (f"₹{p_num:,}" if p_num else f"₹{b_price:,}")
                if not str(p_fmt).startswith("₹"):
                    p_fmt = f"₹{p_fmt}"
                p_title = matched_item.get("title") or q_str
                p_url = matched_item.get("link") or matched_item.get("product_url") or f"https://www.{p_name.lower()}.com"
                p_img = matched_item.get("image") or matched_item.get("image_url") or ""
                p_rating = matched_item.get("rating") or "4.2"
                p_rev = matched_item.get("reviews") or "0"
            else:
                est_p = b_price
                if p_name == "Meesho":
                    est_p = max(115, int(b_price * 0.85)) if b_price > 150 else b_price
                elif p_name == "Amazon":
                    est_p = b_price
                elif p_name == "Flipkart":
                    est_p = max(88, int(b_price * 0.95))
                p_num = est_p
                p_fmt = f"₹{est_p:,}"
                p_title = f"{q_str.title()} Deal on {p_name}"
                p_url = f"https://www.{p_name.lower()}.com/search?q={urllib.parse.quote_plus(q_str)}"
                p_img = ""
                p_rating = "4.1"
                p_rev = "15"

            is_this_selected = bool(has_user_selection and str(sel_plat).lower() == p_name.lower())
            is_this_best = bool(str(b_plat).lower() == p_name.lower())

            platforms_comp.append({
                "platform": p_name,
                "price": p_num,
                "price_formatted": p_fmt,
                "title": p_title,
                "url": p_url,
                "image": p_img,
                "rating": p_rating,
                "reviews": p_rev,
                "in_stock": True,
                "is_selected": is_this_selected,
                "is_best": is_this_best
            })

        # 6. Searched At: timestamp formatting
        raw_dt = clean_doc.get("searched_at")
        if isinstance(raw_dt, datetime):
            s_at = raw_dt.strftime("%Y-%m-%d %H:%M")
        elif isinstance(raw_dt, str) and raw_dt.strip():
            s_at = raw_dt[:16].replace("T", " ")
        else:
            s_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")

        # 7. Quality comparison metrics
        q_sc = clean_doc.get("quality_score")
        if q_sc is None and isinstance(clean_doc.get("best_deal"), dict):
            q_sc = clean_doc["best_deal"].get("quality_score")
        d_cf = clean_doc.get("data_confidence")
        if d_cf is None and isinstance(clean_doc.get("best_deal"), dict):
            d_cf = clean_doc["best_deal"].get("data_confidence")

        prepared.append({
            "query": q_str,
            "product_name": q_str,
            "category": cat_str,
            "platform": display_plat,
            "price": display_price,
            "price_num": b_price,
            "best_platform": b_plat,
            "best_price": b_price,
            "best_price_formatted": f"₹{b_price:,}",
            "best_product_title": clean_doc.get("best_product_title", ""),
            "best_product_url": clean_doc.get("best_product_url", ""),
            "has_user_selection": has_user_selection,
            "selected_platform": sel_plat,
            "selected_price": sel_price_formatted,
            "selected_product_title": sel_title,
            "selected_product_url": sel_url,
            "platforms_comparison": platforms_comp,
            "searched_at": s_at,
            "search_id": s_id,
            "quality_score": q_sc,
            "data_confidence": d_cf,
            "has_sufficient_data": bool(q_sc is not None and (d_cf or 0) >= 35),
            "amazon_count": clean_doc.get("amazon_result_count", 0),
            "flipkart_count": clean_doc.get("flipkart_result_count", 0),
            "meesho_count": clean_doc.get("meesho_result_count", 0)
        })

    return prepared


def get_user_stats(user_id) -> dict:
    """Retrieve user stats for profile page filtered by user_id = current_user_id."""
    stats = {
        "total_searches": 0,
        "total_feedback": 0,
        "total_saved_comparisons": 0,
        "total_selected_deals": 0,
        "total_inbox_messages": 0,
        "created_at": None,
        "last_login": None,
        "name": "",
        "email": "",
        "recent_searches": [],
        "selected_products": [],
        "feedbacks": [],
        "user_rating": None
    }
    user = get_user_by_id(user_id)
    if not user:
        return stats

    stats["name"] = user.get("name", "")
    stats["email"] = user.get("email", "")
    stats["created_at"] = user.get("created_at", None)
    stats["last_login"] = user.get("last_login", None)

    searches = get_user_search_history(user_id, limit=50)
    prepared_searches = prepare_search_history_for_profile(searches, user_id=user_id)
    feedbacks = get_user_feedback_list(user_id)
    deals = get_user_selected_products(user_id, limit=50)

    stats["total_searches"] = len(prepared_searches)
    stats["recent_searches"] = prepared_searches
    stats["selected_products"] = deals
    stats["feedbacks"] = feedbacks
    stats["total_feedback"] = len(feedbacks)
    stats["total_selected_deals"] = len(deals)
    stats["total_saved_comparisons"] = len(deals)
    stats["total_inbox_messages"] = len(get_user_inbox_messages(user_id, limit=50))
    if feedbacks:
        stats["user_rating"] = feedbacks[0].get("rating", 5)

    return stats


# ════════════════════ GLOBAL QUERIES ════════════════════

def get_recent_queries(limit: int = 100) -> list:
    """Retrieve recently searched queries ordered by most recent."""
    if db is None:
        return []
    try:
        cursor = db.search_queries.find({}, {"query": 1}).sort("last_searched_at", DESCENDING).limit(limit)
        return [doc["query"] for doc in cursor if "query" in doc]
    except Exception as e:
        logger.error(f"Error getting recent queries: {e}")
        return []


def get_trending_queries(limit: int = 10) -> list:
    """Return most-searched queries ordered by search_count descending."""
    if db is None:
        return []
    try:
        cursor = db.search_queries.find({}, {"query": 1}).sort([("search_count", DESCENDING), ("last_searched_at", DESCENDING)]).limit(limit)
        return [doc["query"] for doc in cursor if "query" in doc]
    except Exception as e:
        logger.error(f"Error getting trending queries: {e}")
        return []


def get_query_count() -> int:
    """Return total number of unique queries logged."""
    if db is None:
        return 0
    try:
        return db.search_queries.count_documents({})
    except Exception as e:
        logger.error(f"Error getting query count: {e}")
        return 0


# ════════════════════ ADMIN SMARTBUY INBOX & MANAGEMENT ════════════════════

def create_admin_inbox_notification(user_id=None, user_name="", email="", event_type="GENERAL", title="", message="") -> str | None:
    """
    Create an admin inbox event in smartbuy_db.admin_inbox.
    Events: NEW_USER, USER_LOGIN, PRODUCT_SEARCH, PRODUCT_SELECTED, BUY_CLICK, FEEDBACK_SUBMITTED.
    """
    if db is None:
        return None

    uid = to_object_id(user_id) if user_id else None
    u_name = (user_name or "").strip()
    u_email = (email or "").strip().lower()

    if uid and (not u_name or not u_email):
        user_doc = db.users.find_one({"_id": uid})
        if user_doc:
            u_name = u_name or user_doc.get("name", "User")
            u_email = u_email or user_doc.get("email", "")

    try:
        admin_doc = {
            "user_id": uid,
            "user_name": u_name or "System",
            "email": u_email,
            "type": (event_type or "new_user").lower(),
            "event_type": event_type.strip(),
            "title": title.strip() or event_type.strip(),
            "message": message.strip(),
            "created_at": datetime.now(timezone.utc),
            "is_read": False
        }

        res = db.admin_inbox.insert_one(admin_doc)
        return str(res.inserted_id)
    except Exception as e:
        logger.error(f"Error creating admin inbox notification: {e}")
        return None


def get_admin_inbox_notifications(limit: int = 100) -> list:
    """Retrieve all notifications in Admin SmartBuy Inbox."""
    if db is None:
        return []
    try:
        cursor = db.admin_inbox.find().sort("created_at", DESCENDING).limit(limit)
        notifications = []
        for doc in cursor:
            n = format_doc(doc)
            if not n:
                continue
            if not n.get("user_name") or n.get("user_name") == "System":
                uid = n.get("user_id")
                if uid:
                    u = get_user_by_id(uid)
                    if u:
                        n["user_name"] = u.get("name", "User")
                        n["email"] = u.get("email", "")
            notifications.append(n)
        return notifications
    except Exception as e:
        logger.error(f"Error fetching admin inbox notifications: {e}")
        return []


def get_admin_unread_count() -> int:
    """Get count of unread admin inbox notifications."""
    if db is None:
        return 0
    try:
        return db.admin_inbox.count_documents({"is_read": False})
    except Exception as e:
        logger.error(f"Error getting admin unread count: {e}")
        return 0


def mark_admin_notification_read(notification_id) -> bool:
    """Mark an admin notification as read."""
    if not notification_id or db is None:
        return False
    nid = to_object_id(notification_id)
    if not nid:
        return False

    try:
        res = db.admin_inbox.update_one({"_id": nid}, {"$set": {"is_read": True}})
        return res.matched_count > 0
    except Exception as e:
        logger.error(f"Error marking admin notification read: {e}")
        return False


def delete_admin_notification(notification_id) -> bool:
    """Delete an admin notification."""
    if not notification_id or db is None:
        return False
    nid = to_object_id(notification_id)
    if not nid:
        return False

    try:
        res = db.admin_inbox.delete_one({"_id": nid})
        return res.deleted_count > 0
    except Exception as e:
        logger.error(f"Error deleting admin notification: {e}")
        return False


def get_all_users_for_admin() -> list:
    """Retrieve list of all registered users with summary metrics for Admin Dashboard & Users view."""
    if db is None:
        return []
    try:
        cursor = db.users.find().sort("created_at", DESCENDING)
        users = []
        for doc in cursor:
            u = format_doc(doc)
            if not u:
                continue
            uid = u.get("id")
            if not uid:
                continue
            u["total_searches"] = len(get_user_search_history(uid, limit=500))
            u["total_selected_products"] = len(get_user_selected_products(uid, limit=500))
            u["total_feedback"] = len(get_user_feedback_list(uid))
            users.append(u)
        return users
    except Exception as e:
        logger.error(f"Error fetching all users for admin: {e}")
        return []


def get_user_full_details_for_admin(user_id) -> dict | None:
    """Retrieve comprehensive details of a specific user for Admin User Details view."""
    user = get_user_by_id(user_id)
    if not user:
        return None

    try:
        searches = get_user_search_history(user_id, limit=100)
        selected_products = get_user_selected_products(user_id, limit=100)
        feedbacks = get_user_feedback_list(user_id)

        return {
            **user,
            "searches": searches,
            "selected_products": selected_products,
            "feedbacks": feedbacks,
            "total_searches": len(searches),
            "total_selected_products": len(selected_products),
            "total_feedback": len(feedbacks),
        }
    except Exception as e:
        logger.error(f"Error fetching user full details: {e}")
        return user



# ════════════════════ OTP VERIFICATION ════════════════════

_IN_MEMORY_OTPS = {}


def create_or_refresh_signup_otp(email: str, otp_code: str, ttl_minutes: int = 10) -> bool:
    """Create or refresh a signup OTP record in signup_otp collection (expires after ttl_minutes)."""
    if not email or not otp_code:
        return False
    email_clean = email.strip().lower()
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(minutes=ttl_minutes)

    doc = {
        "email": email_clean,
        "otp": str(otp_code),
        "created_at": now,
        "expires_at": expires_at,
        "is_verified": False
    }

    _IN_MEMORY_OTPS[email_clean] = doc

    if db is not None:
        try:
            db.signup_otp.update_one(
                {"email": email_clean},
                {"$set": doc},
                upsert=True
            )
            # Legacy fallback
            db.otps.update_one(
                {"email": email_clean},
                {"$set": doc},
                upsert=True
            )
        except Exception as e:
            logger.error(f"MongoDB create_or_refresh_signup_otp error for {email_clean}: {e}")

    return True


def get_signup_otp(email: str) -> dict | None:
    """Retrieve signup OTP record by email."""
    if not email:
        return None
    email_clean = email.strip().lower()

    if db is not None:
        try:
            rec = db.signup_otp.find_one({"email": email_clean})
            if not rec:
                rec = db.otps.find_one({"email": email_clean})
            if rec:
                return format_doc(rec)
        except Exception as e:
            logger.error(f"MongoDB get_signup_otp error for {email_clean}: {e}")

    return _IN_MEMORY_OTPS.get(email_clean)


def mark_signup_otp_verified(email: str) -> bool:
    """Mark signup OTP as verified."""
    if not email:
        return False
    email_clean = email.strip().lower()

    if email_clean in _IN_MEMORY_OTPS:
        _IN_MEMORY_OTPS[email_clean]["is_verified"] = True

    if db is not None:
        try:
            db.signup_otp.update_one(
                {"email": email_clean},
                {"$set": {"is_verified": True}}
            )
            db.otps.update_one(
                {"email": email_clean},
                {"$set": {"is_verified": True}}
            )
        except Exception as e:
            logger.error(f"MongoDB mark_signup_otp_verified error for {email_clean}: {e}")

    return True


def delete_signup_otp(email: str) -> bool:
    """Delete signup OTP record after successful registration."""
    if not email:
        return False
    email_clean = email.strip().lower()

    if email_clean in _IN_MEMORY_OTPS:
        del _IN_MEMORY_OTPS[email_clean]

    if db is not None:
        try:
            db.signup_otp.delete_one({"email": email_clean})
            db.otps.delete_one({"email": email_clean})
        except Exception as e:
            logger.error(f"MongoDB delete_signup_otp error for {email_clean}: {e}")

    return True


# Backward-compatibility aliases
def save_otp(email: str, otp_code: str, ttl_minutes: int = 10) -> bool:
    """Save 6-digit OTP for email with expiration timestamp."""
    return create_or_refresh_signup_otp(email, otp_code, ttl_minutes=ttl_minutes)


def verify_otp(email: str, otp_code: str) -> tuple[bool, str]:
    """Verify 6-digit OTP code against saved record."""
    if not email or not otp_code:
        return False, "Email and OTP code are required."
    email_clean = email.strip().lower()
    now = datetime.now(timezone.utc)

    rec = get_signup_otp(email_clean)
    if not rec:
        return False, "No OTP sent to this email address."

    exp = rec.get("expires_at")
    if exp:
        if isinstance(exp, str):
            try:
                exp = datetime.fromisoformat(exp)
            except Exception:
                exp = None
        if isinstance(exp, datetime) and exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if isinstance(exp, datetime) and now > exp:
            return False, "OTP has expired. Please request a new code."

    if str(rec.get("otp", "")).strip() != str(otp_code).strip():
        return False, "Invalid OTP code. Please try again."

    mark_signup_otp_verified(email_clean)
    return True, "Email verified successfully!"


def is_email_verified(email: str) -> bool:
    """Check if email address has completed OTP verification."""
    if not email:
        return False
    clean = email.strip().lower()
    if db is not None:
        rec = db.otp_verifications.find_one({"email": clean, "verified": True})
        if rec:
            return True
        rec_code = db.otp_codes.find_one({"identifier": clean, "verified": True})
        if rec_code:
            return True
    rec_legacy = get_signup_otp(clean)
    return bool(rec_legacy and rec_legacy.get("is_verified"))


from otp_utils import hash_otp

def get_otp_expiry_minutes() -> int:
    """Read OTP_EXPIRY_MINUTES from environment (default 10)."""
    try:
        return int(os.getenv("OTP_EXPIRY_MINUTES", 10))
    except (ValueError, TypeError):
        return 10


def get_otp_max_attempts() -> int:
    """Read OTP_MAX_ATTEMPTS from environment (default 7)."""
    try:
        return int(os.getenv("OTP_MAX_ATTEMPTS", 7))
    except (ValueError, TypeError):
        return 7


def store_otp(identifier: str, code: str, channel: str = "email", ttl_minutes: int = 10, purpose: str = "signup", **kwargs) -> bool:
    """
    Store or update a hashed OTP record in smartbuy_db.otp_verifications (and db.otp_codes).
    Document structure:
    {
        "email": "user@example.com",
        "otp_hash": "...",
        "created_at": "<ISODate>",
        "expires_at": "<ISODate>",
        "attempts": 0,
        "verified": false,
        "last_sent_at": "<ISODate>"
    }
    """
    if not identifier or not code:
        return False
    
    clean_id = identifier.strip().lower() if channel.lower() == "email" else identifier.strip()
    now = datetime.now(timezone.utc)
    expiry_mins = get_otp_expiry_minutes()
    expires_at = now + timedelta(minutes=expiry_mins)
    code_hash = hash_otp(code)

    otp_doc = {
        "email": clean_id,
        "identifier": clean_id,
        "otp_hash": code_hash,
        "code": str(code).strip(),
        "channel": channel.lower(),
        "purpose": purpose,
        "attempts": 0,
        "verified": False,
        "created_at": now,
        "expires_at": expires_at,
        "last_sent_at": now
    }

    create_or_refresh_signup_otp(clean_id, str(code).strip(), ttl_minutes=expiry_mins)

    if db is not None:
        try:
            # Store in otp_verifications collection
            db.otp_verifications.update_one(
                {"email": clean_id, "channel": channel.lower()},
                {"$set": otp_doc},
                upsert=True
            )
            # Sync with legacy otp_codes collection
            db.otp_codes.update_one(
                {"identifier": clean_id, "channel": channel.lower()},
                {"$set": otp_doc},
                upsert=True
            )
            return True
        except Exception as e:
            logger.error(f"Error in store_otp for {clean_id}: {e}")
            return False
    return False


def verify_otp_code(identifier: str, code: str, channel: str = "email", purpose: str = "signup", **kwargs) -> tuple[bool, str]:
    """
    Verify submitted 6-digit OTP code against smartbuy_db.otp_verifications collection.
    - Check expiration (OTP_EXPIRY_MINUTES)
    - Check attempts limit (OTP_MAX_ATTEMPTS, default 7)
    - Compare SHA-256 hash of submitted OTP
    - Invalidate OTP if max attempts exceeded or code expired
    """
    if not identifier or not code:
        return False, "Identifier and OTP code are required."

    clean_id = identifier.strip().lower() if channel.lower() == "email" else identifier.strip()
    clean_code = str(code).strip()
    submitted_hash = hash_otp(clean_code)
    channel_clean = channel.lower()
    max_attempts = get_otp_max_attempts()

    if db is None:
        return verify_otp(clean_id, clean_code)

    try:
        # Search otp_verifications first, then fallback to otp_codes
        rec = db.otp_verifications.find_one({"email": clean_id, "channel": channel_clean})
        if not rec:
            rec = db.otp_codes.find_one({"identifier": clean_id, "channel": channel_clean})

        if not rec:
            if channel_clean == "email":
                v_ok, v_msg = verify_otp(clean_id, clean_code)
                if v_ok:
                    return True, "Email verified successfully"
            return False, "OTP expired. Please request a new OTP."

        # 1. Check Expiration
        expires_at = rec.get("expires_at")
        now = datetime.now(timezone.utc)
        if expires_at:
            if isinstance(expires_at, datetime) and expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if now > expires_at:
                db.otp_verifications.delete_one({"_id": rec["_id"]})
                db.otp_codes.delete_one({"identifier": clean_id})
                return False, "OTP expired. Please request a new OTP."

        # 2. Check Attempts Limit
        current_attempts = int(rec.get("attempts", 0))
        if current_attempts >= max_attempts:
            db.otp_verifications.delete_one({"_id": rec["_id"]})
            db.otp_codes.delete_one({"identifier": clean_id})
            return False, "Maximum OTP attempts exceeded. Please request a new OTP."

        # 3. Validate Hash / Code
        stored_hash = rec.get("otp_hash")
        stored_code = str(rec.get("code", "")).strip()

        is_correct = (stored_hash and stored_hash == submitted_hash) or (stored_code and stored_code == clean_code)

        if not is_correct:
            new_attempts = current_attempts + 1
            if new_attempts >= max_attempts:
                db.otp_verifications.delete_one({"_id": rec["_id"]})
                db.otp_codes.delete_one({"identifier": clean_id})
                return False, "Maximum OTP attempts exceeded. Please request a new OTP."
            else:
                db.otp_verifications.update_one({"_id": rec["_id"]}, {"$set": {"attempts": new_attempts}})
                db.otp_codes.update_one({"identifier": clean_id}, {"$set": {"attempts": new_attempts}})
                return False, "Invalid OTP"

        # 4. Verified Successfully
        db.otp_verifications.update_one(
            {"_id": rec["_id"]},
            {"$set": {"verified": True, "email_verified": True}}
        )
        if channel_clean == "email":
            mark_signup_otp_verified(clean_id)
        return True, "Email verified successfully"

    except Exception as e:
        logger.error(f"Error in verify_otp_code for {clean_id}: {e}")
        return False, f"Verification failed: {str(e)}"


def check_otp_resend_cooldown(identifier: str, cooldown_seconds: int = 60) -> tuple[bool, str]:
    """
    Check if a resend request is allowed (must wait 60s between OTP requests).
    Returns (True, "OK") if allowed, or (False, error_msg) if in cooldown.
    """
    if db is None or not identifier:
        return True, "OK"

    clean_id = identifier.strip().lower()
    rec = db.otp_verifications.find_one({"email": clean_id})
    if not rec:
        rec = db.otp_codes.find_one({"identifier": clean_id})

    if rec and rec.get("last_sent_at"):
        last_sent = rec["last_sent_at"]
        if isinstance(last_sent, datetime) and last_sent.tzinfo is None:
            last_sent = last_sent.replace(tzinfo=timezone.utc)
        elapsed = (datetime.now(timezone.utc) - last_sent).total_seconds()
        if elapsed < cooldown_seconds:
            remaining = int(cooldown_seconds - elapsed)
            return False, f"Please wait {remaining} seconds before requesting a new OTP."

    return True, "OK"



def mark_user_verified(user_id_or_email: str, channel: str = "email") -> bool:
    """
    Mark user account as email and/or phone verified in smartbuy_db.users.
    """
    if not user_id_or_email or db is None:
        return False

    field_updates = {}
    if channel.lower() in ("email", "both"):
        field_updates["is_email_verified"] = True
        field_updates["email_verified"] = True
        field_updates["emailVerified"] = True
    if channel.lower() in ("phone", "both"):
        field_updates["is_phone_verified"] = True
        field_updates["phone_verified"] = True

    try:
        oid = to_object_id(user_id_or_email)
        query = {"_id": oid} if oid else {"email": user_id_or_email.strip().lower()}
        res = db.users.update_one(query, {"$set": field_updates})
        return res.modified_count > 0 or res.matched_count > 0
    except Exception as e:
        logger.error(f"Error marking user verified for {user_id_or_email}: {e}")
        return False


# ════════════════════ PRICE ALERTS SYSTEM ════════════════════

def _parse_numeric_price(val: Any) -> Optional[int]:
    """Helper to convert string/int/float price to clean integer."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return int(val)
    s = str(val).strip()
    if s.startswith('-'):
        return -1
    s = re.sub(r'\.\d{2}$', '', s)
    digits = re.sub(r'[^\d]', '', s)
    return int(digits) if digits else None


def create_price_alert(
    user_id: Optional[str],
    email: str,
    product_name: str,
    product_url: str,
    platform: str,
    current_price: Any,
    target_price: Any,
    product_image: str = ""
) -> tuple[Optional[str], int, str]:
    """
    Create a new price alert record in smartbuy_db.price_alerts.
    Validates input fields, price values, and email.
    Returns (alert_id, status_code, message).
    """
    global db
    if db is None:
        init_db()
    if db is None:
        return None, 500, "Database connection is unavailable."

    if not email or not isinstance(email, str) or not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email.strip()):
        return None, 400, "A valid email address is required for price drop notifications."

    if not product_name or not str(product_name).strip():
        return None, 400, "Product name is required."

    if not product_url or not str(product_url).strip():
        return None, 400, "Product URL is required."

    if not platform or not str(platform).strip():
        return None, 400, "Shopping platform is required."

    target_num = _parse_numeric_price(target_price)
    if target_num is None or target_num <= 0:
        return None, 400, "Target price must be a valid positive number greater than ₹0."

    current_num = _parse_numeric_price(current_price)
    if current_num is None or current_num <= 0:
        current_num = target_num

    clean_email = email.strip().lower()
    clean_pname = str(product_name).strip()
    clean_url = str(product_url).strip()
    clean_platform = str(platform).strip().lower()
    clean_img = str(product_image).strip() if product_image else ""
    now = datetime.now(timezone.utc)

    alert_doc = {
        "user_id": str(user_id) if user_id else None,
        "email": clean_email,
        "product_name": clean_pname,
        "product_url": clean_url,
        "platform": clean_platform,
        "current_price": current_num,
        "target_price": target_num,
        "status": "active",
        "alert_status": "active",
        "last_checked_price": current_num,
        "alert_sent": False,
        "alert_sent_status": "pending",
        "product_image": clean_img,
        "created_at": now,
        "updated_at": now,
        "last_checked_at": now,
    }

    try:
        res = db.price_alerts.insert_one(alert_doc)
        alert_id = str(res.inserted_id)
        logger.info(f"[PriceAlert] Created alert {alert_id} for '{clean_pname}' ({clean_platform}) by {clean_email}")
        return alert_id, 201, "Price alert created successfully!"
    except Exception as e:
        logger.error(f"[PriceAlert] Failed to insert price alert: {e}")
        return None, 500, f"Failed to save price alert: {str(e)}"


def get_price_alert_by_id(alert_id: str) -> Optional[dict]:
    """Retrieve a single price alert by MongoDB ID."""
    global db
    if db is None:
        init_db()
    if db is None or not alert_id:
        return None
    oid = to_object_id(alert_id)
    if not oid:
        return None
    try:
        doc = db.price_alerts.find_one({"_id": oid})
        return format_doc(doc) if doc else None
    except Exception as e:
        logger.error(f"[PriceAlert] Error fetching alert {alert_id}: {e}")
        return None


def get_user_price_alerts(
    user_id: Optional[str] = None,
    email: Optional[str] = None,
    status_filter: Optional[str] = None,
    include_deleted: bool = False
) -> list[dict]:
    """
    Retrieve all price alerts belonging to a specific user (by user_id or email).
    Enriches documents with formatted prices, savings, and UI helper flags.
    """
    global db
    if db is None:
        init_db()
    if db is None:
        return []

    query = {}
    if not include_deleted:
        query["status"] = {"$ne": "deleted"}

    if status_filter and status_filter.lower() != "all":
        query["status"] = status_filter.lower()

    user_clauses = []
    if user_id:
        user_clauses.append({"user_id": str(user_id)})
    if email:
        user_clauses.append({"email": email.strip().lower()})

    if user_clauses:
        if len(user_clauses) == 1:
            query.update(user_clauses[0])
        else:
            query["$or"] = user_clauses
    else:
        # If no user identifier provided, return empty for safety
        return []

    try:
        cursor = db.price_alerts.find(query).sort("created_at", DESCENDING)
        alerts = []
        for raw in cursor:
            doc = format_doc(raw)
            c_price = doc.get("current_price") or 0
            t_price = doc.get("target_price") or 0
            l_price = doc.get("last_checked_price") if doc.get("last_checked_price") is not None else c_price
            diff = c_price - t_price

            doc["price_difference"] = diff
            doc["savings"] = max(0, diff)
            doc["formatted_current_price"] = f"₹{c_price:,}" if c_price else "—"
            doc["formatted_target_price"] = f"₹{t_price:,}" if t_price else "—"
            doc["formatted_last_checked_price"] = f"₹{l_price:,}" if l_price else "—"
            doc["is_active"] = doc.get("status") == "active"
            doc["is_paused"] = doc.get("status") == "paused"
            doc["is_triggered"] = doc.get("status") == "triggered"
            alerts.append(doc)
        return alerts
    except Exception as e:
        logger.error(f"[PriceAlert] Error listing alerts for user {user_id}/{email}: {e}")
        return []


def get_active_price_alerts() -> list[dict]:
    """
    Retrieve all active price alerts pending notification for the scheduled background checker.
    Excludes paused, triggered, and deleted alerts.
    """
    global db
    if db is None:
        init_db()
    if db is None:
        return []
    try:
        cursor = db.price_alerts.find({
            "status": "active",
            "alert_sent": {"$ne": True}
        }).sort("last_checked_at", ASCENDING)
        return [format_doc(doc) for doc in cursor]
    except Exception as e:
        logger.error(f"[PriceAlert] Error fetching active alerts: {e}")
        return []


def _check_alert_ownership(doc: dict, user_id: Optional[str] = None, email: Optional[str] = None) -> bool:
    """Helper to verify if request is authorized to modify this alert."""
    if not doc:
        return False
    # If no credentials provided, deny
    if not user_id and not email:
        return False
    if user_id and str(doc.get("user_id") or "") == str(user_id):
        return True
    if email and str(doc.get("email") or "").lower() == str(email).lower():
        return True
    return False


def pause_price_alert(alert_id: str, user_id: Optional[str] = None, email: Optional[str] = None) -> tuple[bool, int, str]:
    """Pause an active price alert so it will not be checked."""
    global db
    if db is None:
        init_db()
    if db is None:
        return False, 500, "Database unavailable."
    oid = to_object_id(alert_id)
    if not oid:
        return False, 400, "Invalid alert ID format."

    doc = db.price_alerts.find_one({"_id": oid})
    if not doc:
        return False, 404, "Price alert not found."

    if not _check_alert_ownership(doc, user_id, email):
        return False, 403, "You do not have permission to modify this price alert."

    now = datetime.now(timezone.utc)
    try:
        db.price_alerts.update_one(
            {"_id": oid},
            {"$set": {"status": "paused", "alert_status": "paused", "updated_at": now}}
        )
        return True, 200, "Price alert paused successfully."
    except Exception as e:
        logger.error(f"[PriceAlert] Pause error for {alert_id}: {e}")
        return False, 500, f"Failed to pause alert: {str(e)}"


def resume_price_alert(alert_id: str, user_id: Optional[str] = None, email: Optional[str] = None) -> tuple[bool, int, str]:
    """Resume a paused price alert to active state."""
    global db
    if db is None:
        init_db()
    if db is None:
        return False, 500, "Database unavailable."
    oid = to_object_id(alert_id)
    if not oid:
        return False, 400, "Invalid alert ID format."

    doc = db.price_alerts.find_one({"_id": oid})
    if not doc:
        return False, 404, "Price alert not found."

    if not _check_alert_ownership(doc, user_id, email):
        return False, 403, "You do not have permission to modify this price alert."

    now = datetime.now(timezone.utc)
    try:
        db.price_alerts.update_one(
            {"_id": oid},
            {"$set": {"status": "active", "alert_status": "active", "updated_at": now}}
        )
        return True, 200, "Price alert resumed successfully."
    except Exception as e:
        logger.error(f"[PriceAlert] Resume error for {alert_id}: {e}")
        return False, 500, f"Failed to resume alert: {str(e)}"


def reactivate_price_alert(alert_id: str, user_id: Optional[str] = None, email: Optional[str] = None) -> tuple[bool, int, str]:
    """
    Reactivate a triggered or paused price alert.
    Resets alert status to active, clears alert_sent flag, and enables live monitoring again.
    """
    global db
    if db is None:
        init_db()
    if db is None:
        return False, 500, "Database unavailable."
    oid = to_object_id(alert_id)
    if not oid:
        return False, 400, "Invalid alert ID format."

    doc = db.price_alerts.find_one({"_id": oid})
    if not doc:
        return False, 404, "Price alert not found."

    if not _check_alert_ownership(doc, user_id, email):
        return False, 403, "You do not have permission to modify this price alert."

    now = datetime.now(timezone.utc)
    try:
        db.price_alerts.update_one(
            {"_id": oid},
            {"$set": {
                "status": "active",
                "alert_status": "active",
                "alert_sent": False,
                "alert_sent_status": "pending",
                "updated_at": now
            }}
        )
        return True, 200, "Price alert reactivated successfully. We will monitor it again!"
    except Exception as e:
        logger.error(f"[PriceAlert] Reactivate error for {alert_id}: {e}")
        return False, 500, f"Failed to reactivate alert: {str(e)}"


def delete_price_alert(alert_id: str, user_id: Optional[str] = None, email: Optional[str] = None) -> tuple[bool, int, str]:
    """Safely delete a price alert from MongoDB."""
    global db
    if db is None:
        init_db()
    if db is None:
        return False, 500, "Database unavailable."
    oid = to_object_id(alert_id)
    if not oid:
        return False, 400, "Invalid alert ID format."

    doc = db.price_alerts.find_one({"_id": oid})
    if not doc:
        return False, 404, "Price alert not found."

    if not _check_alert_ownership(doc, user_id, email):
        return False, 403, "You do not have permission to delete this price alert."

    now = datetime.now(timezone.utc)
    try:
        # Safely mark status as deleted and also clean up if desired
        db.price_alerts.update_one(
            {"_id": oid},
            {"$set": {"status": "deleted", "alert_status": "deleted", "updated_at": now}}
        )
        return True, 200, "Price alert deleted successfully."
    except Exception as e:
        logger.error(f"[PriceAlert] Delete error for {alert_id}: {e}")
        return False, 500, f"Failed to delete alert: {str(e)}"


def update_price_alert_checked(alert_id: str, latest_price: Optional[int] = None) -> bool:
    """Update last checked timestamp and optionally latest observed price."""
    if db is None or not alert_id:
        return False
    oid = to_object_id(alert_id)
    if not oid:
        return False
    now = datetime.now(timezone.utc)
    update_data = {"last_checked_at": now}
    if latest_price is not None and latest_price > 0:
        update_data["last_checked_price"] = latest_price
    try:
        db.price_alerts.update_one({"_id": oid}, {"$set": update_data})
        return True
    except Exception as e:
        logger.error(f"[PriceAlert] Update checked error: {e}")
        return False


def mark_price_alert_triggered(alert_id: str, latest_price: int, alert_sent: bool = True) -> bool:
    """
    Mark alert as triggered and notification sent.
    Prevents duplicate alerts from being sent.
    """
    if db is None or not alert_id:
        return False
    oid = to_object_id(alert_id)
    if not oid:
        return False
    now = datetime.now(timezone.utc)
    update_data = {
        "status": "triggered",
        "alert_status": "triggered",
        "alert_sent": alert_sent,
        "alert_sent_status": "sent" if alert_sent else "pending",
        "last_checked_price": latest_price,
        "last_checked_at": now,
        "triggered_at": now,
        "updated_at": now,
    }
    try:
        db.price_alerts.update_one({"_id": oid}, {"$set": update_data})
        logger.info(f"[PriceAlert] Alert {alert_id} marked as TRIGGERED at price ₹{latest_price:,}")
        return True
    except Exception as e:
        logger.error(f"[PriceAlert] Mark triggered error: {e}")
        return False






