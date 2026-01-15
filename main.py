# ============================================================
# Cape Coast Delivery API (MVP)
# - Orders: quote, create, confirm, status transitions
# - Drivers: register, list, set availability, assign to confirmed orders
#
# NOTE:
# - Uses in-memory dicts for storage (ORDERS_DB / DRIVERS_DB).
# - On Cloud Run, memory resets on redeploy and may not persist across instances.
# ============================================================


# ============================================================
# Imports
# ============================================================

from fastapi import FastAPI, HTTPException, Body
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
    version="0.2.0",
)


# ============================================================
# Temporary In-Memory Databases
# (Later: Firestore / Postgres)
# ============================================================

# Orders:
#   Key   -> order_id
#   Value -> full order record (dict)
ORDERS_DB: Dict[str, dict] = {}

# Drivers:
#   Key   -> driver_id
#   Value -> full driver record (dict)
DRIVERS_DB: Dict[str, dict] = {}


# ============================================================
# Economic / Pricing Logic
# (SINGLE SOURCE OF TRUTH)
# ============================================================

def calculate_quote(food_subtotal: float, platform_fee: float, delivery_fee: float) -> dict:
    """
    Core economic engine.

    RULES:
    - Restaurant keeps 100% of food subtotal
    - Platform + Driver split margin pool (platform_fee + delivery_fee)
    - Platform: 60%
    - Driver:   40%

    This function MUST be reused everywhere.
    """

    # ---- Fee pool (platform + delivery) ----
    margin_pool = platform_fee + delivery_fee

    # ---- Split of the margin pool ----
    platform_net = round(margin_pool * 0.60, 2)
    driver_base = round(margin_pool * 0.40, 2)

    # ---- What the customer pays ----
    customer_total = round(food_subtotal + margin_pool, 2)

    return {
        "food_subtotal": food_subtotal,
        "fees": {
            "platform_fee": platform_fee,
            "delivery_fee": delivery_fee,
        },
        "margin_pool": margin_pool,
        "payouts": {
            "restaurant": food_subtotal,
            "platform_net": platform_net,
            "driver_base": driver_base,
        },
        "customer_total": customer_total,
        "valid": True,
    }


# ============================================================
# Order Lifecycle Rules (BUSINESS LAW)
# ============================================================

