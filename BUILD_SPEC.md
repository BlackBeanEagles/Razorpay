# Build Specification: Shelf + Parity + Guardrail

This document is a direct build specification. Give this file to a coding AI (or use it yourself) to implement the system described below. It intentionally contains no pitch language — just what to build, how the pieces connect, and how to verify each piece works.

---

## 1. System summary

Build a pipeline with three backend components that sit between a buying agent and Razorpay's real payment infrastructure, plus a proper frontend so the whole workflow is visible and demoable end to end:

1. **Shelf** — a product catalog search tool.
2. **Parity** — a pricing-fairness checker.
3. **Guardrail** — a spending-mandate-enforcing, verifying proxy in front of Razorpay's official MCP Server.
4. **A backend API layer** wrapping all three so a frontend can call them.
5. **A frontend** with two views: a customer-facing shopping chat, and a merchant/audit dashboard.

Flow: a person types a buying intent into the chat UI → the agent calls Shelf to find a product → Parity checks the offered price is fair → the agent calls Guardrail to execute the purchase → Guardrail enforces the mandate, forwards to Razorpay's real MCP Server (test mode), then verifies via Razorpay's Settlements/reconciliation tool → the result and every step of the audit trail are shown live in the UI, not just logged to a file.

## 2. Prerequisites / environment setup

- A Razorpay account with **test-mode** API key and secret (Dashboard → Settings → API Keys).
- Docker installed, OR Go installed to build from source, for running Razorpay's official MCP Server (`razorpay/razorpay-mcp-server`, https://github.com/razorpay/razorpay-mcp-server).
- An Anthropic API key (or equivalent) for the agent's LLM calls with tool-calling support.
- Python 3.10+ or Node 18+ for the backend (pick one language and use it consistently across Shelf, Parity, Guardrail, and the API layer).
- A basic frontend toolchain: plain HTML/CSS/JS is sufficient and recommended for a hackathon timeline — do not introduce a heavy framework (React/Vue/etc.) unless the person building this is already fast in one; the UI described below has few enough components that plain JS with `fetch`/`EventSource` is genuinely simpler to finish.
- SQLite (or plain JSON files) for local data — no external database needed.

**First validation task before writing any other code:** confirm the Razorpay MCP Server accepts test-mode key/secret and returns valid responses for at least one tool call (e.g., create a test order). If test-mode auth doesn't work cleanly with the MCP Server, build a thin wrapper module that calls Razorpay's standard test-mode REST API directly instead, exposing the same function signatures used below — every other component should be unaffected by this choice.

## 3. Suggested repository structure

```
/shelf
  catalog.json / shelf.py / test_shelf.py

/parity
  customer_profiles.json, pricing_log.json / parity.py / test_parity.py

/guardrail
  mandates.json, ledger.json / mandate.py, guardrail.py, razorpay_client.py / test_guardrail.py

/agent
  agent.py                  # LLM agent wiring Shelf, Parity, Guardrail as tools
  intents.json                # batch test buying intents

/api
  server.py (or server.js)     # backend API layer, see section 8
  routes/
    chat.py                      # POST /api/chat
    mandates.py                    # mandate CRUD
    audit.py                         # audit log + batch results endpoints

/frontend
  index.html                  # customer chat view
  dashboard.html                 # merchant/audit view
  app.js                            # shared client logic (fetch calls, SSE handling)
  styles.css

/mockup
  ui_mockup.html               # provided — static visual reference, match this look and feel exactly

/audit
  audit_log.py / audit_log.jsonl (generated at runtime)

/batch_tests
  run_shelf_batch.py, run_parity_batch.py, run_guardrail_batch.py, run_full_pipeline_batch.py
  results/

/data                          # provided seed data, see section 13 — use as-is
  catalog.json, customer_profiles.json, pricing_log.json, mandates.json, test_intents.json

.env.example                  # provided — copy to .env and fill in real values
README.md
```

## 4. Feature 1: Shelf — catalog search

**Purpose:** given a natural-language buying intent (query text + budget), return the best-matching product(s) from a mock catalog, or a clear "no match" result.

**Data model — `data/catalog.json`** (already provided, 15 products):
```json
{
  "product_id": "p001", "name": "Wireless Earbuds X200", "category": "audio",
  "price_inr": 1799, "stock": 12, "rating": 4.3, "description": "Bluetooth 5.3 wireless earbuds, 24hr battery"
}
```

