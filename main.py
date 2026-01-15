# ============================================================
# Cape Coast Delivery API (MVP)
# - Orders: quote, create, confirm, status transitions
# - Drivers: register, list, set availability, assign to confirmed orders
#
# SECURITY (added):
# - JWT auth + role-based access control
# - Roles: customer, driver, admin
#
# NOTE:
# - Uses in-memory dicts for storage (ORDERS_DB / DRIVERS_DB / USERS_DB).
# - On Cloud Run, memory resets on redeploy and may not persist across instances.
# ============================================================

from fastapi import FastAPI, HTTPException, Body, Depends
from pydantic import BaseModel, Field
from typing import Dict, Optional, List, Literal
from uuid import uuid4
import time
import os
import hashlib
import hmac

from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

# JWT dependency (install: pip install "python-jose[cryptography]")
from jose import jwt, JWTError

# ============================================================
# App Initialization
# ============================================================

app = FastAPI(
    title="Cape Coast Delivery API",
    description="Local-first food & grocery delivery platform",
    version="0.3.0",
)

# ============================================================
# Temporary In-Memory Databases
# ============================================================

ORDERS_DB: Dict[str, dict] = {}
DRIVERS_DB: Dict[str, dict] = {}
USERS_DB: Dict[str, dict] = {}  # auth users (customer/driver/admin)

# ============================================================
# Auth / JWT Config
# ============================================================

JWT_SECRET = os.environ.get("JWT_SECRET", "DEV_ONLY_CHANGE_ME")
JWT_ALG = os.environ.get("JWT_ALG", "HS256")
JWT_EXP_SECONDS = int(os.environ.get("JWT_EXP_SECONDS", "86400"))  # 24h default

auth_scheme = HTTPBearer(auto_error=True)

Role = Literal["customer", "driver", "admin"]


def now_ts() -> int:
    return int(time.time())


def _hash_password(password: str) -> str:
    """
    MVP password hashing (HMAC-SHA256 w/ server secret).
    For production: use passlib bcrypt/argon2 + per-user salt.
    """
    msg = password.encode("utf-8")
    key = JWT_SECRET.encode("utf-8")
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def create_user(*, email: str, password: str, role: Role, driver_id: Optional[str] = None) -> dict:
    user_id = f"USR-{uuid4().hex[:10].upper()}"
    record = {
        "user_id": user_id,
        "email": email.lower().strip(),
        "password_hash": _hash_password(password),
        "role": role,
        "driver_id": driver_id,
        "created_at": now_ts(),
    }
    USERS_DB[user_id] = record
    return record


def find_user_by_email(email: str) -> Optional[dict]:
    email = email.lower().strip()
    for u in USERS_DB.values():
        if u["email"] == email:
            return u
    return None


def create_access_token(user: dict) -> str:
    payload = {
        "sub": user["user_id"],
        "role": user["role"],
        "driver_id": user.get("driver_id"),
        "iat": now_ts(),
        "exp": now_ts() + JWT_EXP_SECONDS,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALG)


def get_current_user(creds: HTTPAuthorizationCredentials = Depends(auth_scheme)) -> dict:
    token = creds.credentials
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALG])
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    user_id = payload.get("sub")
    if not user_id or user_id not in USERS_DB:
        raise HTTPException(status_code=401, detail="User not found")

    # Return authoritative user record (not just token claims)
    user = USERS_DB[user_id]
    return user


def require_role(*allowed_roles: Role):
    def _guard(user: dict = Depends(get_current_user)) -> dict:
        if user["role"] not in allowed_roles:
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        return user
    return _guard


# ============================================================
# Seed Users (MVP)
# - Remove later; replace with proper registration + persistence.
# ============================================================

def seed_users_once():
    if USERS_DB:
        return

    # Admin
    create_user(email="admin@cape.co", password="admin123", role="admin")

    # Example customer
    create_user(email="customer@cape.co", password="customer123", role="customer")

    # Example driver (driver record created below at runtime in /drivers/register too)
    # We'll create a driver record now to link the driver user.
    driver_id = f"DRV-{uuid4().hex[:10].upper()}"
    DRIVERS_DB[driver_id] = {
        "driver_id": driver_id,
        "name": "Seed Driver",
        "phone": "0000000000",
        "is_available": True,
        "current_order_id": None,
        "created_at": now_ts(),
        "status_timestamps": {"created": now_ts()},
    }
    create_user(email="driver@cape.co", password="driver123", role="driver", driver_id=driver_id)


seed_users_once()

# ============================================================
# Economic / Pricing Logic
# ============================================================