# Centralized state machine:
# - Prevents illegal jumps (pending -> delivered)
# - Keeps behavior consistent across the entire platform
ALLOWED_TRANSITIONS = {
    "pending": ["confirmed", "cancelled"],
    "confirmed": ["assigned", "cancelled"],
    "assigned": ["picked_up", "cancelled"],  # optional policy choice
    "picked_up": ["en_route"],
    "en_route": ["delivered"],
    "delivered": [],
    "cancelled": [],
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
    restaurant_id: str = Field(min_length=1)


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
    new_status: str = Field(min_length=1)


# ----------------------------
# Driver models
# ----------------------------

class RegisterDriverRequest(BaseModel):
    """
    Driver registration (temporary / basic).
    Later: add KYC, vehicle type, license, etc.
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
    If driver_id is omitted (or null), the system auto-selects
    the first available driver (simple MVP).
    """
    driver_id: Optional[str] = None


# ============================================================
# Helper Functions (keep logic out of endpoints)
# ============================================================

def now_ts() -> int:
    """Unix timestamp helper (seconds)."""
    return int(time.time())


def safe_transition(order: dict, requested_status: str) -> None:
    """
    Enforces the state machine.
    Mutates the order safely (in place).
    """
    current_status = order["status"]
    allowed = ALLOWED_TRANSITIONS.get(current_status, [])

    if requested_status not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid transition: {current_status} -> {requested_status}",
        )

    # ---- State change ----
    order["status"] = requested_status

    # ---- Timestamp tracking ----
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

    # ---- Compute quote (no persistence) ----
    return calculate_quote(
        payload.food_subtotal,
        payload.platform_fee,
        payload.delivery_fee,
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

    restaurant_id = payload.restaurant_id.strip()
    if not restaurant_id:
        raise HTTPException(status_code=400, detail="restaurant_id cannot be empty")

    # ---- Compute quote at creation time ----
    quote = calculate_quote(
        payload.food_subtotal,
        payload.platform_fee,
        payload.delivery_fee,
    )

    # ---- Generate identifiers / timestamps ----
    order_id = f"ORD-{uuid4().hex[:10].upper()}"
    timestamp = now_ts()

    # ---- Create order record ----
    order_record = {
        "order_id": order_id,
        "restaurant_id": restaurant_id,
        "status": "pending",
        "quote": quote,
        "created_at": timestamp,

        # ---- Status timestamps start here ----
        "status_timestamps": {"pending": timestamp},

        # ---- Driver assignment fields (filled later) ----
        "driver_id": None,
        "driver_payout_locked": None,
        "platform_payout_locked": None,
    }

    # ---- Persist in memory ----
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

    # ---- Lookup ----
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
    - Makes order eligible for driver assignment
    """

    # ---- Lookup ----
    order = ORDERS_DB.get(order_id)

    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    # ---- Enforce transition via the state machine ----
    safe_transition(order, "confirmed")

    return {
        "order_id": order_id,
        "status": order["status"],
        "confirmed_at": order["status_timestamps"]["confirmed"],
        "quote": order["quote"],
    }


# ============================================================
# 5) Generic Status Transition (admin/system use)
# ============================================================

@app.patch("/orders/{order_id}/status")
def update_order_status(order_id: str, payload: OrderStatusUpdate):
    """
    Safely moves an order through its lifecycle.
    Prevents illegal jumps (e.g. pending -> delivered).
    """

    # ---- Lookup ----
    order = ORDERS_DB.get(order_id)

    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    # ---- Transition ----
    safe_transition(order, payload.new_status.strip())

    return {
        "order_id": order_id,
        "new_status": order["status"],
        "status_timestamps": order.get("status_timestamps", {}),
    }


# ============================================================
# 6) DRIVER: Register, List, Availability, Assign
# ============================================================

@app.post("/drivers/register", response_model=DriverResponse)
def register_driver(payload: RegisterDriverRequest):
    """
    Create a driver in our system.
    """

    name = payload.name.strip()
    phone = payload.phone.strip()

    if not name:
        raise HTTPException(status_code=400, detail="name cannot be empty")
    if not phone:
        raise HTTPException(status_code=400, detail="phone cannot be empty")

    # ---- Create driver id + record ----
    driver_id = f"DRV-{uuid4().hex[:10].upper()}"

    record = {
        "driver_id": driver_id,
        "name": name,
        "phone": phone,
        "is_available": True,
        "current_order_id": None,
        "created_at": now_ts(),
    }

    # ---- Persist in memory ----
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

    Rule:
    - You cannot set available=True if currently on an order.
    """

    # ---- Lookup ----
    driver = DRIVERS_DB.get(driver_id)

    if not driver:
        raise HTTPException(status_code=404, detail="Driver not found")

    # ---- Policy enforcement ----
    if payload.is_available is True and driver.get("current_order_id"):
        raise HTTPException(
            status_code=400,
            detail="Driver is on an active order and cannot be set to available",
        )

    # ---- Apply change ----
    driver["is_available"] = payload.is_available

    return driver


@app.post("/orders/{order_id}/assign-driver")
def assign_driver(
    order_id: str,
    payload: Optional[AssignDriverRequest] = Body(default=None),
):
    """
    Assign a driver to a CONFIRMED order.

    Request body options:
    - (no body) or {}           => auto-assign first available driver
    - {"driver_id": "DRV-..."}  => manually assign that driver
    """

    # ---- Lookup order ----
    order = ORDERS_DB.get(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    # ---- Must be confirmed before assignment ----
    if order["status"] != "confirmed":
        raise HTTPException(
            status_code=400,
            detail=f"Order must be confirmed before assignment. Current: {order['status']}",
        )

    # ---- Prevent double assignment ----
    if order.get("driver_id"):
        raise HTTPException(status_code=409, detail="Order already has a driver assigned")

    # ============================================================
    # Driver Selection (manual or auto)
    # ============================================================

    if payload and payload.driver_id:
        # ---- Manual assignment path ----
        chosen_driver_id = payload.driver_id.strip()
        if not chosen_driver_id:
            raise HTTPException(status_code=400, detail="driver_id cannot be empty")

        driver = DRIVERS_DB.get(chosen_driver_id)
        if not driver:
            raise HTTPException(status_code=404, detail="Driver not found")

        # ---- Availability checks ----
        if driver.get("is_available") is not True or driver.get("current_order_id") is not None:
            raise HTTPException(status_code=409, detail="Driver is not available")

    else:
        # ---- Auto assignment path ----
        driver = pick_available_driver()

    # ============================================================
    # Lock Payouts (prevents later manipulation)
    # ============================================================

    driver_payout = order["quote"]["payouts"]["driver_base"]
    platform_payout = order["quote"]["payouts"]["platform_net"]

    order["driver_id"] = driver["driver_id"]
    order["driver_payout_locked"] = driver_payout
    order["platform_payout_locked"] = platform_payout

    # ============================================================
    # Transition Order -> assigned
    # ============================================================

    safe_transition(order, "assigned")

    # ============================================================
    # Update Driver State (driver now busy)
    # ============================================================

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
            "phone": driver["phone"],
        },
        "payouts_locked": {
            "driver_payout": driver_payout,
            "platform_payout": platform_payout,
        },
        "status_timestamps": order.get("status_timestamps", {}),
    }


# ============================================================
# 7A) DRIVER CONFIRMS PICKUP (PHOTO REQUIRED)
# ============================================================

class PickupRequest(BaseModel):
    """
    Driver must upload a photo of the picked-up items.
    Pickup CANNOT be confirmed without this.
    """
    pickup_photo_url: str = Field(min_length=10)