**Function signature:**
```
search_catalog(query: str, max_budget_inr: int | None) -> {
  "matches": [ {product_id, name, price_inr, rating, reason} ],
  "no_match_reason": str | None   # populated only if matches is empty
}
```
`reason` on each match should be a short, plain-language explanation of why it was selected (e.g., "best-rated option under budget").

**Acceptance criteria / test cases (put these in `test_shelf.py`, and use `data/test_intents.json` for the batch):**
1. Clear match: query with an obvious best product within budget → returns it with correct reasoning.
2. Multiple candidates: query matches several products → returns the best one by a stated, consistent rule (e.g., highest rating within budget), not arbitrarily.
3. No match: query below the cheapest available product's price → returns empty matches with a clear `no_match_reason`.
4. Out of stock: the only matching product has `stock == 0` → excluded from matches, reflected in `no_match_reason` if it was the only candidate.
5. Category mismatch: query for a category not in the catalog → clear no-match, not a hallucinated result.

## 5. Feature 2: Parity — pricing fairness check

**Purpose:** given a specific price being offered to a specific (simulated) customer for a specific product, determine whether that price is consistent with prices offered to other customers for the same product, allowing for declared legitimate factors, and flag it if not.

**Data — already provided:** `data/customer_profiles.json` (15 profiles) and `data/pricing_log.json` (68 entries, 8 marked `is_seeded_unfair: true` as ground truth).

**Function signature:**
```
check_price_fairness(product_id: str, customer_id: str, offered_price_inr: int) -> {
  "verdict": "fair" | "flagged",
  "reason": str,
  "comparison_baseline": float,
  "explained_by": str | None
}
```

**Logic guidance:** compute a baseline (median or mode price for the product across `pricing_log.json`). If the offered price deviates from that baseline beyond a threshold (e.g. >10%), check whether a declared legitimate factor for this customer explains the deviation in the expected direction (loyalty tier should mean *lower* price, not higher). Explained → "fair." Unexplained → "flagged."

**Acceptance criteria / test cases:**
1. Price at or near baseline → "fair," no factor needed.
2. Price below baseline, explained by a legitimate factor → "fair."
3. Price below baseline, no legitimate factor → "flagged."
4. Price above baseline, no legitimate factor → "flagged."
5. Run against the full `data/pricing_log.json`: report recall on the 8 seeded-unfair entries and the false-positive rate on the other 60 — report the real numbers, don't round up.

## 6. Feature 3: Guardrail — mandate enforcement, execution, verification

**Purpose:** the only component allowed to talk to Razorpay's real MCP Server. Enforces a spending mandate before forwarding any request, and verifies the result afterward before reporting success.

**Data — already provided:** `data/mandates.json` (4 test mandates covering the normal, expired, single-use-exhausted, and cumulative-spend cases).

**Function signatures:**
```
issue_mandate(merchant_id: str, max_amount_inr: int, expires_in_seconds: int, single_use: bool) -> signed_mandate_token

check_mandate(signed_mandate_token: str, requested_amount_inr: int) -> { "allowed": bool, "reason": str }

execute_purchase(signed_mandate_token: str, product_id: str, amount_inr: int) -> {
  "status": "success" | "blocked" | "failed_verification",
  "razorpay_order_id": str | None,
  "verification": {...},
  "reason": str
}
```

**Internal flow of `execute_purchase`:**
1. Call `check_mandate`. If not allowed, return `status: "blocked"` immediately — do not call Razorpay.
2. If allowed, call Razorpay's MCP Server (or REST fallback) to create an order and capture payment in test mode. Record the result in `ledger.json`.
3. Call Razorpay's Settlements tool (reconciliation). Compare: (a) amount charged per Razorpay's response, (b) amount expected per the ledger entry, (c) amount allowed per the mandate. All three must agree.
4. If they agree, update `amount_spent_so_far_inr` on the mandate, return `status: "success"`.
5. If they disagree, return `status: "failed_verification"` with a specific reason — never a silent success.

