"""GET/POST /api/catalog -- exposes data/catalog.json so the storefront (BUILD_SPEC.md
section 9a) can render the real product grid. Adding a product is a logged-in-customer
action (marketplace-style listing via the same AI Shopping Agent chat), not an admin one --
any signed-in shopper can list a product, but only via the authenticated session.

add_product_to_catalog() is a plain function (not just the route below) so the LLM agent
(agent/llm_agent.py) can call it directly as a tool, not only via HTTP."""
import json
import os
import sys
import threading
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from audit.audit_log import log_event
from api.customer_auth import require_customer

router = APIRouter()

CATALOG_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "data", "catalog.json")
_lock = threading.Lock()

# The only categories the storefront's UI (CATEGORY_LABELS/CATEGORY_GRADIENTS in app.js) and
# category-filter chips actually know how to display -- anything else is silently invisible
# from every filter except "All". Literal[...] enforces this on the HTTP route (a bad value
# is rejected with a clean 422 before it ever reaches add_product_to_catalog); the same set is
# also checked inside add_product_to_catalog itself, since the LLM agent calls that function
# directly as a tool, bypassing this pydantic model entirely.
VALID_CATEGORIES = ("audio", "wearables", "accessories", "computer-accessories")


class NewProduct(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    category: Literal[VALID_CATEGORIES]
    price_inr: int = Field(gt=0)
    stock: int = Field(ge=0)
    rating: float = Field(ge=0, le=5)
    description: str = Field(default="", max_length=200)


def _load():
    with open(CATALOG_PATH, encoding="utf-8") as f:
        return json.load(f)


def _atomic_write_json(path: str, data) -> None:
    """Write-temp-then-rename, so a process kill mid-write can never leave a truncated,
    corrupt JSON file behind -- os.replace is atomic on both Windows and POSIX, so the old
    file stays fully intact right up until the new one is completely written."""
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_path, path)


def add_product_to_catalog(name: str, category: str, price_inr: int, stock: int, rating: float, description: str = "") -> dict:
    if category not in VALID_CATEGORIES:
        raise ValueError(f"Unknown category {category!r} -- must be one of: {', '.join(VALID_CATEGORIES)}")
    with _lock:
        products = _load()
        next_num = max((int(p["product_id"][1:]) for p in products), default=0) + 1
        product = {
            "product_id": f"p{next_num:03d}",
            "name": name,
            "category": category,
            "price_inr": price_inr,
            "stock": stock,
            "rating": rating,
            "description": description,
        }
        products.append(product)
        _atomic_write_json(CATALOG_PATH, products)
    log_event("catalog", "add_product", {"name": name, "category": category, "price_inr": price_inr}, product, "ok")
    return product


def catalog_overview() -> dict:
    """Category counts + top-rated picks, for general "what's available" style questions."""
    products = _load()
    counts = {}
    for p in products:
        counts[p["category"]] = counts.get(p["category"], 0) + 1
    top_rated = sorted(products, key=lambda p: p["rating"], reverse=True)[:5]
    return {
        "total_products": len(products),
        "category_counts": counts,
        "top_rated": [{"product_id": p["product_id"], "name": p["name"], "price_inr": p["price_inr"], "rating": p["rating"]} for p in top_rated],
    }


@router.get("/api/catalog")
def catalog():
    return _load()


@router.post("/api/catalog")
def add_product(body: NewProduct, _customer_id: str = Depends(require_customer)):
    return add_product_to_catalog(body.name, body.category, body.price_inr, body.stock, body.rating, body.description)