@app.post("/orders/{order_id}/pickup")
def confirm_pickup(order_id: str, payload: PickupRequest):
    """
    Driver confirms pickup at restaurant/store.

    RULES:
    - Order must be ASSIGNED
    - Pickup photo is mandatory
    """

    order = ORDERS_DB.get(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    if order["status"] != "assigned":
        raise HTTPException(
            status_code=400,
            detail=f"Pickup not allowed in state: {order['status']}",
        )

    # ---- Record pickup proof ----
    order["pickup_photo_url"] = payload.pickup_photo_url
    order["pickup_confirmed_at"] = now_ts()

    # ---- Transition lifecycle ----
    safe_transition(order, "picked_up")

    return {
        "order_id": order_id,
        "status": order["status"],
        "pickup_time": order["pickup_confirmed_at"],
        "message": "Pickup confirmed. Great work 🚀",
    }


# ============================================================
# 7B) START DELIVERY (BEGIN TIMER + GPS)
# ============================================================

@app.post("/orders/{order_id}/start-delivery")
def start_delivery(order_id: str):
    """
    Driver begins delivery after pickup.

    RULES:
    - Order must be PICKED_UP
    - Starts delivery timer
    - Enables GPS routing on frontend
    """

    order = ORDERS_DB.get(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    if order["status"] != "picked_up":
        raise HTTPException(
            status_code=400,
            detail=f"Cannot start delivery from state: {order['status']}",
        )

    order["delivery_started_at"] = now_ts()

    safe_transition(order, "en_route")

    return {
        "order_id": order_id,
        "status": order["status"],
        "delivery_started_at": order["delivery_started_at"],
        "message": "Delivery started. Drive safe 🛣️",
    }


# ============================================================
# 7C) ARRIVAL DETECTED (GPS / SYSTEM EVENT)
# ============================================================

@app.post("/orders/{order_id}/arrived")
def mark_arrival(order_id: str):
    """
    System marks arrival at destination based on GPS proximity.

    RULES:
    - Order must be EN_ROUTE
    - Enables delivery confirmation UI
    """

    order = ORDERS_DB.get(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    if order["status"] != "en_route":
        raise HTTPException(
            status_code=400,
            detail=f"Arrival not valid in state: {order['status']}",
        )

    order["arrival_detected_at"] = now_ts()

    return {
        "order_id": order_id,
        "arrival_time": order["arrival_detected_at"],
        "message": "Arrived at destination 📍",
    }


# ============================================================
# 7D) COMPLETE DELIVERY (PROOF ENFORCED)
# ============================================================

class CompleteDeliveryRequest(BaseModel):
    """
    Delivery completion payload.

    RULES:
    - leave_at_door → delivery_photo_url REQUIRED
    - hand_to_customer → handed_to_customer MUST be TRUE
    """
    delivery_photo_url: Optional[str] = None
    handed_to_customer: Optional[bool] = None


@app.post("/orders/{order_id}/complete")
def complete_delivery(order_id: str, payload: CompleteDeliveryRequest):
    """
    Completes delivery and releases driver.

    RULES:
    - Order must be EN_ROUTE
    - Arrival must be detected first
    - Proof rules enforced based on delivery type
    """

    order = ORDERS_DB.get(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    if order["status"] != "en_route":
        raise HTTPException(
            status_code=400,
            detail=f"Cannot complete delivery from state: {order['status']}",
        )

    if not order.get("arrival_detected_at"):
        raise HTTPException(
            status_code=400,
            detail="Arrival must be detected before completing delivery",
        )

    # ---- Default delivery type ----
    delivery_type = order.get("delivery_type", "hand_to_customer")

    # ========================================================
    # LEAVE AT DOOR → PHOTO REQUIRED
    # ========================================================
    if delivery_type == "leave_at_door":
        if not payload.delivery_photo_url:
            raise HTTPException(
                status_code=400,
                detail="Delivery photo required for leave-at-door orders",
            )
        order["delivery_photo_url"] = payload.delivery_photo_url

    # ========================================================
    # HAND TO CUSTOMER → CONFIRMATION REQUIRED
    # ========================================================
    if delivery_type == "hand_to_customer":
        if payload.handed_to_customer is not True:
            raise HTTPException(
                status_code=400,
                detail="Driver must confirm handoff to customer",
            )

    # ========================================================
    # FINALIZE DELIVERY
    # ========================================================

    order["delivered_at"] = now_ts()
    safe_transition(order, "delivered")

    # ---- Release driver ----
    driver = DRIVERS_DB.get(order["driver_id"])
    if driver:
        driver["current_order_id"] = None
        driver["is_available"] = True
        driver.setdefault("status_timestamps", {})
        driver["status_timestamps"]["available"] = now_ts()

    # ---- Delivery duration ----
    delivery_duration = (
        order["delivered_at"]
        - order.get("delivery_started_at", order["delivered_at"])
    )

    return {
        "order_id": order_id,
        "status": order["status"],
        "delivery_time_seconds": delivery_duration,
        "message": "Delivery complete 🎉 Thank you for your great work!",
        "payouts_finalized": {
            "driver_payout": order["driver_payout_locked"],
            "platform_payout": order["platform_payout_locked"],
        },
    }



# ============================================================
# Cloud Run Entrypoint
# ============================================================

if __name__ == "__main__":
    print(">>> Starting Cape Coast API <<<")

    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8080)),
        log_level="info",
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
