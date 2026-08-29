# Shelf + Parity + Guardrail

A discover -> price fairly -> buy -> verify pipeline sitting in front of Razorpay's payment infrastructure, with a backend API layer and a two-view frontend (customer chat + merchant/audit dashboard). See [BUILD_SPEC.md](BUILD_SPEC.md) for the full spec and [trust-but-verify-agent-spec.md](trust-but-verify-agent-spec.md) for the pitch narrative this was built from.

- **Shelf** (`shelf/`) — catalog search: `search_catalog(query, max_budget_inr)`.
- **Parity** (`parity/`) — pricing fairness check: `check_price_fairness(product_id, customer_id, offered_price_inr)`.
- **Guardrail** (`guardrail/`) — mandate-enforced, verified purchase execution: `execute_purchase(mandate_token, product_id, amount_inr)`. Only component that talks to Razorpay.
- **Agent** (`agent/agent.py`) — orchestrates Shelf -> Parity -> Guardrail per chat turn (`run_chat_turn`) and composes the natural-language reply + pipeline trace.
- **API** (`api/`) — FastAPI wrapper exposing the pipeline over HTTP (`/api/chat`, `/api/mandates`, `/api/audit-log`, `/api/batch-results`, `/api/catalog`, `/api/auth/*`, `/api/customer/*`) and serving the frontend.
- **Two separate auth systems**, both PBKDF2-HMAC-SHA256 hashed + HMAC-signed HttpOnly session cookies (same mechanics, different roles):
  - **Admin** (`api/auth.py`) — gates only the merchant/audit dashboard (`/api/audit-log`, `/api/batch-results`). Credentials from `.env` (`ADMIN_USERNAME` / `ADMIN_PASSWORD`).
  - **Customer** (`api/customer_auth.py`) — shoppers sign up / log in (`frontend/customer_login.html`) before the AI agent can act (`POST /api/chat`, `POST /api/mandates`, and `POST /api/catalog` — adding a product is a customer/marketplace-listing action, not an admin one — all require a valid session). `customer_id` is derived from the verified session server-side, never trusted from the request body. New signups are appended to `data/customer_profiles.json` as real first-time customers (eligible for Parity's first-time-promo factor). `GET /api/catalog` (browsing) stays public either way.
  - Both are a deliberate reversal of `BUILD_SPEC.md` section 14's "no login system," made explicitly at request.
- **Frontend** (`frontend/`) — `index.html`: a real storefront (products from `data/catalog.json` as cards, each with a real photo via Lorem Picsum, seeded per `product_id` so it's stable) with a slide-in AI agent panel. Clicking "AI Shopping Agent" redirects to `customer_login.html` unless already signed in. The **same chat panel** handles both buying and listing a product, marketplace-style, routed by intent (`dispatchChatMessage` in `app.js`): a normal request goes through the buy pipeline, while "Add product ..." (either one free-form message with all the details, or just "add product" to be walked through each field conversationally) creates a new catalog listing via `POST /api/catalog`. The agent panel shows a brief "thinking" indicator (2-5s) then reveals the result directly — no stage-by-stage stepper narration to the customer; Search/Fairness Check/Execute/Verify still all run for real on the backend (see `agent/agent.py`'s `run_chat_turn`), the full stage trace just isn't surfaced in this view. The agent visibly acts on the storefront itself — the matching product card glows while "under review", then gets a green "Purchased & Verified" ribbon or a red "Blocked" tag. The mandate box includes a "Set" control so the user can define their own spending limit live. `dashboard.html` is the admin-only merchant/audit view (batch stat tiles + live audit log table + the full per-stage trace) — that's where the "explainable, bounded, gated, audited" evidence lives for anyone who wants to see the mechanism, not just the outcome. Plain HTML/CSS/JS, no framework.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in real Razorpay test-mode credentials if you have them
```

Without real credentials in `.env`, Guardrail's `razorpay_client.py` automatically runs in **mock mode** — deterministic fake order IDs and settlement responses — so every test, batch, and the app work end to end offline. Drop in real `rzp_test_...` credentials and Docker to also exercise the real Razorpay MCP Server via `verify_real_transaction.py` (see that file's docstring for exactly what it does and does not automate, and why).

## Run the app

```bash
python -m uvicorn api.server:app --reload --port 8000
```

Open `http://localhost:8000/` for the storefront, `http://localhost:8000/dashboard.html` for the audit dashboard (redirects to `login.html` if not signed in — default credentials `admin` / value of `ADMIN_PASSWORD` in `.env`). Shopping requires a customer account (`customer_login.html` — sign up takes a few seconds, no email verification). Every API endpoint is also directly curl-able, but `/api/chat` needs a logged-in session cookie first:

```bash
curl -c cookies.txt -X POST localhost:8000/api/customer/signup -H "Content-Type: application/json" \
  -d '{"username":"demo1","password":"demopass123","name":"Demo User"}'
curl -b cookies.txt -X POST localhost:8000/api/chat -H "Content-Type: application/json" \
  -d '{"session_id":"s1","message":"Get me wireless earbuds under 2000","mandate_id":"m_default"}'
```

Note on `/api/stream`: the spec's SSE endpoint was descoped in favor of its own stated fallback (return the full trace at once; the frontend animates through it with a short delay per stage) — same visual result, less backend complexity, and explicitly listed as the first thing to cut if short on time.

## Run the tests and batches

```bash
python shelf/test_shelf.py
python parity/test_parity.py
python guardrail/test_guardrail.py

python batch_tests/run_shelf_batch.py
python batch_tests/run_parity_batch.py
python batch_tests/run_guardrail_batch.py
python batch_tests/run_full_pipeline_batch.py   # writes batch_tests/results/*.json, read by /api/batch-results

python agent/agent.py   # deterministic Shelf -> Parity -> Guardrail terminal demo over agent/intents.json
```

To reset to a clean demo state (mandate spend, ledger, audit log):

```bash
python -c "import sys; sys.path.insert(0,'.'); from guardrail import guardrail; guardrail.reset_mandate_store()"
rm -f audit/audit_log.jsonl guardrail/ledger.json
```

## Real batch numbers (last run)

- **Shelf:** 30/30 correct outcomes (5 acceptance cases + 25 seeded intents from `data/test_intents.json`).
- **Parity:** 8/8 seeded-unfair entries caught (100% recall), 0/60 false positives, against the full 68-entry `data/pricing_log.json`.
- **Guardrail:** 23/23 correct outcomes (6 acceptance cases + 17 additional varied purchase attempts).
- **Full pipeline:** 25/25 test intents from `data/test_intents.json` resolved to their expected outcome end to end.

Re-run the batch scripts above to regenerate these numbers from scratch — the JSON output lives in `batch_tests/results/`, and `/api/batch-results` serves it straight to the dashboard.

## Repository structure

See `BUILD_SPEC.md` section 3 for the intended layout; this repo follows it, using the seed fixtures in `data/` as-is (per spec section 13) rather than regenerating data.

## Scope

Per `BUILD_SPEC.md` section 14: no animation-heavy or multi-page frontend beyond the two views in section 9, test-mode only, one mock merchant, no generic multi-platform abstraction. If you want something beyond what's specified, that's a deliberate scope decision to make explicitly, not something to add by default. One exception, made explicitly at request: the audit dashboard now sits behind real admin auth (see above) — the spec's original "no login system" applied to the customer-facing storefront, and that part is still account-free.
