# ============================================================
# Imports
# ============================================================

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from typing import Dict, Optional, List
from uuid import uuid4
import time
import os

# ============================================================
# App Initialization
# ============================================================

app = FastAPI(
    title="Cape Coast Delivery API",
    description="Local-first food & grocery delivery platform",
    version="0.2.0"
)

# ============================================================
# Temporary In-Memory Databases
# (Later: Firestore / Postgres)
# ============================================================

# Orders:
#   Key   → order_id
#   Value → full order record
ORDERS_DB: Dict[str, dict] = {}

# Drivers:
#   Key   → driver_id
#   Value → full driver record
DRIVERS_DB: Dict[str, dict] = {}

# ============================================================
# Economic / Pricing Logic
# (SINGLE SOURCE OF TRUTH)
# ============================================================

def calculate_quote(food_subtotal: float, platform_fee: float, delivery_fee: float):
    """
    Core economic engine.

    RULES:
    - Restaurant keeps 100% of food subtotal
    - Platform + Driver split margin pool (platform_fee + delivery_fee)
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
# Order Lifecycle Rules (BUSINESS LAW)
# ============================================================

# Centralized state machine:
# - Prevents illegal jumps (pending -> delivered)
# - Keeps behavior consistent across the entire platform
ALLOWED_TRANSITIONS = {
    "pending":   ["confirmed", "cancelled"],
    "confirmed": ["assigned", "cancelled"],
    "assigned":  ["picked_up", "cancelled"],   # you can allow cancel here, depending on policy
    "picked_up": ["en_route"],
    "en_route":  ["delivered"],
    "delivered": [],
    "cancelled": []
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
    """
    Generic status update model.
    """
    new_status: str


# ----------------------------
# Driver models
# ----------------------------

class RegisterDriverRequest(BaseModel):
    """
    Driver registration (temporary / basic).
    Later you'll add KYC, vehicle type, license, etc.
    """
    name: str = Field(min_length=1)
    phone: str = Field(min_length=6)


class DriverResponse(BaseModel):
    driver_id: str
    name: str
    phone: str
    is_available: bool
    current_order_id: Optional[str] = None
    created_at: int


class SetDriverAvailabilityRequest(BaseModel):
    is_available: bool


class AssignDriverRequest(BaseModel):
    """
    If driver_id is omitted, the system auto-selects
    the first available driver (simple MVP).
    """
    driver_id: Optional[str] = None


# ============================================================
# Helper Functions (keep logic out of endpoints)
# ============================================================

def now_ts() -> int:
    """Unix timestamp for consistent time tracking."""
    return int(time.time())


def safe_transition(order: dict, requested_status: str):
    """
    Enforces the state machine.
    Mutates the order safely.
    """
    current_status = order["status"]
    allowed = ALLOWED_TRANSITIONS.get(current_status, [])

    if requested_status not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid transition: {current_status} → {requested_status}"
        )

    order["status"] = requested_status
    order.setdefault("status_timestamps", {})
    order["status_timestamps"][requested_status] = now_ts()


def pick_available_driver() -> dict:
    """
    Simple MVP driver matching:
    - Pick the first available driver in DRIVERS_DB.
    Later: distance, ratings, fairness rotation, vehicle type, etc.
    """
    for d in DRIVERS_DB.values():
        if d.get("is_available") is True and d.get("current_order_id") is None:
            return d
    raise HTTPException(status_code=409, detail="No available drivers right now")


# ============================================================
# Health Check
# ============================================================

@app.get("/")
def root():
    """Service health check"""
    return {"message": "Cape Coast API running", "ts": now_ts()}


# ============================================================
# 1) Quote Order (NO STORAGE)
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
# 2) Create Order (PERSIST)
# ============================================================

@app.post("/orders", response_model=OrderResponse)
def create_order(payload: CreateOrderRequest):
    """
    Creates a pending order.
    Economics are calculated but NOT yet finalized (payment not confirmed).
    """

    quote = calculate_quote(
        payload.food_subtotal,
        payload.platform_fee,
        payload.delivery_fee
    )

    order_id  = f"ORD-{uuid4().hex[:10].upper()}"
    timestamp = now_ts()

    order_record = {
        "order_id": order_id,
        "restaurant_id": payload.restaurant_id,
        "status": "pending",  # lifecycle starts here
        "quote": quote,
        "created_at": timestamp,

        # driver assignment fields (filled later)
        "driver_id": None,
        "driver_payout_locked": None,
        "platform_payout_locked": None,
    }

    ORDERS_DB[order_id] = order_record
    return order_record


# ============================================================
# 3) Retrieve Order
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
# 4) Confirm Order (PAYMENT LOCK-IN)
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

    # Enforce transition via the state machine
    safe_transition(order, "confirmed")

    return {
        "order_id": order_id,
        "status": order["status"],
        "confirmed_at": order["status_timestamps"]["confirmed"],
        "quote": order["quote"]
    }


# ============================================================
# 5) Generic Status Transition (admin/system use)
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

    safe_transition(order, payload.new_status)

    return {
        "order_id": order_id,
        "new_status": order["status"],
        "status_timestamps": order.get("status_timestamps", {})
    }


# ============================================================
# 6) DRIVER: Register, List, Availability, Assign
# ============================================================

@app.post("/drivers/register", response_model=DriverResponse)
def register_driver(payload: RegisterDriverRequest):
    """
    Create a driver in our system.

    MVP behavior:
    - driver starts as available
    - no KYC/identity checks (later feature)
    """

    driver_id = f"DRV-{uuid4().hex[:10].upper()}"
    record = {
        "driver_id": driver_id,
        "name": payload.name,
        "phone": payload.phone,
        "is_available": True,
        "current_order_id": None,
        "created_at": now_ts()
    }

    DRIVERS_DB[driver_id] = record
    return record


@app.get("/drivers", response_model=List[DriverResponse])
def list_drivers():
    """
    List drivers (admin/testing).
    """
    return list(DRIVERS_DB.values())


@app.patch("/drivers/{driver_id}/availability", response_model=DriverResponse)
def set_driver_availability(driver_id: str, payload: SetDriverAvailabilityRequest):
    """
    Driver toggles availability.
    You cannot set available=True if currently on an order.
    """

    driver = DRIVERS_DB.get(driver_id)
    if not driver:
        raise HTTPException(status_code=404, detail="Driver not found")

    if payload.is_available is True and driver.get("current_order_id"):
        raise HTTPException(
            status_code=400,
            detail="Driver is on an active order and cannot be set to available"
        )

    driver["is_available"] = payload.is_available
    return driver


@app.post("/orders/{order_id}/assign-driver")
def assign_driver(
    order_id: str,
    payload: Optional[AssignDriverRequest] = Body(default=None)
):
    """
    Assign a driver to a CONFIRMED order.

    IMPORTANT:
    - Order MUST be confirmed first (payment lock-in)
    - Assignment locks the driver payout + platform payout
    - Driver becomes unavailable until delivery is completed
    """

    order = ORDERS_DB.get(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    # Must be confirmed to assign a driver
    if order["status"] != "confirmed":
        raise HTTPException(
            status_code=400,
            detail=f"Order must be confirmed before assignment. Current: {order['status']}"
        )

    # Prevent reassignment
    if order.get("driver_id"):
        raise HTTPException(status_code=409, detail="Order already has a driver assigned")

    # ----------------------------
    # Select driver (manual or auto)
    # ----------------------------
    if payload and payload.driver_id:
        driver_id = payload.driver_id.strip()
        if not driver_id:
            raise HTTPException(status_code=400, detail="driver_id cannot be empty")

        driver = DRIVERS_DB.get(driver_id)
        if not driver:
            raise HTTPException(status_code=404, detail="Driver not found")

        if not driver.get("is_available") or driver.get("current_order_id"):
            raise HTTPException(status_code=409, detail="Driver is not available")
    else:
        driver = pick_available_driver()

    # ----------------------------
    # Lock payouts at assignment time
    # ----------------------------
    driver_payout = order["quote"]["payouts"]["driver_base"]
    platform_payout = order["quote"]["payouts"]["platform_net"]

    order["driver_id"] = driver["driver_id"]
    order["driver_payout_locked"] = driver_payout
    order["platform_payout_locked"] = platform_payout

    # Transition order to assigned
    safe_transition(order, "assigned")

    # Update driver state
    driver["is_available"] = False
    driver["current_order_id"] = order_id
    driver.setdefault("status_timestamps", {})
    driver["status_timestamps"]["assigned"] = now_ts()

    return {
        "order_id": order_id,
        "status": order["status"],
        "driver": {
            "driver_id": driver["driver_id"],
            "name": driver["name"],
            "phone": driver["phone"]
        },
        "payouts_locked": {
            "driver_payout": driver_payout,
            "platform_payout": platform_payout
        },
        "status_timestamps": order.get("status_timestamps", {})
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
