"""Checkout Agent — FastAPI server.

Routes:
  GET  /                    -> chat UI
  POST /api/chat            -> one agent turn {message, session_id}
  GET  /api/return?token=   -> PayPal redirects here after approval; we capture
  GET  /api/cancel          -> PayPal redirects here on cancel
  GET  /api/orders/{id}     -> receipt / status lookup
  GET  /api/config          -> public config (never secrets)

Run:  uvicorn main:app --port 8000
Env:  see .env.example
"""
import os
import re

def _sanitize_proxy_env():
    """httpx fails to parse bracketed IPv6 entries (e.g. '[::1]') in
    no_proxy on some sandboxed hosts. Strip the brackets; harmless elsewhere."""
    for var in ("no_proxy", "NO_PROXY"):
        val = os.environ.get(var)
        if val and "[" in val:
            cleaned = ",".join(
                re.sub(r"[\[\]]", "", p.strip()) for p in val.split(",") if p.strip())
            os.environ[var] = cleaned

_sanitize_proxy_env()

import uuid
from collections import defaultdict

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agent import ShoppingAgent
from paypal_client import PayPalClient, PayPalError

load_dotenv()

BASE_DIR = os.path.dirname(__file__)
APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:8000")
SANDBOX = os.environ.get("PAYPAL_SANDBOX", "true").lower() != "false"

paypal = PayPalClient(
    client_id=os.environ.get("PAYPAL_CLIENT_ID", ""),
    secret=os.environ.get("PAYPAL_CLIENT_SECRET", ""),
    sandbox=SANDBOX,
)
agent = ShoppingAgent(
    paypal=paypal,
    app_base_url=APP_BASE_URL,
    gemini_api_key=os.environ.get("GEMINI_API_KEY", ""),
    model=os.environ.get("GEMINI_MODEL", "gemini-3.8-flash"),
)

app = FastAPI(title="Checkout Agent")
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")

# session_id -> {"history": [...], "receipts": {order_id: {...}}}
_sessions: dict = defaultdict(lambda: {"history": [], "receipts": {}})


@app.get("/")
def index():
    return FileResponse(os.path.join(BASE_DIR, "static", "index.html"))


@app.get("/api/config")
def config():
    return {"sandbox": SANDBOX}


@app.post("/api/chat")
async def chat(req: Request):
    body = await req.json()
    session_id = body.get("session_id") or str(uuid.uuid4())
    message = (body.get("message") or "").strip()
    if not message:
        return JSONResponse({"error": "empty message"}, status_code=400)
    sess = _sessions[session_id]
    try:
        reply, order_info, new_history = agent.chat(sess["history"], message)
    except Exception as e:
        return JSONResponse({"error": f"agent error: {str(e)[:200]}"}, status_code=500)
    sess["history"] = new_history[-30:]  # keep memory bounded
    return {"session_id": session_id, "reply": reply, "order": order_info}


@app.get("/api/return")
def paypal_return(token: str = ""):
    """PayPal redirects here after the buyer approves. Capture the order."""
    if not token:
        return RedirectResponse("/?error=no_token")
    try:
        result = paypal.capture_order(token)
    except PayPalError as e:
        return RedirectResponse(f"/?error={e}")
    pu = (result.get("purchase_units") or [{}])[0]
    cap = ((pu.get("payments") or {}).get("captures") or [{}])[0]
    receipt = {
        "order_id": result["id"],
        "status": result["status"],
        "capture_id": cap.get("id"),
        "amount": (cap.get("amount") or {}).get("value"),
        "currency": (cap.get("amount") or {}).get("currency_code"),
        "description": pu.get("description"),
    }
    # stash on every session (single-demo simplicity); UI looks it up by id
    for sess in _sessions.values():
        sess["receipts"][token] = receipt
    _sessions["latest"]["receipts"][token] = receipt
    return RedirectResponse(f"/?receipt={token}")


@app.get("/api/cancel")
def paypal_cancel():
    return RedirectResponse("/?cancelled=1")


@app.get("/api/orders/{order_id}")
def order_lookup(order_id: str):
    for sess in _sessions.values():
        if order_id in sess["receipts"]:
            return sess["receipts"][order_id]
    try:
        order = paypal.get_order(order_id)
        return {"order_id": order["id"], "status": order["status"]}
    except PayPalError as e:
        return JSONResponse({"error": str(e)[:200]}, status_code=404)
