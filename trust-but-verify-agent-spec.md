# Shelf + Parity + Guardrail: A Complete Discover-Price Fairly-Buy-Verify Pipeline for Razorpay's MCP Server
### Razorpay AI Buildathon — Track 1 (AI Growth & Agentic Commerce), with Track 4-grade reconciliation as the verification backbone

---

## 1. The one-line pitch

Razorpay's official MCP Server lets an AI agent pay for things, but gives it no way to discover what's for sale, no check that the price it's being offered is fair, and no built-in spending limits or verification. This project builds all three missing pieces — Shelf (discovery), Parity (pricing fairness), and Guardrail (safe execution + verification) — as one complete pipeline, built on Razorpay's own open-source tools, answering both an inferred infrastructure gap and a real, publicly-raised concern about their own product.

## 2. The three real gaps this fills

**Gap 1 — no catalog.** Razorpay's official MCP Server (`razorpay/razorpay-mcp-server`, open source on GitHub) exposes 50+ tools across Payments, Payment Links, Orders, Refunds, QR Codes, Settlements, Payouts, Tokens, Registration Links, and Integration Helpers — but nothing for product discovery. An agent can pay, but has no standard way to know what's for sale. This is also literally one of the buildathon brief's own named Track 1 example directions: "Agent-readable catalog."

**Gap 2 — no fairness check.** Razorpay's separate Agent Studio product (built on Anthropic's Claude Agent SDK) ships AI sales/negotiation agents. At its own public launch demo, an AI agent offered Razorpay's CEO escalating, personalized discounts — and tech press (Medianama) immediately raised an unanswered concern: this pattern could constitute price discrimination under India's Competition Act, 2002. There's no visible system checking that AI-driven pricing decisions are fair and explainable.

**Gap 3 — no governance.** Razorpay's real production feature for AI-agent spending limits and trust, UPI Reserve Pay, currently only exists for three hand-picked partners (Zomato, Swiggy, Zepto). Every other merchant using the open MCP Server today has raw, ungoverned payment tools — no spending limits, no verification, no audit trail.

**Shelf** solves Gap 1. **Parity** solves Gap 2. **Guardrail** solves Gap 3. Together they form one pipeline: discover → check the price is fair → buy within a safe, governed limit → verify it actually happened correctly.

## 3. How it works end to end

1. A merchant catalog is exposed via Shelf (10-15 mock products: name, price, stock, category, rating).
2. A human grants a spending mandate through Guardrail, modeled on Razorpay's real UPI Reserve Pay consent shape: *"This agent may spend up to ₹2000 at this merchant over the next 24 hours."*
3. A buying intent arrives: *"Get me wireless earbuds under ₹2000, good rating."*
4. The agent calls Shelf to search the catalog and picks a match, stating its reasoning.
5. Before proceeding, Parity checks the price/offer being shown to this customer against a log of prices shown to other simulated customers for the same product, flagging any statistically meaningful, unexplained difference (while correctly allowing legitimate reasons like loyalty tier or bulk order).
6. If the price passes the fairness check, the agent calls what it thinks is a normal payment tool — actually Guardrail. Guardrail checks the request against the spending mandate. Out-of-scope requests are blocked immediately, before Razorpay's real tools are touched.
7. If in scope, Guardrail forwards the request to Razorpay's real MCP Server (Orders/Payments tools) to execute a real test-mode transaction.
8. Guardrail then calls Razorpay's own Settlements tool (with reconciliation) to independently verify the transaction settled correctly, cross-checked against the mandate and Guardrail's internal ledger.
9. Only once verified is the agent told "success." Any mismatch — a fairness flag, a mandate violation, or a reconciliation failure — halts the flow, logs an exception with the specific reason, and escalates for human approval.
10. Every step — catalog search, fairness check, mandate check, forwarded call, verification result — is written to a structured, timestamped audit log.

## 4. Architecture

