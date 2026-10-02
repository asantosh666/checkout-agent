"""The shopping agent: Gemini + function calling + PayPal.

A small manual tool-call loop (no agent framework) so the whole flow is
visible and debuggable: the LLM decides, we execute, it responds.
"""
import json
import os

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langchain_google_genai import ChatGoogleGenerativeAI

from paypal_client import PayPalClient

CATALOG_PATH = os.path.join(os.path.dirname(__file__), "catalog.json")

SYSTEM_PROMPT = """You are Checkout Agent, a friendly AI shopping assistant.
You help the user find products from OUR catalog and buy them with PayPal.

Rules:
- ONLY recommend products from the catalog (use search_catalog). Never invent products or prices.
- When the user describes what they want, search the catalog (at most 2 searches)
  and present the best 1-3 matches with exact prices. Then STOP searching and talk to the user.
- Ask which one they want. Do NOT create a PayPal order until the user explicitly confirms the specific product(s).
- Before creating the order, restate the product name(s) and total price and get a clear yes.
- BUNDLES (your wow move): if the user asks for a gift set, bundle, or "something for X under $Y",
  pick 2-4 products that fit the theme and stay under the budget, then create ONE order with
  create_paypal_order using comma-separated ids (e.g. "p01,p05,p06"). One order, one payment.
- After creating the order (create_paypal_order), tell the user to approve it with the PayPal button/link. The order is NOT paid until they approve and it is captured.
- ORDER TRACKING: if the user asks about their order ("where's my order?", "did my payment go through?"),
  call check_order_status. "My order" means their most recent order from the session context below.
  Report the status in plain words: CREATED = waiting for their approval, APPROVED = approved and will be
  captured, COMPLETED = paid. Never claim money moved unless status is COMPLETED.
- Keep replies short and chatty, like a helpful store clerk. Prices in USD.
"""

_catalog_cache = None


def _catalog():
    global _catalog_cache
    if _catalog_cache is None:
        with open(CATALOG_PATH) as f:
            _catalog_cache = json.load(f)["products"]
    return _catalog_cache


class ShoppingAgent:
    def __init__(self, paypal: PayPalClient, app_base_url: str,
                 gemini_api_key: str, model: str = "gemini-2.5-flash"):
        self.paypal = paypal
        self.app_base_url = app_base_url.rstrip("/")
        self.llm = ChatGoogleGenerativeAI(
            model=model, google_api_key=gemini_api_key, temperature=0.4)

        # -- tools (closures so they see self) --------------------------
        @tool
        def search_catalog(query: str, max_price: float = 0) -> str:
            """Search the product catalog. query: keywords (e.g. 'earbuds');
            max_price: optional upper price bound in USD (0 = no limit).
            Matches ANY keyword, best matches first. Search at most twice
            per request, then present what you found."""
            words = query.lower().split()
            scored = []
            for p in _catalog():
                text = f"{p['name']} {p['category']} {p['blurb']}".lower()
                score = sum(1 for w in words if w in text)
                if score and (not max_price or p["price"] <= max_price):
                    scored.append((score, p))
            scored.sort(key=lambda x: -x[0])
            return json.dumps([p for _, p in scored[:6]])

        @tool
        def create_paypal_order(product_ids: str) -> str:
            """Create ONE PayPal order for one or more catalog product ids.
            product_ids: comma-separated ids, e.g. 'p01' or 'p01,p05,p06' for a bundle.
            Call ONLY after the user confirmed the products and total price."""
            ids = [p.strip() for p in product_ids.split(",") if p.strip()]
            items = []
            for pid in ids:
                product = next((p for p in _catalog() if p["id"] == pid), None)
                if not product:
                    return json.dumps({"error": f"unknown product_id {pid}"})
                items.append({"name": product["name"],
                              "unit_price": product["price"], "quantity": 1})
            order = self.paypal.create_order(
                items,
                return_url=f"{self.app_base_url}/api/return",
                cancel_url=f"{self.app_base_url}/api/cancel",
            )
            approve = PayPalClient.approval_url(order)
            total = sum(i["unit_price"] for i in items)
            label = items[0]["name"] if len(items) == 1 else \
                f"Bundle: {' + '.join(i['name'] for i in items)}"
            return json.dumps({
                "order_id": order["id"],
                "status": order["status"],
                "approval_url": approve,
                "product": label,
                "amount": f"{total:.2f}",
            })

        @tool
        def check_order_status(order_id: str) -> str:
            """Check the current status of a PayPal order. Use when the user
            asks 'where is my order?' or 'did my payment go through?'."""
            order = self.paypal.get_order(order_id)
            pu = (order.get("purchase_units") or [{}])[0]
            amount = pu.get("amount") or {}
            return json.dumps({
                "order_id": order["id"],
                "status": order["status"],
                "amount": amount.get("value"),
                "currency": amount.get("currency_code"),
                "description": pu.get("description"),
            })

        @tool
        def capture_paypal_order(order_id: str) -> str:
            """Capture an APPROVED PayPal order (moves the money)."""
            result = self.paypal.capture_order(order_id)
            pu = (result.get("purchase_units") or [{}])[0]
            cap = ((pu.get("payments") or {}).get("captures") or [{}])[0]
            return json.dumps({
                "order_id": result["id"],
                "status": result["status"],
                "capture_id": cap.get("id"),
                "amount": (cap.get("amount") or {}).get("value"),
                "currency": (cap.get("amount") or {}).get("currency_code"),
            })

        self._tools = [search_catalog, create_paypal_order,
                       check_order_status, capture_paypal_order]
        self._tool_map = {t.name: t for t in self._tools}
        self._llm_tools = self.llm.bind_tools(self._tools)

    def chat(self, history: list, user_text: str,
             recent_orders: list | None = None) -> tuple[str, dict | None, list]:
        """One turn. Returns (reply_text, order_info|None, updated_history)."""
        ctx = ""
        if recent_orders:
            lines = [f"- {o['order_id']}: {o.get('product', '')} "
                     f"(${o.get('amount', '')})" for o in recent_orders[-3:]]
            ctx = ("\n\nSession's recent orders, newest last "
                   "(use for 'my order' questions):\n" + "\n".join(lines))
        messages = [SystemMessage(content=SYSTEM_PROMPT + ctx), *history,
                    HumanMessage(content=user_text)]
        order_info = None
        for _ in range(8):  # max tool rounds per turn
            resp = self._llm_tools.invoke(messages)
            messages.append(resp)
            if not resp.tool_calls:
                break
            for tc in resp.tool_calls:
                fn = self._tool_map.get(tc["name"])
                try:
                    raw = fn.invoke(tc["args"])
                except Exception as e:  # never let a tool crash the chat
                    raw = json.dumps({"error": str(e)[:200]})
                if tc["name"] == "create_paypal_order":
                    try:
                        parsed = json.loads(raw)
                        if "order_id" in parsed:
                            order_info = parsed
                    except Exception:
                        pass
                messages.append(ToolMessage(content=str(raw), tool_call_id=tc["id"]))
        reply = messages[-1].content if messages else ""
        if isinstance(messages[-1], ToolMessage):
            # Loop ended on a tool result: ask the model for a closing reply, no more tools.
            closer = self.llm.invoke([
                *messages,
                HumanMessage(content="Present your recommendation as a short chat reply "
                                    "based on the tool results above. Do not call more tools."),
            ])
            messages.append(closer)
            reply = closer.content
        if isinstance(reply, list):  # content blocks
            reply = " ".join(b.get("text", "") for b in reply if isinstance(b, dict))
        new_history = messages[1:]  # drop system prompt before storing
        return str(reply), order_info, new_history
