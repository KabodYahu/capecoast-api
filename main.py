# ============================================================
# Cape Coast Delivery API (MVP)
# - Orders: quote, create, confirm, status transitions
# - Drivers: register, list, set availability, assign to confirmed orders
#
# NOTE:
# - Uses in-memory dicts for storage (ORDERS_DB / DRIVERS_DB).
# - On Cloud Run, memory resets on redeploy and may not persist across instances.
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
    version="0.2.1",
)

# ============================================================
# Temporary In-Memory Databases
# (Later: Firestore / Postgres)
# ============================================================

ORDERS_DB: Dict[str, dict] = {}
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
    """

    margin_pool = platform_fee + delivery_fee
    platform_net = round(margin_pool * 0.60, 2)
    driver_base = round(margin_pool * 0.40, 2)
    customer_total = round(food_subtotal + margin_pool, 2)

    return {
        "food_subtotal": round(food_subtotal, 2),
        "fees": {
            "platform_fee": round(platform_fee, 2),
            "delivery_fee": round(delivery_fee, 2),
        },
        "margin_pool": round(margin_pool, 2),
        "payouts": {
            "restaurant": round(food_subtotal, 2),
            "platform_net": platform_net,
            "driver_base": driver_base,
        },
        "customer_total": customer_total,
        "valid": True,
    }

# ============================================================
# Order Lifecycle Rules (BUSINESS LAW)
# ============================================================

ALLOWED_TRANSITIONS = {
    "pending": ["confirmed", "cancelled"],
    "confirmed": ["assigned", "cancelled"],
    "assigned": ["picked_up", "cancelled"],
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
    restaurant_id: str = Field(min_length=1)

class OrderResponse(BaseModel):
    order_id: str
    restaurant_id: str
    status: str
    quote: dict
    created_at: int

class OrderStatusUpdate(BaseModel):
    new_status: str = Field(min_length=1)

# ----------------------------
# Driver models
# ----------------------------

class RegisterDriverRequest(BaseModel):
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
    driver_id: Optional[str] = None

# ----------------------------
# Driver location ping model
# ----------------------------

class DriverLocationPing(BaseModel):
    driver_id: Optional[str] = None
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    accuracy_meters: Optional[float] = Field(default=None, ge=0)

# ============================================================
# Helper Functions (keep logic out of endpoints)
# ============================================================

def now_ts() -> int:
    return int(time.time())

def safe_transition(order: dict, requested_status: str) -> None:
    requested_status = requested_status.strip()
    current_status = order["status"]
    allowed = ALLOWED_TRANSITIONS.get(current_status, [])

    if requested_status not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid transition: {current_status} -> {requested_status}",
        )

    order["status"] = requested_status
    order.setdefault("status_timestamps", {})
    order["status_timestamps"][requested_status] = now_ts()

def pick_available_driver() -> dict:
    for d in DRIVERS_DB.values():
        if d.get("is_available") is True and d.get("current_order_id") is None:
            return d
    raise HTTPException(status_code=409, detail="No available drivers right now")

def assert_driver_authorized(order: dict, driver_id: str) -> None:
    assigned_driver_id = order.get("driver_id")

    if not assigned_driver_id:
        raise HTTPException(status_code=400, detail="No driver assigned to this order")

    if driver_id != assigned_driver_id:
        raise HTTPException(status_code=403, detail="Driver not authorized for this order")

# ============================================================
# Health Check
# ============================================================

@app.get("/")
def root():
    return {"message": "Cape Coast API running", "ts": now_ts()}

# ============================================================
# 1) Quote Order (NO STORAGE)
# ============================================================

@app.post("/orders/quote")
def quote_order(payload: OrderQuoteRequest):
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
    restaurant_id = payload.restaurant_id.strip()
    if not restaurant_id:
        raise HTTPException(status_code=400, detail="restaurant_id cannot be empty")

    quote = calculate_quote(
        payload.food_subtotal,
        payload.platform_fee,
        payload.delivery_fee,
    )

    order_id = f"ORD-{uuid4().hex[:10].upper()}"
    timestamp = now_ts()

    order_record = {
        "order_id": order_id,
        "restaurant_id": restaurant_id,
        "status": "pending",
        "quote": quote,
        "created_at": timestamp,
        "status_timestamps": {"pending": timestamp},
        "driver_id": None,
        "driver_payout_locked": None,
        "platform_payout_locked": None,
        # Default delivery type for MVP
        "delivery_type": "hand_to_customer",
    }

    ORDERS_DB[order_id] = order_record
    return order_record

# ============================================================
# 3) Retrieve Order
# ============================================================

@app.get("/orders/{order_id}")
def get_order(order_id: str):
    order = ORDERS_DB.get(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    return order

# ============================================================
# 4) Confirm Order (PAYMENT LOCK-IN)
# ============================================================

@app.post("/orders/{order_id}/confirm")
def confirm_order(order_id: str):
    order = ORDERS_DB.get(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

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
    order = ORDERS_DB.get(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    safe_transition(order, payload.new_status)

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
    name = payload.name.strip()
    phone = payload.phone.strip()

    if not name:
        raise HTTPException(status_code=400, detail="name cannot be empty")
    if not phone:
        raise HTTPException(status_code=400, detail="phone cannot be empty")

    driver_id = f"DRV-{uuid4().hex[:10].upper()}"

    record = {
        "driver_id": driver_id,
        "name": name,
        "phone": phone,
        "is_available": True,
        "current_order_id": None,
        "created_at": now_ts(),
        "status_timestamps": {"created": now_ts()},
    }

    DRIVERS_DB[driver_id] = record
    return record

@app.get("/drivers", response_model=List[DriverResponse])
def list_drivers():
    return list(DRIVERS_DB.values())

@app.patch("/drivers/{driver_id}/availability", response_model=DriverResponse)
def set_driver_availability(driver_id: str, payload: SetDriverAvailabilityRequest):
    driver = DRIVERS_DB.get(driver_id)
    if not driver:
        raise HTTPException(status_code=404, detail="Driver not found")

    if payload.is_available is True and driver.get("current_order_id"):
        raise HTTPException(
            status_code=400,
            detail="Driver is on an active order and cannot be set to available",
        )

    driver["is_available"] = payload.is_available
    driver.setdefault("status_timestamps", {})
    driver["status_timestamps"]["availability_changed"] = now_ts()
    return driver

@app.post("/orders/{order_id}/assign-driver")
def assign_driver(
    order_id: str,
    payload: Optional[AssignDriverRequest] = Body(default=None),
):
    order = ORDERS_DB.get(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    if order["status"] != "confirmed":
        raise HTTPException(
            status_code=400,
            detail=f"Order must be confirmed before assignment. Current: {order['status']}",
        )

    if order.get("driver_id"):
        raise HTTPException(status_code=409, detail="Order already has a driver assigned")

    # Driver selection: manual or auto
    if payload and payload.driver_id:
        chosen_driver_id = payload.driver_id.strip()
        if not chosen_driver_id:
            raise HTTPException(status_code=400, detail="driver_id cannot be empty")

        driver = DRIVERS_DB.get(chosen_driver_id)
        if not driver:
            raise HTTPException(status_code=404, detail="Driver not found")

        if driver.get("is_available") is not True or driver.get("current_order_id") is not None:
            raise HTTPException(status_code=409, detail="Driver is not available")
    else:
        driver = pick_available_driver()

    # Lock payouts
    driver_payout = order["quote"]["payouts"]["driver_base"]
    platform_payout = order["quote"]["payouts"]["platform_net"]

    order["driver_id"] = driver["driver_id"]
    order["driver_payout_locked"] = driver_payout
    order["platform_payout_locked"] = platform_payout

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
            "phone": driver["phone"],
        },
        "payouts_locked": {
            "driver_payout": driver_payout,
            "platform_payout": platform_payout,
        },
        "status_timestamps": order.get("status_timestamps", {}),
    }

# ============================================================
# 6B) DRIVER: Live GPS tracking (order-level)
# ============================================================

@app.post("/orders/{order_id}/location")
def driver_location_ping(order_id: str, payload: DriverLocationPing):
    order = ORDERS_DB.get(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    assigned_driver_id = order.get("driver_id")
    if not assigned_driver_id:
        raise HTTPException(status_code=400, detail="Order has no assigned driver")

    if order["status"] not in ["assigned", "picked_up", "en_route"]:
        raise HTTPException(
            status_code=400,
            detail=f"Location updates not allowed in state: {order['status']}",
        )

    if payload.driver_id and payload.driver_id.strip() != assigned_driver_id:
        raise HTTPException(status_code=403, detail="Driver mismatch for this order")

    order["driver_last_location"] = {
        "lat": payload.lat,
        "lng": payload.lng,
        "accuracy_meters": payload.accuracy_meters,
        "ts": now_ts(),
    }

    order.setdefault("driver_location_history", [])
    order["driver_location_history"].append(order["driver_last_location"])

    if len(order["driver_location_history"]) > 50:
        order["driver_location_history"] = order["driver_location_history"][-50:]

    return {
        "order_id": order_id,
        "status": order["status"],
        "driver_id": assigned_driver_id,
        "last_location": order["driver_last_location"],
        "history_count": len(order["driver_location_history"]),
    }

# ============================================================
# 7) DRIVER DELIVERY FLOW (AUTHORIZED)
# ============================================================

class DriverActionRequest(BaseModel):
    driver_id: str = Field(min_length=5)

class PickupRequest(DriverActionRequest):
    pickup_photo_url: str = Field(min_length=10)

@app.post("/orders/{order_id}/pickup")
def confirm_pickup(order_id: str, payload: PickupRequest):
    order = ORDERS_DB.get(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    if order["status"] != "assigned":
        raise HTTPException(
            status_code=400,
            detail=f"Pickup not allowed in state: {order['status']}",
        )

    assert_driver_authorized(order, payload.driver_id)

    order["pickup_photo_url"] = payload.pickup_photo_url
    order["pickup_confirmed_at"] = now_ts()
    safe_transition(order, "picked_up")

    return {
        "order_id": order_id,
        "status": order["status"],
        "pickup_time": order["pickup_confirmed_at"],
        "message": "Pickup confirmed. Great work 🚀",
    }

@app.post("/orders/{order_id}/start-delivery")
def start_delivery(order_id: str, payload: DriverActionRequest):
    order = ORDERS_DB.get(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    if order["status"] != "picked_up":
        raise HTTPException(
            status_code=400,
            detail=f"Cannot start delivery from state: {order['status']}",
        )

    assert_driver_authorized(order, payload.driver_id)

    order["delivery_started_at"] = now_ts()
    safe_transition(order, "en_route")

    return {
        "order_id": order_id,
        "status": order["status"],
        "delivery_started_at": order["delivery_started_at"],
        "message": "Delivery started. Drive safe 🛣️",
    }

@app.post("/orders/{order_id}/arrived")
def mark_arrival(order_id: str):
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

class CompleteDeliveryRequest(DriverActionRequest):
    delivery_photo_url: Optional[str] = None
    handed_to_customer: Optional[bool] = None

@app.post("/orders/{order_id}/complete-delivery")
def complete_delivery(order_id: str, payload: CompleteDeliveryRequest):
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

    assert_driver_authorized(order, payload.driver_id)

    delivery_type = order.get("delivery_type", "hand_to_customer")

    if delivery_type == "leave_at_door":
        if not payload.delivery_photo_url:
            raise HTTPException(
                status_code=400,
                detail="Delivery photo required for leave-at-door orders",
            )
        order["delivery_photo_url"] = payload.delivery_photo_url

    if delivery_type == "hand_to_customer":
        if payload.handed_to_customer is not True:
            raise HTTPException(
                status_code=400,
                detail="Driver must confirm handoff to customer",
            )

    order["delivered_at"] = now_ts()
    safe_transition(order, "delivered")

    # Release driver
    driver = DRIVERS_DB.get(order["driver_id"])
    if driver:
        driver["current_order_id"] = None
        driver["is_available"] = True
        driver.setdefault("status_timestamps", {})
        driver["status_timestamps"]["available"] = now_ts()

    delivery_duration = order["delivered_at"] - order.get("delivery_started_at", order["delivered_at"])

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
