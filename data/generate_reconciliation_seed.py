"""One-time generator for the reconciliation module's synthetic seed data
(data/internal_orders.json, data/settlement_records.json).

Kept as a real, readable script rather than hand-typed JSON so the ground truth is
transparent and reproducible (fixed random seed) -- the same reasoning the existing seed
files in this repo already follow (pricing_log.json's is_seeded_unfair, test_intents.json's
expected_guardrail_outcome): a reconciliation batch is only meaningful proof if the "correct"
answer for every record is known in advance and wasn't picked after the fact.

Ties into the real catalog (data/catalog.json) and real customer pool
(data/customer_profiles.json) rather than inventing unrelated fake products, so the demo
reconciles orders that actually correspond to real TechBazaar products/prices.

Distribution (53 orders, deliberately NOT all clean -- a batch that's 100% matches proves
nothing about exception handling):
  42 matched              -- settlement exists, one capture, amount matches exactly
   4 amount_mismatch      -- settlement captured, but for a different amount than expected
   3 missing_settlement   -- order exists, no settlement record at all
   2 duplicate_settlement -- order exists, TWO captured settlements against it
   2 status_exception     -- order exists, its only settlement is failed/refunded, not captured
Plus 3 orphan_settlement records -- settlements referencing an order_id with no matching
internal order at all (e.g. a stray/duplicate charge, or an order from a different system).

Run: python data/generate_reconciliation_seed.py
"""
import json
import os
import random

DATA_DIR = os.path.dirname(__file__)
random.seed(20260828)  # fixed -- reproducible seed data, not re-rolled on every run

with open(os.path.join(DATA_DIR, "catalog.json"), encoding="utf-8") as f:
    catalog = json.load(f)
with open(os.path.join(DATA_DIR, "customer_profiles.json"), encoding="utf-8") as f:
    customer_ids = [p["customer_id"] for p in json.load(f)]

orders = []
settlements = []
order_seq = 1
settlement_seq = 1


def _new_order_id():
    global order_seq
    oid = f"ord_{order_seq:04d}"
    order_seq += 1
    return oid


def _new_settlement_id():
    global settlement_seq
    sid = f"stl_{settlement_seq:04d}"
    settlement_seq += 1
    return sid


def _pick_product():
    return random.choice(catalog)


def _base_order(status_label):
    product = _pick_product()
    order_id = _new_order_id()
    orders.append({
        "order_id": order_id,
        "product_id": product["product_id"],
        "customer_id": random.choice(customer_ids),
        "expected_amount_inr": product["price_inr"],
        "order_date": f"2026-08-{random.randint(1, 27):02d}",
        "_expected_reconciliation_status": status_label,
    })
    return order_id, product["price_inr"]


# 42 clean matches
for _ in range(42):
    order_id, amount = _base_order("matched")
    settlements.append({
        "settlement_id": _new_settlement_id(), "order_id": order_id,
        "settled_amount_inr": amount, "status": "captured",
        "settlement_date": f"2026-08-{random.randint(1, 27):02d}",
    })

# 4 amount mismatches -- captured, but for the wrong amount
for _ in range(4):
    order_id, amount = _base_order("amount_mismatch")
    drift = random.choice([-50, -25, 15, 40])
    settlements.append({
        "settlement_id": _new_settlement_id(), "order_id": order_id,
        "settled_amount_inr": max(1, amount + drift), "status": "captured",
        "settlement_date": f"2026-08-{random.randint(1, 27):02d}",
    })

# 3 missing settlements -- order exists, nothing settled against it
for _ in range(3):
    _base_order("missing_settlement")

# 2 duplicate settlements -- two captured settlements for the same order
for _ in range(2):
    order_id, amount = _base_order("duplicate_settlement")
    for _dup in range(2):
        settlements.append({
            "settlement_id": _new_settlement_id(), "order_id": order_id,
            "settled_amount_inr": amount, "status": "captured",
            "settlement_date": f"2026-08-{random.randint(1, 27):02d}",
        })

# 2 status exceptions -- the only settlement is failed/refunded, not captured
for _ in range(2):
    order_id, amount = _base_order("status_exception")
    settlements.append({
        "settlement_id": _new_settlement_id(), "order_id": order_id,
        "settled_amount_inr": amount, "status": random.choice(["failed", "refunded"]),
        "settlement_date": f"2026-08-{random.randint(1, 27):02d}",
    })

# 3 orphan settlements -- no corresponding internal order at all
for _ in range(3):
    product = _pick_product()
    settlements.append({
        "settlement_id": _new_settlement_id(), "order_id": f"ord_ghost_{random.randint(1000,9999)}",
        "settled_amount_inr": product["price_inr"], "status": "captured",
        "settlement_date": f"2026-08-{random.randint(1, 27):02d}",
    })

random.shuffle(orders)
random.shuffle(settlements)

with open(os.path.join(DATA_DIR, "internal_orders.json"), "w", encoding="utf-8") as f:
    json.dump(orders, f, indent=2)
with open(os.path.join(DATA_DIR, "settlement_records.json"), "w", encoding="utf-8") as f:
    json.dump(settlements, f, indent=2)

print(f"Generated {len(orders)} orders and {len(settlements)} settlement records.")