**Acceptance criteria / test cases:** (exercised concretely by `data/test_intents.json` and `data/mandates.json`)
1. Purchase within mandate, clean execution → `success`.
2. Purchase exceeding `max_amount_inr` → `blocked` before Razorpay is called at all.
3. Expired mandate → `blocked`.
4. Single-use mandate reused after one successful purchase → `blocked`.
5. Seeded settlement mismatch → `failed_verification` with a clear reason.
6. Cumulative spend across two purchases exceeding the mandate → second purchase `blocked`.

## 7. Agent orchestration

**Purpose:** the LLM agent that a user actually talks to. Wires Shelf, Parity, and Guardrail together as callable tools, in this order per turn:

1. Take the user's message and, if it's a buying intent, call `search_catalog`.
2. If a match is returned, call `check_price_fairness` for the chosen product/customer before proceeding.
3. If fairness passes, call `execute_purchase` (which internally does the mandate check → Razorpay call → verification, per section 6).
4. Compose a natural-language reply summarizing what happened at each stage — this reply, plus structured data about each stage, is what the API layer sends to the frontend (see section 8).
5. If Parity flags the price, or Guardrail blocks/fails verification, the agent must clearly state why in its reply — never silently succeed or silently fail.

The agent should also expose a `get_pipeline_trace()` style helper (or simply return trace data alongside its reply) so the frontend can show *which stage it's currently in* during a turn, not just the final outcome.

## 8. Backend API layer

**Purpose:** turn the modules above into something a web frontend can actually call. Keep this thin — it's a wrapper, not new business logic.

**Suggested framework:** FastAPI (Python) or Express (Node), whichever matches the language used for Shelf/Parity/Guardrail.

**Endpoints:**

```
POST /api/chat
  body: { session_id: str, message: str, mandate_id: str }
  # customer_id is deliberately NOT a body field -- it's derived server-side from the
  # authenticated session (see api/customer_auth.py's require_customer), so a client can't
  # spoof another customer's identity by just changing a request body value.
  returns: {
    agent_reply: str,
    pipeline_trace: [ { stage: "shelf"|"parity"|"guardrail", status: str, detail: {...} } ],
    product_card: {...} | null,      # populated when Shelf found a match worth showing
    purchase_result: {...} | null    # populated when a purchase was attempted this turn
  }

GET /api/mandates/:mandate_id
  returns: the mandate's current state (max_amount_inr, amount_spent_so_far_inr, expires_at, etc.)

POST /api/mandates
  body: { merchant_id, max_amount_inr, expires_in_seconds, single_use }
  returns: the new mandate (id + token)

GET /api/audit-log?limit=50
  returns: the most recent audit log entries, newest first

GET /api/batch-results
  returns: the latest output from the four batch_tests scripts (read from batch_tests/results/), for the dashboard

GET /api/stream (Server-Sent Events)
  purpose: pushes pipeline_trace updates live during a /api/chat turn, so the frontend can show
  "searching catalog... checking price fairness... executing payment... verifying settlement..."
  as it happens rather than only after the full response returns. If SSE is more than the time budget
  allows, a simpler fallback is acceptable: have /api/chat return the full trace at once and have the
  frontend animate through the trace array with a short delay per stage — visually similar result, much
  less backend complexity.
```

**Acceptance criteria:** every endpoint above should be callable with `curl` and return the documented shape, independent of the frontend existing yet — build and verify this layer before wiring up any UI.

## 9. Frontend — a real storefront, an agent panel, and a dashboard

**A visual reference mockup is provided: `mockup/ui_mockup.html`.** Open it in a browser first — it's a clickable, static (fake-data) preview of the exact look, layout, and interaction pattern to build for real. Match its visual language closely: the dark navy/charcoal palette with a blue-to-violet accent gradient, the rounded card style with subtle borders (not heavy shadows or bright colors), and the color coding (green = success/fair/purchased, amber = flagged, red = blocked). The mockup's three suggestion prompts ("earbuds under ₹2000," "over-mandate smartwatch," "out-of-stock power bank") demonstrate the three key demo scenarios — build the real app so these same three scenarios work against the real backend and real data in `/data`.

The core interaction model, and the thing that makes this demo land: **the agent doesn't operate in an isolated chat window — it visibly acts on the actual storefront the person is looking at.** When the agent finds a match, the matching product card on the page itself gets a glowing highlight and an "🤖 Agent is reviewing this" tag. When a purchase completes, that same card gets a green "✓ Purchased by Agent — Verified" ribbon. When something gets blocked, the card gets a red "⛔ Blocked" tag and stays unpurchased. This is what makes "bounded and gated" visible as something happening to a real product on a real page, not an abstraction described in a side panel.

