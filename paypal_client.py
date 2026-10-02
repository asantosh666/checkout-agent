"""PayPal REST client (Orders v2) — sandbox-first.

All money operations happen server-side. The browser never sees the
client secret, and amounts are set here, never by the frontend.
"""
import base64
import os
import re
import time
import uuid

import httpx


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

SANDBOX_BASE = "https://api-m.sandbox.paypal.com"
LIVE_BASE = "https://api-m.paypal.com"


class PayPalError(Exception):
    pass


class PayPalClient:
    def __init__(self, client_id: str, secret: str, sandbox: bool = True):
        if not client_id or not secret:
            raise PayPalError("PayPal client_id/secret are required (see .env.example)")
        self.client_id = client_id
        self.secret = secret
        self.base = SANDBOX_BASE if sandbox else LIVE_BASE
        self._token: str | None = None
        self._token_expires_at: float = 0.0

    # -- auth -----------------------------------------------------------
    def _auth_header(self) -> str:
        raw = f"{self.client_id}:{self.secret}".encode()
        return "Basic " + base64.b64encode(raw).decode()

    def access_token(self) -> str:
        # Cache the token; sandbox tokens last several hours.
        if self._token and time.time() < self._token_expires_at - 60:
            return self._token
        resp = httpx.post(
            f"{self.base}/v1/oauth2/token",
            headers={"Authorization": self._auth_header()},
            data={"grant_type": "client_credentials"},
            timeout=20,
        )
        if resp.status_code != 200:
            raise PayPalError(f"OAuth failed: {resp.status_code} {resp.text[:200]}")
        data = resp.json()
        self._token = data["access_token"]
        self._token_expires_at = time.time() + int(data.get("expires_in", 30000))
        return self._token

    def _headers(self) -> dict:
        # PayPal-Request-Id makes POSTs idempotent: retries never double-charge.
        return {
            "Authorization": f"Bearer {self.access_token()}",
            "Content-Type": "application/json",
            "PayPal-Request-Id": str(uuid.uuid4()),
        }

    # -- orders ---------------------------------------------------------
    def create_order(self, items: list, return_url: str, cancel_url: str) -> dict:
        """Create an order with intent CAPTURE for one or more items.

        items: [{"name": str, "unit_price": float, "quantity": int}]
        Returns the full order JSON.
        """
        total = sum(i["unit_price"] * i["quantity"] for i in items)
        total_str = f"{total:.2f}"
        paypal_items = [{
            "name": i["name"][:127],
            "quantity": str(i["quantity"]),
            "unit_amount": {"currency_code": "USD",
                            "value": f"{i['unit_price']:.2f}"},
        } for i in items]
        body = {
            "intent": "CAPTURE",
            "purchase_units": [{
                "reference_id": f"agent-{uuid.uuid4().hex[:8]}",
                "description": (f"{items[0]['name']}" if len(items) == 1
                                else f"Bundle of {len(items)} items")
                             + " (Checkout Agent demo)",
                "amount": {
                    "currency_code": "USD",
                    "value": total_str,
                    "breakdown": {"item_total": {"currency_code": "USD",
                                                "value": total_str}},
                },
                "items": paypal_items,
            }],
            "application_context": {
                "brand_name": "Checkout Agent",
                "user_action": "PAY_NOW",
                "return_url": return_url,
                "cancel_url": cancel_url,
            },
        }
        resp = httpx.post(f"{self.base}/v2/checkout/orders",
                          headers=self._headers(), json=body, timeout=20)
        if resp.status_code not in (200, 201):
            raise PayPalError(f"create_order failed: {resp.status_code} {resp.text[:300]}")
        return resp.json()

    @staticmethod
    def approval_url(order: dict) -> str | None:
        for link in order.get("links", []):
            if link.get("rel") == "approve":
                return link["href"]
        return None

    def get_order(self, order_id: str) -> dict:
        resp = httpx.get(f"{self.base}/v2/checkout/orders/{order_id}",
                         headers=self._headers(), timeout=20)
        if resp.status_code != 200:
            raise PayPalError(f"get_order failed: {resp.status_code} {resp.text[:300]}")
        return resp.json()

    def capture_order(self, order_id: str) -> dict:
        """APPROVED != paid. This call moves the money."""
        resp = httpx.post(f"{self.base}/v2/checkout/orders/{order_id}/capture",
                          headers=self._headers(), timeout=20)
        if resp.status_code not in (200, 201):
            raise PayPalError(f"capture failed: {resp.status_code} {resp.text[:300]}")
        return resp.json()