```
[Buying intent] ──▶ [Agent — Claude + tool-calling]
                          │
                          ▼
                 ┌─── Shelf (catalog search) ───┐
                 │  search_catalog(query,budget) │
                 └───────────────┬───────────────┘
                                  │ picks a product
                                  ▼
                 ┌─── Parity (fairness check) ───┐
                 │  check_price_fairness(product, │
                 │  price, customer_profile)      │
                 │  → allow / flag + reason        │
                 └───────────────┬───────────────┘
                                  │ price passes
                                  ▼
                 ┌────────── Guardrail ──────────┐
                 │ 1. mandate check (signed,      │
                 │    scoped spending limit)      │
                 │ 2. forward to real Razorpay MCP│──▶ [Razorpay's official
                 │    (Orders/Payments, test-mode)│    MCP Server]
                 │ 3. verify via Settlements tool │◀── (reconciliation)
                 │    (mandate + ledger cross-check)
                 └───────────────┬────────────────┘
                                  │
                                  ▼
                  [Audit log — every step, timestamped]
                                  │
                                  ▼
     [Dashboard: catalog-match accuracy, fairness flag rate + false-
      positive rate, mandate-compliance rate, reconciliation match rate]
```

## 5. The batch-proof (report all three layers honestly)

Run 20-30 pre-written buying intents through the full pipeline, plus a separate batch of 50+ simulated pricing interactions for Parity specifically (it needs more volume to say anything statistically meaningful about fairness).

*Shelf-side cases:* a clear match, a no-match, an out-of-stock item.
*Parity-side cases:* varied simulated customer profiles receiving the same product at different prices — some with legitimate reasons (loyalty tier, bulk order, first-time promo) and some with no reason (a seeded unfair pattern) — report flag rate and false-positive rate separately.
*Guardrail-side cases:* a request exceeding the mandate, an expired mandate, a seeded settlement mismatch, a couple of clean end-to-end successes.

Report three separate, honest numbers rather than one blended claim — e.g. *"Shelf: 28/30 correct matches. Parity: correctly flagged 6/6 seeded unfair patterns with 0 false positives against 44 legitimate variations. Guardrail: 27/30 purchases completed, settled, and reconciled within mandate — 3 correctly blocked, here's why each failed."*

## 6. Suggested tech stack

