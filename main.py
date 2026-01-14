from fastapi import FastAPI
import time
import os

app = FastAPI()

@app.get("/")
def root():
    return {"message": "Cape Coast API running"}

@app.get("/health")
def health():
    return {"ok": True, "timestamp": int(time.time())}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8080))
    )