### 9a. The storefront (`index.html`) — the primary demo screen

A real-looking e-commerce site, not a stripped-down form:

- **Header:** store name/logo, a (decorative, non-functional) search bar, nav links, a cart icon, and a prominent "AI Shopping Agent" button that opens the agent panel.
- **Product grid:** every product from `data/catalog.json` rendered as a real card — icon/thumbnail, name, star rating, price, stock status, and a "Buy Now" button for a normal human purchase path (this button can be a simple non-functional or lightly-functional add-to-cart for demo purposes — the AI purchase path is the one that matters and must be fully real).
- **Agent panel:** a slide-in panel (from the right, docked, not a separate page) containing: the active mandate shown as a balance/progress bar, the four-step pipeline status strip (Search → Fairness Check → Execute → Verify), a scrollable conversation log, and a text input with the same three demo prompts as suggestions.

**What must actually happen, driven by real API responses (not scripted like the mockup):**
1. User opens the agent panel and types a buying intent.
2. The panel calls `/api/chat`. As the response's `pipeline_trace` comes back (or streams via SSE per section 8), the matching product card in the grid gets found by `product_id` and highlighted live — scroll it into view and apply the "agent is reviewing" state.
3. As Parity and Guardrail's stages resolve, the stepper updates accordingly.
4. On `status: "success"`, the highlighted card switches to the "purchased and verified" state, and the agent panel shows the result card with the real Razorpay test-mode transaction ID.
5. On `status: "blocked"` or `"failed_verification"`, the card switches to the "blocked" state instead, and the panel shows the specific reason — never silently do nothing.

**User flow to actually demo:** open the agent panel, send a clean request (product gets found, highlighted, then marked purchased — full pipeline green) → send a request that exceeds the mandate (product gets found and highlighted, then marked blocked at the Execute step, with a clear reason) → optionally a third showing a no-match/out-of-stock case stopping at the Search step. Three short exchanges, all visibly acting on the real storefront, is enough for a compelling recording.

### 9b. Merchant / Audit Dashboard (separate page, linked from the header)

- The batch-result summaries from `/api/batch-results` — Shelf match rate, Parity flag rate + false-positive rate, Guardrail success/block breakdown — as simple stat tiles, not a chart library. This is where you show the honest numbers from your batch runs (see section 11).
- A scrollable, readable table of recent audit log entries (`/api/audit-log`), each row showing timestamp, component, event, and outcome.

**Why the dashboard still matters separately from the storefront demo:** the storefront proves the mechanism works and is understandable to a person watching one conversation; the dashboard proves it works at scale with real numbers, not just in the two or three scenarios you happened to demo live. Both are needed for the pitch — show the storefront live, then cut to the dashboard for 20-30 seconds to show the batch numbers.

This is not just "make it look nice" — it's specifically designed to make the four judging-relevant properties (explainable, bounded, gated, audited) visible on screen through real product cards changing state, not just claimed in narration. Keep the same restraint the mockup shows: no illustrations, no stock photography, no unnecessary animation beyond what signals pipeline progress — the polish comes from consistent spacing, a disciplined color palette, and clean typography, not decoration. No user login system, no pages beyond the storefront (with its agent panel) and the dashboard, no unrelated features.

## 10. Audit log — used by all backend components

**Format:** append-only JSON Lines (`audit_log.jsonl`), one entry per event:
```json
{
  "timestamp": "<iso8601>",
  "component": "shelf" | "parity" | "guardrail",
  "event": "search" | "fairness_check" | "mandate_check" | "purchase_forwarded" | "verification",
  "input_summary": {},
  "result_summary": {},
  "outcome": "ok" | "blocked" | "flagged" | "failed"
}
```
Every function call in Shelf, Parity, and Guardrail should write one entry here. This is what both the dashboard and the API's audit endpoint read from.

## 11. Batch test plan

Run these as actual scripts producing real output files — the numbers you report in the pitch, and the numbers shown on the dashboard, must come from actually running these.

