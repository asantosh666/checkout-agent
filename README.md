# 🛒 Checkout Agent

**An AI shopping assistant that finds products and checks out with PayPal — built for the [PayPal AI Hackathon](https://paypalaihackathon.devpost.com/).**

Tell it what you want in plain words (*"find me wireless earbuds under $60"*). It searches the catalog, recommends options, and — after you confirm — creates a PayPal order, walks you through approval, and captures the payment. All in the PayPal **sandbox**, so no real money moves.

## How it works

```
You ──chat──▶ Gemini agent ──tools──▶ catalog search
                                    ▶ PayPal Orders API v2 (create / capture)
                                    ▼
                           PayPal sandbox approval page
                                    ▼
                           receipt in chat
```

- **Agent:** Google Gemini with function calling (manual tool loop, no framework magic)
- **Payments:** PayPal Orders v2, `intent=CAPTURE`. Orders are created **and** captured server-side — the browser never sees secrets or amounts.
- **Catalog:** a static 12-product JSON (fast, deterministic demo). Swap in any product API later.
- **UI:** single-page chat, no build step.
- **Order tracking:** ask *"where's my order?"* anytime — the agent looks up the live PayPal status and explains it in plain words. The human approval step is deliberate: the agent can spend, but only with your okay.

## Run it (judges start here)

**Prerequisites:** Python 3.10+, a PayPal developer account, a Gemini API key ([AI Studio](https://aistudio.google.com), free tier is fine).

```bash
git clone <this repo> && cd checkout-agent/app
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

1. **PayPal:** at [developer.paypal.com](https://developer.paypal.com) → Dashboard → *Apps & Credentials* → *Sandbox* → *Create App* (type **Merchant**). Copy the **Client ID** and **Secret** into `.env`.
2. **Sandbox buyer:** Dashboard → *Sandbox accounts* → create a **Personal** account (note its email/password — you'll approve the test purchase with it).
3. **Gemini:** put your API key in `.env` as `GEMINI_API_KEY`.
4. **Run:** `uvicorn main:app --port 8000` → open http://localhost:8000

**Try the demo:** type *"find me wireless earbuds under $60"* → pick one → confirm → click **Approve with PayPal** → log in with the sandbox *Personal* account → approve → you're redirected back to a captured-payment receipt. ✅

To expose the demo publicly (for webhooks/remote judging), put the local server behind `ngrok http 8000` and set `APP_BASE_URL` to the https URL.

## Project structure

```
app/
  main.py          FastAPI server + PayPal return/cancel handlers
  agent.py         Gemini tool-calling loop + shopping system prompt
  paypal_client.py PayPal REST client (OAuth, create/get/capture order)
  catalog.json     12-product demo catalog
  static/index.html  chat UI
  requirements.txt
  .env.example
```

## Why this fits the hackathon

- **PayPal is the transaction rail**, not a bolt-on: every purchase flows through Orders v2.
- **Agentic commerce:** the AI doesn't just chat — it takes payment actions (create order, capture) via tools.
- **AI-assisted build:** scaffolded and iterated with AI coding assistance (allowed by the rules).

## License

MIT — see [LICENSE](LICENSE).
