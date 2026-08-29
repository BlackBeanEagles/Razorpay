# TechBazaar — Shelf + Parity + Guardrail

A discover -> price fairly -> buy -> verify pipeline sitting in front of Razorpay's payment infrastructure, with a backend API layer, a real-money-adjacent reconciliation loop, an MCP server for external AI buyers, and a two-view frontend (customer storefront + merchant/audit dashboard). See [ARCHITECTURE.md](ARCHITECTURE.md) for how it all fits together and how it maps to the hackathon tracks, [BUILD_SPEC.md](BUILD_SPEC.md) for the full spec, and [trust-but-verify-agent-spec.md](trust-but-verify-agent-spec.md) for the pitch narrative this was built from.

## Core pipeline

- **Shelf** (`shelf/`) — catalog search: `search_catalog(query, max_budget_inr)`. Typo/synonym-tolerant, honest "no match" reasons, hard feature filters (rating floor, quantity).
- **Parity** (`parity/`) — pricing fairness check: `check_price_fairness(product_id, customer_id, offered_price_inr)`. Recency-windowed baseline, tier-differentiated legitimate-discount caps, catalog-price fallback for products with no purchase history.
- **Guardrail** (`guardrail/`) — mandate-enforced, independently-verified purchase execution. Only component that talks to Razorpay (mock client for automated tests/batches, real test-mode REST client for human-completed Checkout). Race-free spend reservation under concurrent purchases (`_atomic_reserve_spend`, `_atomic_confirm_and_reserve` — see their docstrings for the concurrency bugs found and fixed here).
- **Agent** — two implementations sharing the same three functions above:
  - `agent/agent.py` — deterministic regex-routed fallback, used when `GROQ_API_KEY` isn't set. Run standalone: `python agent/agent.py`.
  - `agent/llm_agent.py` — the real agent, Groq tool-calling (OpenAI-compatible API), used by `/api/chat` whenever a real key is configured. Distinguishes browsing intent ("wireless earbuds") from explicit buy intent ("buy the earbuds") so it never attempts a purchase nobody asked for. Retries through Groq's tokens-per-minute rate limit via `agent/groq_client.py` (shared with `reconciliation/settlement_qa.py`).

## Beyond the core pipeline