- `run_shelf_batch.py`: run against `data/test_intents.json`. Output: match rate vs. `expected_shelf_outcome`.
- `run_parity_batch.py`: run against the full `data/pricing_log.json`. Output: recall on the 8 seeded-unfair entries, false-positive rate on the other 60.
- `run_guardrail_batch.py`: run against `data/mandates.json` + `data/test_intents.json`. Output: success rate, block-reason breakdown, reconciliation match rate.
- `run_full_pipeline_batch.py`: run all 25 entries in `data/test_intents.json` end to end. Output: full funnel breakdown vs. each entry's `expected_shelf_outcome` / `expected_guardrail_outcome`.

Write results to `batch_tests/results/` as JSON — this is what `/api/batch-results` reads for the dashboard.

## 12. Build order

1. Shelf (catalog + search + tests) — using `data/catalog.json`.
2. Razorpay MCP Server running locally, test-mode auth confirmed, one manual transaction.
3. Guardrail's mandate logic + forwarding (no verification yet) + tests — using `data/mandates.json`.
4. Guardrail's verification logic (Settlements/reconciliation check) + tests.
5. Parity (using `data/customer_profiles.json` + `data/pricing_log.json`) + tests.
6. Agent orchestration wiring Shelf → Parity → Guardrail as tools (section 7).
7. Backend API layer (section 8) — verify every endpoint with `curl` before touching the frontend.
8. Audit log integration across all backend components.
9. Run all four batch tests, capture real output files into `batch_tests/results/`.
10. Frontend: build the Customer Chat View first (9a) — this is the primary demo screen — then the Dashboard View (9b), which mostly just renders `/api/batch-results` and `/api/audit-log`.
11. Record the demo video from a real, working run — chat view live, then cut to dashboard for the batch numbers.

**If you're short on time, descope in this order:** cut the SSE live-updating status strip first (fall back to the "animate through the returned trace array" approach mentioned in section 8) → cut the dashboard's visual polish (plain tables are fine) → cut Parity entirely and ship Shelf + Guardrail alone with their two views → if truly squeezed, the frontend can be a single page combining a simplified chat and a results table rather than two separate pages. Never cut the batch-proof numbers, and never fall back to a backend-only, UI-less demo — a working frontend is now part of the core deliverable, not a stretch item.

## 13. Provided seed data — use these, don't invent your own

A `/data` folder ships alongside this spec:
- `data/catalog.json` — 15 products, including two deliberately out-of-stock items (p011, p014).
- `data/customer_profiles.json` — 15 simulated customers with varied loyalty tiers, first-time status, and order-size patterns.
- `data/pricing_log.json` — 68 simulated price offers, 8 marked `is_seeded_unfair: true` — Parity's ground-truth answer key.
- `data/mandates.json` — four test mandates (`m_default`, `m_expired`, `m_single_use_spent`, `m_cumulative_test`), each built to exercise one Guardrail rule; `_purpose` field explains each.
- `data/test_intents.json` — 25 buying intents with `expected_shelf_outcome` and `expected_guardrail_outcome` fields — the answer key for the full-pipeline batch and a good source of demo-worthy intents for the chat view.
- `.env.example` (repo root) — exact environment variable names expected. Copy to `.env`, fill in real values, never commit the real one.

## 14. Scope guardrails — what NOT to build

- No user login/authentication system — this is a prototype, use a single fixed demo customer/session, not real accounts.
- No animation-heavy or multi-page frontend beyond the two views in section 9 — the pipeline status strip and result cards are the important visual elements; don't go further than that.
- No support for real (non-test-mode) payments or production Razorpay credentials.
- No additional merchants, payment methods, or catalog categories beyond `data/catalog.json` — expand seed data deliberately if needed, don't let the AI invent new scope.
- No generic "plug in any e-commerce platform" abstraction layer — build concretely for this one mock merchant.
- No feature beyond what's specified in sections 4-9 unless the person running this build explicitly asks for it.

If the coding AI proposes adding something beyond this spec, treat that as a suggestion to bring back to a human for a decision — not something to build by default.

## 15. What "done" looks like

A running backend (API layer + Shelf + Parity + Guardrail) and a running frontend (chat view + dashboard view) that together let a person: open the chat view, type a buying intent, watch the pipeline status strip progress through Search → Fairness Check → Execute → Verify live, see a real Razorpay test-mode transaction ID on success, and then click through to the dashboard to see real batch numbers from all four batch test scripts. That full sequence, screen-recorded once for real, is the demo video.
