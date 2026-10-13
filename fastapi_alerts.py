"""
fastapi_alerts.py
=================
FastAPI Application and Router for SmartBuy Price Alert System:
- POST /price-alert (Create price alert in MongoDB)
- GET /price-alerts (Retrieve user's price alerts)
- PUT /price-alert/{id}/pause (Pause price monitoring)
- PUT /price-alert/{id}/resume (Resume price monitoring)
- PUT /price-alert/{id}/reactivate (Reactivate triggered price alert)
- DELETE /price-alert/{id} (Delete price alert)
- POST /price-alert/{id}/check (Check single alert against live scraper)
- POST /price-alerts/check-now (Trigger batch price check across all active alerts)

Reuses database.py (MongoDB) and services/price_alert_service.py (Scrapers & SMTP Email).
Can be run independently with Uvicorn:
    uvicorn fastapi_alerts:app --port 8000 --reload
"""

import os
import re
import logging
from typing import Optional, List, Dict, Any, Union
from datetime import datetime

from fastapi import FastAPI, APIRouter, HTTPException, Depends, Header, Query, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator

import database
from database import (
    create_price_alert,
    get_price_alert_by_id,
    get_user_price_alerts,
    get_active_price_alerts,
    pause_price_alert,
    resume_price_alert,
    reactivate_price_alert,
    delete_price_alert,
)
from services.price_alert_service import (
    run_price_check_now,
    check_single_alert,
    check_all_active_alerts,
)

logger = logging.getLogger("smartbuy.fastapi_alerts")

from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: ensure MongoDB is connected
    database.init_db()
    yield
    # Shutdown logic if any

# Initialize FastAPI App
app = FastAPI(
    title="SmartBuy Price Alert API",
    description="Multi-Platform Real-time Price Drop Alert & Notification System for Amazon, Flipkart, Meesho, and GEC",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan
)

# CORS Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

router = APIRouter(tags=["Price Alerts"])


# ════════════════════ PYDANTIC MODELS ════════════════════

class PriceAlertCreate(BaseModel):
    product_name: str = Field(..., description="Full title of the product")
    product_url: str = Field(..., description="Direct marketplace URL on Amazon, Flipkart, Meesho, or GEC")
    platform: str = Field(..., description="Shopping marketplace ('amazon', 'flipkart', 'meesho', 'gec')")
    current_price: Union[int, float, str] = Field(..., description="Current price at time of alert creation")
    target_price: Union[int, float, str] = Field(..., description="Target price below which to trigger alert")
    email: str = Field(..., description="User notification email address")
    user_id: Optional[str] = Field(None, description="Optional authenticated user ID")
    product_image: Optional[str] = Field("", description="Optional product image URL")

    @field_validator("email")
    @classmethod
    def validate_email_format(cls, v: str) -> str:
        if not v or not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", v.strip()):
            raise ValueError("Invalid email format")
        return v.strip().lower()

    model_config = {
        "json_schema_extra": {
            "example": {
                "product_name": "Vivo T4 5G (8GB RAM, 128GB)",
                "product_url": "https://www.flipkart.com/vivo-t4-5g/p/itm123456",
                "platform": "flipkart",
                "current_price": 27000,
                "target_price": 25000,
                "email": "shopper@example.com",
                "product_image": "https://rukminim2.flixcart.com/image/312/312/xif0q/mobile.jpeg"
            }
        }
    }


class PriceAlertResponse(BaseModel):
    success: bool
    message: str
    alert_id: Optional[str] = None
    alert: Optional[Dict[str, Any]] = None


class PriceAlertListResponse(BaseModel):
    success: bool
    count: int
    alerts: List[Dict[str, Any]]


class PriceAlertActionResponse(BaseModel):
    success: bool
    message: str
    alert_id: str
    status: Optional[str] = None


# ════════════════════ AUTH & IDENTITY HELPER ════════════════════

def get_requester_identity(
    x_user_id: Optional[str] = Header(None, alias="X-User-ID"),
    x_user_email: Optional[str] = Header(None, alias="X-User-Email"),
    user_id: Optional[str] = Query(None),
    email: Optional[str] = Query(None),
) -> Dict[str, Optional[str]]:
    """Resolve requester user_id and email from Headers or Query params."""
    resolved_id = x_user_id or user_id
    resolved_email = (x_user_email or email or "").strip().lower() or None
    return {"user_id": resolved_id, "email": resolved_email}


# ════════════════════ ENDPOINTS ════════════════════

@router.post(
    "/price-alert",
    response_model=PriceAlertResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new Price Alert in MongoDB"
)
async def create_alert_endpoint(
    payload: PriceAlertCreate,
    identity: Dict[str, Optional[str]] = Depends(get_requester_identity)
):
    """
    Create a new Price Alert in MongoDB collection `price_alerts`.
    Stores user_id, email, product name, product URL, platform, current price,
    target price, alert status, last checked price, and timestamps.
    """
    # Prefer payload user_id if present, else identity
    effective_user_id = payload.user_id or identity.get("user_id")
    effective_email = str(payload.email).strip().lower()

    alert_id, code, msg = create_price_alert(
        user_id=effective_user_id,
        email=effective_email,
        product_name=payload.product_name,
        product_url=payload.product_url,
        platform=payload.platform,
        current_price=payload.current_price,
        target_price=payload.target_price,
        product_image=payload.product_image or ""
    )

    if not alert_id or code >= 400:
        raise HTTPException(status_code=code, detail=msg)

    saved_doc = get_price_alert_by_id(alert_id)
    return PriceAlertResponse(
        success=True,
        message=msg,
        alert_id=alert_id,
        alert=saved_doc
    )