- **Reconciliation** (`reconciliation/reconciliation.py`) — closes the internal-orders-vs-settlements finance-ops loop. Reconciles a fixed 53-order synthetic proof batch (`data/generate_reconciliation_seed.py`, ground-truth-verified) merged with every real transaction in `guardrail/ledger.json`, tagged `source: "live_ledger"` vs `"synthetic_seed"` so the two are never conflated. `reconciliation/settlement_qa.py` is a natural-language Q&A agent over that same real data (dashboard's "Ask about the books").
- **MCP server** (`mcp_server/techbazaar_mcp_server.py`) — exposes this merchant to *external* AI buyer agents (Claude Desktop, another agent framework) over the real Model Context Protocol, not just this project's own storefront chat. Same hardened Shelf/Parity/Guardrail functions underneath — an external agent gets no shortcut around any of Guardrail's rules. `mcp_server/verify_real_mcp_connection.py` proves it's a genuine, spec-compliant server by spawning it as a subprocess and driving it with a real MCP client.
- **Growth** (`growth/upsell.py`) — rule-based complementary-product suggestions (real catalog adjacency, not a fabricated "customers also bought" claim), offered by both the storefront agent and the MCP server after a successful purchase.
- **Support requests** (`audit/support_log.py`, `audit/mailer.py`) — a customer can file an issue (general, or from a specific blocked/flagged result) and gets a real confirmation email via [Resend](https://resend.com); admin resolving it on the dashboard sends a real follow-up email too. Needs `RESEND_API_KEY` in `.env`; without it, requests still log correctly, the email leg just reports itself honestly as unsent (see `audit/mailer.py`'s docstring).

## API, auth, and frontend

- **API** (`api/`) — FastAPI wrapper exposing the pipeline over HTTP and serving the frontend. Key routes: `/api/chat[/stream]`, `/api/mandates`, `/api/catalog`, `/api/purchase/{initiate,confirm}`, `/api/auth/*`, `/api/customer/*`, `/api/support-request[s]`, and the admin-only `/api/audit-log`, `/api/live-stats`, `/api/batch-results`, `/api/ai-agents`, `/api/settlement-qa`.
- **Two separate auth systems**, both PBKDF2-HMAC-SHA256 hashed + HMAC-signed HttpOnly session cookies, with cryptographically **domain-separated signing keys** (a customer token and an admin token are not cross-compatible — this closed a real vulnerability found during this project's own development, where a shared signing key let any logged-in shopper forge their way into the admin dashboard):
  - **Admin** (`api/auth.py`) — gates the merchant/audit dashboard's data endpoints. Credentials from `.env` (`ADMIN_USERNAME` / `ADMIN_PASSWORD`).
  - **Customer** (`api/customer_auth.py`) — shoppers sign up (name, email, username, password) / log in (`frontend/customer_login.html`) before the agent can act. `customer_id` is derived from the verified session server-side, never trusted from the request body. New signups are appended to `data/customer_profiles.json` as real first-time customers. `GET /api/catalog` (browsing) stays public.
  - Logout actually revokes the session server-side (`api/session_revocation.py`), not just a cosmetic cookie delete. Login is rate-limited (`api/rate_limit.py`) against brute-force. Both stores use cross-process file locking (`api/file_lock.py`) — safe under multiple worker processes.
- **Frontend** (`frontend/`) — `index.html`: the storefront (products from `data/catalog.json`) with a slide-in AI agent panel; the same chat handles buying and marketplace-style product listing, routed by intent. The agent visibly acts on the storefront itself (product cards glow, then get a "Purchased & Verified" or "Blocked" tag), offers a "Contact support" link on any checkout card, and naturally mentions a real upsell suggestion after a successful buy. `dashboard.html` is the admin-only view — **Live activity** (real counts only, tagged `source: "live"`, never test/batch noise — click a row to drill into its real failures), Connected AI agents, Ask about the books, Reconciliation exceptions, Support requests, and the full audit log. Plain HTML/CSS/JS, no framework.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in real credentials -- see that file's comments for what each is for
```

Without real Razorpay credentials in `.env`, Guardrail's `razorpay_client.py` automatically runs in **mock mode** — deterministic fake order IDs and settlement responses — so every test, batch, and the app work end to end offline. Without `GROQ_API_KEY`, chat falls back to the deterministic agent. Without `RESEND_API_KEY`, support requests still log correctly, just without the email leg.

## Run the app

```bash
python -m uvicorn api.server:app --reload --port 8000
```

Open `http://localhost:8000/` for the storefront, `http://localhost:8000/dashboard.html` for the audit dashboard (redirects to `login.html` if not signed in — default credentials `admin` / value of `ADMIN_PASSWORD` in `.env`). Shopping requires a customer account (`customer_login.html`).

## Run the tests and batches

```bash
python -m pytest   # every module's acceptance tests, including the source-tagging and email/mailer tests
```

The root `conftest.py` tags everything pytest generates as `source: "unit_test"` in the shared audit log, so a test run can never be mistaken for real customer activity on the dashboard.

```bash
python batch_tests/run_shelf_batch.py
python batch_tests/run_parity_batch.py
python batch_tests/run_guardrail_batch.py
python batch_tests/run_full_pipeline_batch.py
python batch_tests/run_reconciliation_batch.py
python batch_tests/run_upsell_batch.py
```

Each writes scored results to `batch_tests/results/*.json` (served by `/api/batch-results`) and self-tags as `source: "batch_test"`. `run_guardrail_batch.py` and `run_full_pipeline_batch.py` run against an **isolated** mandate store/ledger (`guardrail.use_isolated_store`), never the real ones — the same applies to `python agent/agent.py`'s standalone demo runner.

**Do not** reset the real mandate store/ledger by hand (`guardrail.reset_mandate_store()` called directly rebuilds `guardrail/mandates.json` from seed, discarding any real in-progress mandate — a real customer's, or an external AI buyer's mid-checkout purchase over MCP). If you need a clean demo slate, isolate first the same way the batch scripts do, or delete only the generated files (`audit/audit_log.jsonl`, `guardrail/ledger.json`) and re-run `python -c "from guardrail import guardrail; guardrail.reset_mandate_store()"` only when you're certain nothing real is in flight.

## Real batch numbers (last run)

- **Shelf:** 30/30 correct outcomes.
- **Parity:** 8/8 seeded-unfair entries caught (100% recall), 0/60 false positives.
- **Guardrail:** 23/23 correct outcomes.
- **Full pipeline:** 25/25 test intents resolved to their expected outcome end to end.
- **Reconciliation:** 42/53 synthetic orders matched cleanly (deliberately not all-clean — 14 seeded exceptions across 5 types), 100% classification accuracy against seeded ground truth.
- **Growth:** 26/26 real catalog products get a grounded complementary suggestion.

Re-run the batch scripts above to regenerate these from scratch. The dashboard's **Live activity** panel is separate from all of this — it's not a batch score, it's real counts that only move when a real customer or AI buyer actually does something.

## Repository structure

See `BUILD_SPEC.md` section 3 for the intended layout; this repo follows it, extended with `reconciliation/`, `mcp_server/`, and `growth/` for the additions above.
