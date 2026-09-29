"""
app.py
======
Smart-Buy: Multiplatform Price Review Comparison System
======================================================
Production Entry Point:
- Initializes Flask web application
- Initializes MongoDB connection & collections
- Mounts api.py Blueprint containing all application routes
"""

import os
import logging
from flask import Flask, session
from dotenv import load_dotenv
load_dotenv()

from database import init_db, get_user_by_id, get_user_unread_inbox_count, get_admin_unread_count
from api import api_bp
from services.price_alert_service import start_price_alert_scheduler

# App Setup
app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "smartbuy_super_secret_key_2026")

# Initialize the database locally or when a hosted MongoDB URI is configured.
# Vercel has no local MongoDB service, so avoid blocking function startup there.
if not os.getenv("VERCEL") or os.getenv("MONGO_URI"):
    init_db(app)
    # Start background price alert checker daemon
    start_price_alert_scheduler()

# Inject current logged-in user & inbox unread count into template context
@app.context_processor
def inject_user():
    user_id = session.get('user_id')
    admin_unread = 0
    if user_id:
        user = get_user_by_id(user_id)
        if user:
            unread_count = get_user_unread_inbox_count(user_id)
            if user.get('is_admin'):
                admin_unread = get_admin_unread_count()
            return dict(current_user=user, unread_inbox_count=unread_count, admin_unread_count=admin_unread)
    return dict(current_user=None, unread_inbox_count=0, admin_unread_count=0)

# Register Blueprint
app.register_blueprint(api_bp)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("smartbuy")

if __name__ == '__main__':
    port = int(os.getenv("PORT", 5000))
    logger.info(f"Starting Smart-Buy: Multiplatform Price Review Comparison System on port {port}...")
    app.run(host='0.0.0.0', port=port, debug=True, use_reloader=False)