@router.get(
    "/price-alerts",
    response_model=PriceAlertListResponse,
    summary="Get user's price alerts"
)
async def list_alerts_endpoint(
    identity: Dict[str, Optional[str]] = Depends(get_requester_identity),
    status_filter: Optional[str] = Query(None, alias="status", description="Filter by status ('active', 'paused', 'triggered')")
):
    """
    Retrieve all price alerts for the authenticated user.
    Users can only access their own alerts by user ID or verified email.
    """
    uid = identity.get("user_id")
    uemail = identity.get("email")

    if not uid and not uemail:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Provide 'X-User-ID' header or 'user_id' / 'email' query parameter."
        )

    alerts = get_user_price_alerts(
        user_id=uid,
        email=uemail,
        status_filter=status_filter,
        include_deleted=False
    )

    return PriceAlertListResponse(
        success=True,
        count=len(alerts),
        alerts=alerts
    )


@router.put(
    "/price-alert/{alert_id}/pause",
    response_model=PriceAlertActionResponse,
    summary="Pause price alert monitoring"
)
async def pause_alert_endpoint(
    alert_id: str,
    identity: Dict[str, Optional[str]] = Depends(get_requester_identity)
):
    """Pause an active price alert. Paused alerts are skipped during background price checks."""
    ok, code, msg = pause_price_alert(
        alert_id=alert_id,
        user_id=identity.get("user_id"),
        email=identity.get("email")
    )
    if not ok:
        raise HTTPException(status_code=code, detail=msg)

    return PriceAlertActionResponse(
        success=True,
        message=msg,
        alert_id=alert_id,
        status="paused"
    )


@router.put(
    "/price-alert/{alert_id}/resume",
    response_model=PriceAlertActionResponse,
    summary="Resume paused price alert monitoring"
)
async def resume_alert_endpoint(
    alert_id: str,
    identity: Dict[str, Optional[str]] = Depends(get_requester_identity)
):
    """Resume a paused price alert to active state."""
    ok, code, msg = resume_price_alert(
        alert_id=alert_id,
        user_id=identity.get("user_id"),
        email=identity.get("email")
    )
    if not ok:
        raise HTTPException(status_code=code, detail=msg)

    return PriceAlertActionResponse(
        success=True,
        message=msg,
        alert_id=alert_id,
        status="active"
    )


@router.put(
    "/price-alert/{alert_id}/reactivate",
    response_model=PriceAlertActionResponse,
    summary="Reactivate triggered price alert"
)
async def reactivate_alert_endpoint(
    alert_id: str,
    identity: Dict[str, Optional[str]] = Depends(get_requester_identity)
):
    """
    Reactivate a triggered alert.
    Changes status back to active, clears alert_sent flag, and resumes monitoring.
    """
    ok, code, msg = reactivate_price_alert(
        alert_id=alert_id,
        user_id=identity.get("user_id"),
        email=identity.get("email")
    )
    if not ok:
        raise HTTPException(status_code=code, detail=msg)

    return PriceAlertActionResponse(
        success=True,
        message=msg,
        alert_id=alert_id,
        status="active"
    )


@router.delete(
    "/price-alert/{alert_id}",
    response_model=PriceAlertActionResponse,
    summary="Delete a price alert"
)
async def delete_alert_endpoint(
    alert_id: str,
    identity: Dict[str, Optional[str]] = Depends(get_requester_identity)
):
    """Safely delete a price alert from MongoDB."""
    ok, code, msg = delete_price_alert(
        alert_id=alert_id,
        user_id=identity.get("user_id"),
        email=identity.get("email")
    )
    if not ok:
        raise HTTPException(status_code=code, detail=msg)

    return PriceAlertActionResponse(
        success=True,
        message=msg,
        alert_id=alert_id,
        status="deleted"
    )


@router.post(
    "/price-alert/{alert_id}/check",
    summary="Trigger immediate price check for an alert"
)
async def check_single_alert_endpoint(
    alert_id: str,
    identity: Dict[str, Optional[str]] = Depends(get_requester_identity)
):
    """Fetch latest price for this alert using the platform scraper and check target condition."""
    doc = get_price_alert_by_id(alert_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Price alert not found")

    res = run_price_check_now(alert_id=alert_id)
    return {"success": True, "result": res}


@router.post(
    "/price-alerts/check-now",
    summary="Trigger immediate batch check on all active alerts"
)
async def check_all_alerts_endpoint():
    """Trigger background price checker cycle on all active alerts."""
    res = check_all_active_alerts(send_notifications=True)
    return {"success": True, "summary": res}


# Mount Router onto App
app.include_router(router)


@app.get("/health", tags=["Health"])
async def health_check():
    """FastAPI Health & Database status check."""
    db_ok = database.db is not None
    return {
        "status": "online",
        "service": "SmartBuy Price Alert API",
        "database_connected": db_ok,
        "time": datetime.now().isoformat()
    }