def calculate_quote(food_subtotal: float, platform_fee: float, delivery_fee: float) -> dict:
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
# Order Lifecycle Rules
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

class LoginRequest(BaseModel):
    email: str = Field(min_length=5)
    password: str = Field(min_length=3)

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: Role
    user_id: str
    driver_id: Optional[str] = None
    expires_in: int

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
    customer_id: str

class OrderStatusUpdate(BaseModel):
    new_status: str = Field(min_length=1)

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

class DriverLocationPing(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    accuracy_meters: Optional[float] = Field(default=None, ge=0)

class DriverActionRequest(BaseModel):
    driver_id: str = Field(min_length=5)

class PickupRequest(DriverActionRequest):
    pickup_photo_url: str = Field(min_length=10)

class CompleteDeliveryRequest(DriverActionRequest):
    delivery_photo_url: Optional[str] = None
    handed_to_customer: Optional[bool] = None

# ============================================================
# Helper Functions
# ============================================================

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


def assert_order_access(order: dict, user: dict) -> None:
    """
    customer: must own order
    driver: must be assigned to order
    admin: full access
    """
    if user["role"] == "admin":
        return

    if user["role"] == "customer":
        if order.get("customer_id") != user["user_id"]:
            raise HTTPException(status_code=403, detail="Not allowed to view this order")
        return

    if user["role"] == "driver":
        if order.get("driver_id") != user.get("driver_id"):
            raise HTTPException(status_code=403, detail="Not allowed to view this order")
        return

    raise HTTPException(status_code=403, detail="Not allowed")


def assert_driver_user_matches(user: dict, driver_id: str) -> None:
    if user["role"] != "driver":
        raise HTTPException(status_code=403, detail="Driver role required")
    if user.get("driver_id") != driver_id:
        raise HTTPException(status_code=403, detail="Driver token does not match driver_id")


# ============================================================
# Health Check
# ============================================================

@app.get("/")
def root():
    return {"message": "Cape Coast API running", "ts": now_ts(), "version": app.version}


# ============================================================
# AUTH
# ============================================================

@app.post("/auth/login", response_model=TokenResponse)
def login(payload: LoginRequest):
    user = find_user_by_email(payload.email)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid email or password")

    if _hash_password(payload.password) != user["password_hash"]:
        raise HTTPException(status_code=401, detail="Invalid email or password")

    token = create_access_token(user)
    return {
        "access_token": token,
        "role": user["role"],
        "user_id": user["user_id"],
        "driver_id": user.get("driver_id"),
        "expires_in": JWT_EXP_SECONDS,
    }


@app.get("/auth/me")
def me(user: dict = Depends(get_current_user)):
    return {
        "user_id": user["user_id"],
        "email": user["email"],
        "role": user["role"],
        "driver_id": user.get("driver_id"),
        "created_at": user["created_at"],
    }


# ============================================================
# Orders: Quote (PUBLIC)
# ============================================================

@app.post("/orders/quote")
def quote_order(payload: OrderQuoteRequest):
    return calculate_quote(payload.food_subtotal, payload.platform_fee, payload.delivery_fee)


# ============================================================
# Orders: Create (CUSTOMER)
# ============================================================

@app.post("/orders", response_model=OrderResponse)
def create_order(
    payload: CreateOrderRequest,
    user: dict = Depends(require_role("customer", "admin")),  # allow admin for testing
):
    restaurant_id = payload.restaurant_id.strip()
    if not restaurant_id:
        raise HTTPException(status_code=400, detail="restaurant_id cannot be empty")

    quote = calculate_quote(payload.food_subtotal, payload.platform_fee, payload.delivery_fee)

    order_id = f"ORD-{uuid4().hex[:10].upper()}"
    ts = now_ts()

    order_record = {
        "order_id": order_id,
        "restaurant_id": restaurant_id,
        "status": "pending",
        "quote": quote,
        "created_at": ts,
        "status_timestamps": {"pending": ts},
        "driver_id": None,
        "driver_payout_locked": None,
        "platform_payout_locked": None,
        "delivery_type": "hand_to_customer",
        "customer_id": user["user_id"],
    }

    ORDERS_DB[order_id] = order_record
    return order_record


# ============================================================
# Orders: Retrieve (ROLE-AWARE)
# ============================================================

@app.get("/orders/{order_id}")
def get_order(order_id: str, user: dict = Depends(get_current_user)):
    order = ORDERS_DB.get(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    assert_order_access(order, user)
    return order


# ============================================================
# Orders: Confirm (CUSTOMER owns the order, or ADMIN)
# ============================================================

@app.post("/orders/{order_id}/confirm")
def confirm_order(order_id: str, user: dict = Depends(get_current_user)):
    order = ORDERS_DB.get(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    # customer must own, driver cannot confirm
    if user["role"] not in ("customer", "admin"):
        raise HTTPException(status_code=403, detail="Only customer or admin can confirm")

    assert_order_access(order, user)
    safe_transition(order, "confirmed")

    return {
        "order_id": order_id,
        "status": order["status"],
        "confirmed_at": order["status_timestamps"]["confirmed"],
        "quote": order["quote"],
    }


# ============================================================
# Orders: Generic Status Transition (ADMIN ONLY)
# ============================================================

@app.patch("/orders/{order_id}/status")
def update_order_status(
    order_id: str,
    payload: OrderStatusUpdate,
    user: dict = Depends(require_role("admin")),
):
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
# Drivers: Register/List/Availability (ADMIN for register/list; DRIVER for self availability)
# ============================================================

@app.post("/drivers/register", response_model=DriverResponse)
def register_driver(payload: RegisterDriverRequest, user: dict = Depends(require_role("admin"))):
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

    # Optional: create a linked driver user account (MVP default password)
    create_user(email=f"{driver_id.lower()}@drivers.cape.co", password="driver123", role="driver", driver_id=driver_id)

    return record


@app.get("/drivers", response_model=List[DriverResponse])
def list_drivers(user: dict = Depends(require_role("admin"))):
    return list(DRIVERS_DB.values())


@app.patch("/drivers/{driver_id}/availability", response_model=DriverResponse)
def set_driver_availability(
    driver_id: str,
    payload: SetDriverAvailabilityRequest,
    user: dict = Depends(require_role("driver", "admin")),
):
    driver_id = driver_id.strip()
    driver = DRIVERS_DB.get(driver_id)
    if not driver:
        raise HTTPException(status_code=404, detail="Driver not found")

    # driver can only modify themselves
    if user["role"] == "driver":
        assert_driver_user_matches(user, driver_id)

    if payload.is_available is True and driver.get("current_order_id"):
        raise HTTPException(status_code=400, detail="Driver is on an active order and cannot be set to available")

    driver["is_available"] = payload.is_available
    driver.setdefault("status_timestamps", {})
    driver["status_timestamps"]["availability_changed"] = now_ts()
    return driver


# ============================================================
# Orders: Assign Driver (ADMIN ONLY)
# ============================================================

@app.post("/orders/{order_id}/assign-driver")
def assign_driver(
    order_id: str,
    payload: Optional[AssignDriverRequest] = Body(default=None),
    user: dict = Depends(require_role("admin")),
):
    order = ORDERS_DB.get(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    if order["status"] != "confirmed":
        raise HTTPException(status_code=400, detail=f"Order must be confirmed before assignment. Current: {order['status']}")

    if order.get("driver_id"):
        raise HTTPException(status_code=409, detail="Order already has a driver assigned")

    if payload and payload.driver_id:
        chosen_driver_id = payload.driver_id.strip()
        driver = DRIVERS_DB.get(chosen_driver_id)
        if not driver:
            raise HTTPException(status_code=404, detail="Driver not found")
        if driver.get("is_available") is not True or driver.get("current_order_id") is not None:
            raise HTTPException(status_code=409, detail="Driver is not available")
    else:
        driver = pick_available_driver()

    driver_payout = order["quote"]["payouts"]["driver_base"]
    platform_payout = order["quote"]["payouts"]["platform_net"]

    order["driver_id"] = driver["driver_id"]
    order["driver_payout_locked"] = driver_payout
    order["platform_payout_locked"] = platform_payout

    safe_transition(order, "assigned")

    driver["is_available"] = False
    driver["current_order_id"] = order_id
    driver.setdefault("status_timestamps", {})
    driver["status_timestamps"]["assigned"] = now_ts()

    return {
        "order_id": order_id,
        "status": order["status"],
        "driver": {"driver_id": driver["driver_id"], "name": driver["name"], "phone": driver["phone"]},
        "payouts_locked": {"driver_payout": driver_payout, "platform_payout": platform_payout},
        "status_timestamps": order.get("status_timestamps", {}),
    }


# ============================================================
# Driver GPS ping (DRIVER ONLY, must match token driver_id)
# ============================================================

@app.post("/orders/{order_id}/location")
def driver_location_ping(
    order_id: str,
    payload: DriverLocationPing,
    user: dict = Depends(require_role("driver", "admin")),
):
    order = ORDERS_DB.get(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    if order.get("driver_id") is None:
        raise HTTPException(status_code=400, detail="Order has no assigned driver")

    if order["status"] not in ["assigned", "picked_up", "en_route"]:
        raise HTTPException(status_code=400, detail=f"Location updates not allowed in state: {order['status']}")

    if user["role"] == "driver":
        # driver can only update location for their assigned order
        if order.get("driver_id") != user.get("driver_id"):
            raise HTTPException(status_code=403, detail="Not your order")

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
        "driver_id": order.get("driver_id"),
        "last_location": order["driver_last_location"],
        "history_count": len(order["driver_location_history"]),
    }


# ============================================================
# Driver Delivery Flow (DRIVER ONLY)
# ============================================================

@app.post("/orders/{order_id}/pickup")
def confirm_pickup(
    order_id: str,
    payload: PickupRequest,
    user: dict = Depends(require_role("driver", "admin")),
):
    order = ORDERS_DB.get(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    if order["status"] != "assigned":
        raise HTTPException(status_code=400, detail=f"Pickup not allowed in state: {order['status']}")

    if user["role"] == "driver":
        assert_driver_user_matches(user, payload.driver_id)
        if order.get("driver_id") != user.get("driver_id"):
            raise HTTPException(status_code=403, detail="Not your order")

    assert_driver_authorized(order, payload.driver_id)

    order["pickup_photo_url"] = payload.pickup_photo_url
    order["pickup_confirmed_at"] = now_ts()
    safe_transition(order, "picked_up")

    return {"order_id": order_id, "status": order["status"], "pickup_time": order["pickup_confirmed_at"], "message": "Pickup confirmed."}


@app.post("/orders/{order_id}/start-delivery")
def start_delivery(
    order_id: str,
    payload: DriverActionRequest,
    user: dict = Depends(require_role("driver", "admin")),
):
    order = ORDERS_DB.get(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    if order["status"] != "picked_up":
        raise HTTPException(status_code=400, detail=f"Cannot start delivery from state: {order['status']}")

    if user["role"] == "driver":
        assert_driver_user_matches(user, payload.driver_id)
        if order.get("driver_id") != user.get("driver_id"):
            raise HTTPException(status_code=403, detail="Not your order")

    assert_driver_authorized(order, payload.driver_id)

    order["delivery_started_at"] = now_ts()
    safe_transition(order, "en_route")

    return {"order_id": order_id, "status": order["status"], "delivery_started_at": order["delivery_started_at"], "message": "Delivery started."}


@app.post("/orders/{order_id}/arrived")
def mark_arrival(
    order_id: str,
    user: dict = Depends(require_role("driver", "admin")),
):
    order = ORDERS_DB.get(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    if order["status"] != "en_route":
        raise HTTPException(status_code=400, detail=f"Arrival not valid in state: {order['status']}")

    # driver must be assigned
    if user["role"] == "driver":
        if order.get("driver_id") != user.get("driver_id"):
            raise HTTPException(status_code=403, detail="Not your order")

    order["arrival_detected_at"] = now_ts()
    return {"order_id": order_id, "arrival_time": order["arrival_detected_at"], "message": "Arrived at destination."}


@app.post("/orders/{order_id}/complete-delivery")
def complete_delivery(
    order_id: str,
    payload: CompleteDeliveryRequest,
    user: dict = Depends(require_role("driver", "admin")),
):
    order = ORDERS_DB.get(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    if order["status"] != "en_route":
        raise HTTPException(status_code=400, detail=f"Cannot complete delivery from state: {order['status']}")

    if not order.get("arrival_detected_at"):
        raise HTTPException(status_code=400, detail="Arrival must be detected before completing delivery")

    if user["role"] == "driver":
        assert_driver_user_matches(user, payload.driver_id)
        if order.get("driver_id") != user.get("driver_id"):
            raise HTTPException(status_code=403, detail="Not your order")

    assert_driver_authorized(order, payload.driver_id)

    delivery_type = order.get("delivery_type", "hand_to_customer")
    if delivery_type == "leave_at_door":
        if not payload.delivery_photo_url:
            raise HTTPException(status_code=400, detail="Delivery photo required for leave-at-door orders")
        order["delivery_photo_url"] = payload.delivery_photo_url

    if delivery_type == "hand_to_customer":
        if payload.handed_to_customer is not True:
            raise HTTPException(status_code=400, detail="Driver must confirm handoff to customer")

    order["delivered_at"] = now_ts()
    safe_transition(order, "delivered")

    driver = DRIVERS_DB.get(order.get("driver_id"))
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