- **Agent brain:** Claude with tool-calling.
- **Shelf:** a small tool/API (Python or Node) serving a mock product catalog — SQLite or JSON is enough.
- **Parity:** a lightweight statistical check (compare prices shown to different simulated customer profiles for the same product; flag differences that exceed what's explained by declared legitimate factors) plus an LLM call to generate a plain-English explanation of any flag.
- **Guardrail:** your own MCP-compatible proxy server implementing mandate checks, forwarding, and verification.
- **Underlying payments:** Razorpay's official open-source MCP Server, run via Docker or built from source, authenticated with **test-mode** credentials.
- **Mandate signing:** simple HMAC-signed JSON token.
- **Audit log + dashboard:** structured log (JSON lines or SQLite) plus a clean summary view.

**Important early validation step:** confirm the Razorpay MCP Server works with test-mode credentials as your very first build task — this isn't explicitly documented, so verify it early. Fall back to Razorpay's standard test-mode REST APIs inside Guardrail's forwarding step if needed; the architecture and pitch narrative stay the same either way.

## 7. Build plan — realistic ordering, with fallbacks

Build in the order that leaves you with a demoable product at every checkpoint.

**Phase 1 — Shelf (fast, low-risk, immediately demoable):**
- Build the mock catalog and search tool; get an agent calling it and returning sensible matches with stated reasoning.
- Checkpoint: alone, this satisfies Track 1's "agent-readable catalog" example direction.

**Phase 2 — Real payment foundation:**
- Get Razorpay's real MCP Server running, confirm test-mode credentials (or set up the REST fallback).
- Get one manual test-mode transaction working end to end.

**Phase 3 — Guardrail's mandate layer:**
- Implement mandate signing/enforcement; wire the agent to call Guardrail (not Razorpay directly) for payment execution.
- Checkpoint: agent can search via Shelf, then buy via Guardrail, within a spending limit.

**Phase 4 — Verification layer:**
- Add the Settlements/reconciliation check after every forwarded transaction; seed 2-3 broken scenarios and confirm correct halting.

**Phase 5 — Parity's fairness layer:**
- Build the simulated customer-profile + pricing-history dataset (50+ interactions).
- Implement the fairness check and plain-English flagging.
- Wire it into the pipeline between Shelf's match and Guardrail's execution.
- Checkpoint: full pipeline — search, fairness check, mandate-gated purchase, verification — working end to end.

**Phase 6 — Batch proof + dashboard:**
- Run all three batches, capture real numbers, build the summary dashboard covering all three layers.

**Phase 7 — Pitch + buffer:**
- Record the 5-minute pitch: the three-gap framing → live example (search → fairness check → buy → verify) → batch numbers for all three → why this matters for Razorpay's own roadmap and its own public accountability question.
- Rehearse explaining the architecture for the panel interview.

**If you're short on time, descope in this order:** cut dashboard polish first, then shrink batch sizes, then cut Parity entirely and ship Shelf + Guardrail alone (still a complete, strong two-gap submission), and if truly squeezed, ship Shelf alone with a simple unmandated payment call. Never cut batch-proof numbers entirely — a single demo transaction with no batch evidence is the weakest version of this project.

## 8. What to say in the pitch (the narrative)

"Razorpay's MCP Server lets an AI agent pay for things — but it can't discover what's for sale, has no way to check the price it's offering is fair, and outside of three hand-picked partners, nothing stops it from overspending or trusts that a payment actually settled correctly. So I built the three missing pieces. Shelf gives any merchant a standard way to make their catalog agent-readable — literally one of your own named Track 1 example directions. Parity checks that AI-driven pricing decisions are fair and explainable — directly answering a concern your own team was publicly asked about when Agent Studio launched. Guardrail sits in front of your real MCP Server, enforcing a spending mandate modeled on your own UPI Reserve Pay design, and verifying every transaction with your own Settlements reconciliation tool before the agent is ever told it succeeded. Together, they're a complete discover-price fairly-buy-verify pipeline, built entirely on your own open-source infrastructure — and here are the real batch numbers for all three layers, including exactly where and why the system correctly refused to proceed."

## 9. Sources (verify yourself before building — product details move fast)

- Razorpay MCP Server (official, open source, MIT licensed): https://github.com/razorpay/razorpay-mcp-server
- Razorpay Agentic Payments product page (40+ APIs, MCP, UPI Reserve Pay, UPI Circle): https://razorpay.com/agentic-payments/
- Razorpay + NPCI agentic UPI payments on Claude, live with Zomato/Swiggy/Zepto: https://razorpay.com/blog/agentic-payments-and-npci/
- Razorpay becomes first Indian payment gateway to launch an MCP Server: https://newsroom.razorpay.in/newsroom/razorpay-becomes-indias-first-payment-gateway-to-launch-mcp-server-for-instant-ai-payment-integration/
- Razorpay Agent Studio official product page: https://razorpay.com/agent-studio/
- Medianama: "Razorpay Launches AI Agent Studio: Promise, Gaps, Risks" (dark patterns, price discrimination, regulatory gaps): https://www.medianama.com/2026/03/223-razorpay-launches-ai-agent-studio-questions-loom-dark-patterns-price-discrimination/
- Medianama: Razorpay's Chief Product Officer responding to pricing/compliance concerns: https://www.medianama.com/2026/03/223-razorpay-chief-product-officer-ai-agent-studio-pricing-compliance-concerns/

## 10. One open item before you commit

Worth a one-line email to the organizers before you invest build time: "Can my Track 1 submission be a companion catalog tool, a pricing-fairness checker, and a middleware layer built on top of your official open-source MCP Server, using test-mode credentials?" This protects you from an eligibility surprise and is itself a good signal — it shows deep research into their actual product stack, not just a feature demo.
