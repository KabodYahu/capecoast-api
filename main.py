from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
import time
import os

app = FastAPI(title="Cape Coast Delivery API")

# -------------------------
# CONFIG (CHANGE LATER, NOT LOGIC)
# -------------------------
PLATFORM_SHARE = 0.60
DRIVER_SHARE = 0.40

# -------------------------
# REQUEST MODELS
# -------------------------
class OrderQuoteRequest(BaseModel):
    food_subtotal: float = Field(..., gt=0, description="Total food price (restaurant keeps 100%)")
    platform_fee: float = Field(..., ge=0, description="Flat platform fee")
    delivery_fee: float = Field(..., ge=0, description="Flat delivery fee")

# -------------------------
# RESPONSE MODELS
# -------------------------
class OrderQuoteResponse(BaseModel):
    food_subtotal: float
    fees: dict
    margin_pool: float
    payouts: dict
    customer_total: float
    valid: bool
    timestamp: int

# -------------------------
# HEALTH & ROOT
# -------------------------
@app.get("/")
def root():
    return {"message": "Cape Coast API running"}

@app.get("/health")
def health():
    return {"ok": True, "timestamp": int(time.time())}

# -------------------------
# PRICING ENGINE
# -------------------------
@app.post("/orders/quote", response_model=OrderQuoteResponse)
def quote_order(data: OrderQuoteRequest):
    # Margin pool excludes food and tips
    margin_pool = round(data.platform_fee + data.delivery_fee, 2)

    if margin_pool <= 0:
        raise HTTPException(status_code=400, detail="Invalid margin pool")

    # Split margin
    platform_net = round(margin_pool * PLATFORM_SHARE, 2)
    driver_base = round(margin_pool * DRIVER_SHARE, 2)

    # Safety check (floating point edge cases)
    if round(platform_net + driver_base, 2) != margin_pool:
        driver_base = round(margin_pool - platform_net, 2)

    customer_total = round(data.food_subtotal + margin_pool, 2)

    return {
        "food_subtotal": data.food_subtotal,
        "fees": {
            "platform_fee": data.platform_fee,
            "delivery_fee": data.delivery_fee
        },
        "margin_pool": margin_pool,
        "payouts": {
            "restaurant": data.food_subtotal,
            "platform_net": platform_net,
            "driver_base": driver_base
        },
        "customer_total": customer_total,
        "valid": True,
        "timestamp": int(time.time())
    }

# -------------------------
# CLOUD RUN ENTRYPOINT
# -------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8080))
    )
