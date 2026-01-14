# ============================================================
# Imports
# ============================================================

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from typing import Dict
from uuid import uuid4
import time
import os

# ============================================================
# App Initialization
# ============================================================

app = FastAPI(
    title="Cape Coast Delivery API",
    description="Local-first food & grocery delivery platform",
    version="0.1.0"
)

# ============================================================
# Temporary In-Memory Database
# (Later: Firestore / Postgres)
# ============================================================

# Key   → order_id
# Value → full order record
ORDERS_DB: Dict[str, dict] = {}

# ============================================================
# Economic / Pricing Logic
# (SINGLE SOURCE OF TRUTH)
# ============================================================

def calculate_quote(food_subtotal: float, platform_fee: float, delivery_fee: float):
    """
    Core economic engine.

    RULES:
    - Restaurant keeps 100% of food subtotal
    - Platform + Driver split margin pool
    - Platform: 60%
    - Driver:   40%

    This function MUST be reused everywhere.
    """

    margin_pool = platform_fee + delivery_fee

    platform_net = round(margin_pool * 0.60, 2)
    driver_base  = round(margin_pool * 0.40, 2)

    customer_total = round(food_subtotal + margin_pool, 2)

    return {
        "food_subtotal": food_subtotal,
        "fees": {
            "platform_fee": platform_fee,
            "delivery_fee": delivery_fee
        },
        "margin_pool": margin_pool,
        "payouts": {
            "restaurant": food_subtotal,
            "platform_net": platform_net,
            "driver_base": driver_base
        },
        "customer_total": customer_total,
        "valid": True
    }

# ============================================================
# Request / Response Models
# ============================================================

class OrderQuoteRequest(BaseModel):
    food_subtotal: float = Field(gt=0)
    platform_fee: float = Field(ge=0)
    delivery_fee: float = Field(ge=0)


class CreateOrderRequest(OrderQuoteRequest):
    """
    Extends quote request.
    Locks restaurant identity into the order.
    """
    restaurant_id: str


class OrderResponse(BaseModel):
    order_id: str
    restaurant_id: str
    status: str
    quote: dict
    created_at: int


class OrderStatusUpdate(BaseModel):
    new_status: str

# ============================================================
# Health Check
# ============================================================

@app.get("/")
def root():
    """Service health check"""
    return {"message": "Cape Coast API running"}

# ============================================================
# 1️⃣ Quote Order (NO STORAGE)
# ============================================================

@app.post("/orders/quote")
def quote_order(payload: OrderQuoteRequest):
    """
    Returns pricing ONLY.
    No order is created.
    Used for checkout previews.
    """
    return calculate_quote(
        payload.food_subtotal,
        payload.platform_fee,
        payload.delivery_fee
    )

# ============================================================
# 2️⃣ Create Order (PERSIST)
# ============================================================

@app.post("/orders", response_model=OrderResponse)
def create_order(payload: CreateOrderRequest):
    """
    Creates a pending order.
    Economics are calculated but NOT yet finalized.
    """

    quote = calculate_quote(
        payload.food_subtotal,
        payload.platform_fee,
        payload.delivery_fee
    )

    order_id  = f"ORD-{uuid4().hex[:10].upper()}"
    timestamp = int(time.time())

    order_record = {
        "order_id": order_id,
        "restaurant_id": payload.restaurant_id,
        "status": "pending",  # lifecycle starts here
        "quote": quote,
        "created_at": timestamp
    }

    ORDERS_DB[order_id] = order_record
    return order_record

# ============================================================
# 3️⃣ Retrieve Order
# ============================================================

@app.get("/orders/{order_id}")
def get_order(order_id: str):
    """
    Fetch a persisted order.
    Used by customers, drivers, admins.
    """

    order = ORDERS_DB.get(order_id)

    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    return order

# ============================================================
# 4️⃣ Confirm Order (PAYMENT LOCK-IN)
# ============================================================

@app.post("/orders/{order_id}/confirm")
def confirm_order(order_id: str):
    """
    Confirms an order AFTER successful payment.

    Rules:
    - Only PENDING orders can be confirmed
    - Locks economics permanently
    - Makes order eligible for driver assignment
    """

    order = ORDERS_DB.get(order_id)

    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    if order["status"] != "pending":
        raise HTTPException(
            status_code=400,
            detail=f"Order cannot be confirmed from status {order['status']}"
        )

    order["status"] = "confirmed"

    order.setdefault("status_timestamps", {})
    order["status_timestamps"]["confirmed"] = int(time.time())

    return {
        "order_id": order_id,
        "status": "confirmed",
        "confirmed_at": order["status_timestamps"]["confirmed"],
        "quote": order["quote"]
    }

# ============================================================
# Order State Machine (BUSINESS LAW)
# ============================================================

ALLOWED_TRANSITIONS = {
    "pending":   ["confirmed", "cancelled"],
    "confirmed": ["assigned", "cancelled"],
    "assigned":  ["picked_up"],
    "picked_up": ["en_route"],
    "en_route":  ["delivered"],
    "delivered": [],
    "cancelled": []
}

# ============================================================
# 5️⃣ Generic Status Transition
# ============================================================

@app.patch("/orders/{order_id}/status")
def update_order_status(order_id: str, payload: OrderStatusUpdate):
    """
    Safely moves an order through its lifecycle.
    Prevents illegal jumps (e.g. pending → delivered).
    """

    order = ORDERS_DB.get(order_id)

    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    current_status   = order["status"]
    requested_status = payload.new_status

    allowed = ALLOWED_TRANSITIONS.get(current_status, [])

    if requested_status not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid transition: {current_status} → {requested_status}"
        )

    order["status"] = requested_status

    order.setdefault("status_timestamps", {})
    order["status_timestamps"][requested_status] = int(time.time())

    return {
        "order_id": order_id,
        "old_status": current_status,
        "new_status": requested_status,
        "status_timestamps": order["status_timestamps"]
    }

# ============================================================
# Cloud Run Entrypoint
# ============================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8080))
    )
