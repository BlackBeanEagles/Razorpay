"""Guardrail: mandate enforcement, forwarding to Razorpay, and post-purchase verification."""
import json
import os
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from audit.audit_log import log_event
from guardrail import mandate as mandate_mod
from guardrail import razorpay_client
from guardrail import razorpay_rest

# execute_purchase() below always uses the deterministic mock client (razorpay_client.py) so
# the test suite and batch runs stay fast, reproducible, and always completable -- a real
# payment can only be captured by a human at actual Checkout (card entry/OTP), not by any
# headless backend call. initiate_purchase()/confirm_purchase() are the real, human-verified
# alternative: they create a genuine Razorpay test-mode order (razorpay_rest.py) and hand off
# to the real Checkout UI for a human to complete, then verify the real result afterward.
# Both paths share the same mandate enforcement and audit logging.

SEED_MANDATES_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "mandates.json")
MANDATE_STORE_PATH = os.path.join(os.path.dirname(__file__), "mandates.json")
LEDGER_PATH = os.path.join(os.path.dirname(__file__), "ledger.json")
PENDING_PURCHASES_PATH = os.path.join(os.path.dirname(__file__), "pending_purchases.json")
_LOCK_PATH = MANDATE_STORE_PATH + ".lock"
_LOCK_ACQUIRE_TIMEOUT_SECONDS = 5
_LOCK_STALE_SECONDS = 10  # a lock file older than this is assumed abandoned by a crashed
                          # process, not real contention -- otherwise one crash mid-update
                          # would permanently deadlock every future purchase.

# Snapshotted before anything can call use_isolated_store() -- the only way back to the real,
# production mandate/ledger files once some other test file's isolation has redirected the
# globals above. See use_isolated_store's docstring for why isolation exists; use_real_store()
# is its counterpart, needed because reconciliation's tests (and anything else that reads
# load_ledger() expecting the REAL merchant ledger) run in the same pytest process as guardrail/
# mcp_server's isolated tests -- without an explicit way back, whichever test file isolated
# last silently leaves every later file reading its leftover fixture data instead of production.
_REAL_MANDATE_STORE_PATH = MANDATE_STORE_PATH
_REAL_LEDGER_PATH = LEDGER_PATH


def use_isolated_store(mandate_path: str, ledger_path: str):
    """Redirects the mandate store, ledger, and their lock file to isolated paths -- for
    test/batch scripts ONLY, called once before any other guardrail function in that process.

    Without this, every test/batch script that calls reset_mandate_store() (nearly all of them,
    for deterministic starting spend) rebuilds MANDATE_STORE_PATH from the seed fixtures,
    silently discarding every real mandate that isn't in that seed -- including one just created
    by an actual logged-in customer or an external AI buyer over MCP mid-demo. That happened for
    real during this project's own development (an AI buyer's pending real-Checkout purchase was
    wiped by re-running the guardrail batch). Every module-level function reads MANDATE_STORE_PATH
    /LEDGER_PATH by name at call time, so reassigning these globals here redirects all of them;
    only _LOCK_PATH is a derived constant computed at import time, so it's reassigned explicitly.
    PENDING_PURCHASES_PATH is likewise redirected (derived from mandate_path's directory) -- the
    real one is what the payment webhook handler reads to independently confirm a payment, and a
    batch/test run's fake pending records have no business showing up there."""
    global MANDATE_STORE_PATH, LEDGER_PATH, _LOCK_PATH, PENDING_PURCHASES_PATH
    MANDATE_STORE_PATH = mandate_path
    LEDGER_PATH = ledger_path
    _LOCK_PATH = mandate_path + ".lock"
    PENDING_PURCHASES_PATH = os.path.join(os.path.dirname(mandate_path), "_isolated_pending_purchases.json")


def use_real_store():
    """Restores the real, production mandate/ledger paths -- the counterpart to
    use_isolated_store(). Any test file whose tests genuinely need to read the real ledger
    (reconciliation's, which reads real AI-buyer/human purchases via load_ledger()) must call
    this in its own setup_module(), the same way an isolated test file calls use_isolated_store()
    in its own -- otherwise it silently inherits whatever isolation an earlier-run test file in
    the same pytest process left the globals pointed at."""
    global MANDATE_STORE_PATH, LEDGER_PATH, _LOCK_PATH
    MANDATE_STORE_PATH = _REAL_MANDATE_STORE_PATH
    LEDGER_PATH = _REAL_LEDGER_PATH
    _LOCK_PATH = _REAL_MANDATE_STORE_PATH + ".lock"


@contextmanager
def _mandate_store_lock():
    """Cross-platform exclusive lock via atomic lockfile creation (os.O_CREAT | O_EXCL is
    atomic on both Windows and POSIX, unlike a plain existence check + create). Without this,
    two purchases racing against the SAME mandate_id (a real scenario here -- most test
    customers share "m_default") could both read the same starting spend, both pass their own
    check, and whichever writes last silently discards the other's spend update -- letting the
    mandate limit be exceeded despite every individual check having "passed." Everything that
    reads-then-writes mandate spend must hold this lock for the whole read-check-write, not
    just the final write, or the race just moves earlier."""
    deadline = time.time() + _LOCK_ACQUIRE_TIMEOUT_SECONDS
    while True:
        try:
            fd = os.open(_LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            break
        except (FileExistsError, PermissionError):
            # PermissionError, not just FileExistsError, is possible here on Windows: another
            # thread's os.remove() of this same lockfile can leave it in a transient
            # delete-pending state where a concurrent os.open(O_CREAT|O_EXCL) is denied access
            # rather than told the file exists. Treated identically -- someone else holds the
            # lock (or just released it), so retry -- since without this, that PermissionError
            # would propagate uncaught out of a purchase thread instead of just waiting its turn.
            try:
                if time.time() - os.path.getmtime(_LOCK_PATH) > _LOCK_STALE_SECONDS:
                    os.remove(_LOCK_PATH)
                    continue
            except OSError:
                pass  # another process cleaned it up between our check and remove -- fine
            if time.time() > deadline:
                raise TimeoutError(
                    "Timed out waiting for the mandate store lock -- another purchase may be stuck mid-update."
                )
            time.sleep(0.05)
    try:
        yield
    finally:
        try:
            os.remove(_LOCK_PATH)
        except OSError:
            pass


def reset_mandate_store():
    """(Re)builds the working, signed mandate store from the seed fixtures in data/mandates.json.

    Seed dates are stored as hour offsets from "now" (issued_offset_hours / expires_offset_hours),
    not fixed calendar dates -- computed fresh every time this runs. An earlier version used
    absolute ISO timestamps (e.g. "2026-08-26T00:00:00Z"), which meant every mandate silently
    aged into "expired" a day or two after being written, breaking purchases with no code change
    at fault. Offsets can't go stale: "expires in 24h" is still true whenever this is called.
    """
    with open(SEED_MANDATES_PATH, "r", encoding="utf-8") as f:
        seed = json.load(f)
    now = time.time()
    store = {}
    for m in seed:
        internal = {
            "mandate_id": m["mandate_id"],
            "merchant_id": m["merchant_id"],
            "max_amount_inr": m["max_amount_inr"],
            "issued_at": now + m["issued_offset_hours"] * 3600,
            "expires_at": now + m["expires_offset_hours"] * 3600,
            "single_use": m["single_use"],
            "amount_spent_so_far_inr": m["amount_spent_so_far_inr"],
            # Seed/demo mandates are deliberately shared -- usable by any authenticated
            # customer, not owned by one. See mandate.py's issue_mandate for the contrast.
            "owner_customer_id": None,
        }
        store[m["mandate_id"]] = mandate_mod.encode_mandate(internal)
    with open(MANDATE_STORE_PATH, "w", encoding="utf-8") as f:
        json.dump(store, f, indent=2)
    return store


def _load_mandate_store() -> dict:
    if not os.path.exists(MANDATE_STORE_PATH):
        return reset_mandate_store()
    with open(MANDATE_STORE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_mandate_store(store: dict):
    with open(MANDATE_STORE_PATH, "w", encoding="utf-8") as f:
        json.dump(store, f, indent=2)


def _owned_by(data: dict, requesting_customer_id: str) -> bool:
    """True if this mandate is usable by requesting_customer_id -- either it's a shared/demo
    mandate (owner_customer_id is None, e.g. the seed fixtures used across many different
    customers in the batch dataset) or requesting_customer_id genuinely owns it."""
    owner = data.get("owner_customer_id")
    return owner is None or owner == requesting_customer_id


def get_mandate_token(mandate_id: str, requesting_customer_id: str = None) -> str:
    """requesting_customer_id, when given, enforces ownership -- a customer's own mandate
    (created via "set spending limit") can only be looked up by that same customer. A mismatch
    is deliberately reported identically to "doesn't exist" (KeyError), not a distinct 403,
    so a wrong guess at someone else's mandate_id can't be used to confirm it's real."""
    store = _load_mandate_store()
    if mandate_id not in store:
        raise KeyError(f"Unknown mandate_id: {mandate_id}")
    token = store[mandate_id]
    if requesting_customer_id is not None:
        data = mandate_mod.decode_and_verify(token)
        if data is None or not _owned_by(data, requesting_customer_id):
            raise KeyError(f"Unknown mandate_id: {mandate_id}")
    return token


def issue_mandate(merchant_id: str, max_amount_inr: int, expires_in_seconds: int, single_use: bool,
                   owner_customer_id: str = None) -> str:
    return mandate_mod.issue_mandate(merchant_id, max_amount_inr, expires_in_seconds, single_use, owner_customer_id)


def issue_and_store_mandate(merchant_id: str, max_amount_inr: int, expires_in_seconds: int, single_use: bool,
                             owner_customer_id: str = None) -> dict:
    """Issues a new signed mandate and persists it to the working store so check_mandate/
    execute_purchase can look it up by mandate_id later. Used by POST /api/mandates --
    owner_customer_id ties it to the customer who created it (see _owned_by)."""
    token = issue_mandate(merchant_id, max_amount_inr, expires_in_seconds, single_use, owner_customer_id)
    data = mandate_mod.decode_and_verify(token)
    with _mandate_store_lock():
        # Same unlocked read-modify-write race as the spend updates this lock was originally
        # built for -- two concurrent POST /api/mandates calls could otherwise silently
        # clobber each other's just-issued mandate.
        store = _load_mandate_store()
        store[data["mandate_id"]] = token
        _save_mandate_store(store)
    return {"mandate_id": data["mandate_id"], "token": token, "state": _mandate_state_from_data(data)}


def _mandate_state_from_data(data: dict) -> dict:
    return {
        "mandate_id": data["mandate_id"],
        "merchant_id": data["merchant_id"],
        "max_amount_inr": data["max_amount_inr"],
        "amount_spent_so_far_inr": data["amount_spent_so_far_inr"],
        "issued_at": datetime.fromtimestamp(data["issued_at"], tz=timezone.utc).isoformat(),
        "expires_at": datetime.fromtimestamp(data["expires_at"], tz=timezone.utc).isoformat(),
        "single_use": data["single_use"],
        "is_expired": time.time() > data["expires_at"],
        "owner_customer_id": data.get("owner_customer_id"),
    }


def renew_mandate(mandate_id: str, new_expires_at: float) -> dict | None:
    """Resets a mandate's spend counter to 0 and moves its expiry to new_expires_at -- called
    when a real, bank-authorized recurring top-up (a Razorpay Subscription charge via UPI
    AutoPay/eMandate, see subscriptions/allowance_subscription.py) confirms the customer's
    allowance for a new billing period. max_amount_inr itself is untouched: a refresh doesn't
    grow the ceiling, it makes the existing ceiling available again for the new period, exactly
    like a real recurring allowance rather than an ever-growing balance. new_expires_at is the
    real subscription cycle's own current_end from Razorpay, not a guessed offset, so the
    mandate's window matches the bank-authorized billing cycle exactly. Returns the updated
    state, or None if the mandate doesn't exist."""
    with _mandate_store_lock():
        store = _load_mandate_store()
        token = store.get(mandate_id)
        if token is None:
            return None
        data = mandate_mod.decode_and_verify(token)
        if data is None:
            return None
        data["amount_spent_so_far_inr"] = 0
        data["issued_at"] = time.time()
        data["expires_at"] = new_expires_at
        new_token = mandate_mod.encode_mandate(data)
        store[mandate_id] = new_token
        _save_mandate_store(store)
    return _mandate_state_from_data(data)


def get_mandate_state(mandate_id: str, requesting_customer_id: str = None) -> dict | None:
    """Returns the mandate's current plain-field state (no signed token), or None if unknown
    OR (when requesting_customer_id is given) not owned by that customer -- both cases return
    the same None, not a distinguishable error, so a guessed mandate_id belonging to someone
    else can't be confirmed to exist."""
    store = _load_mandate_store()
    token = store.get(mandate_id)
    if token is None:
        return None
    data = mandate_mod.decode_and_verify(token)
    if data is None:
        return None
    if requesting_customer_id is not None and not _owned_by(data, requesting_customer_id):
        return None
    return _mandate_state_from_data(data)


def _validate_mandate_data(data: dict, requested_amount_inr: int) -> dict:
    """The actual mandate rules (expiry, single-use, cumulative limit), shared by check_mandate
    (a fast, advisory pre-check against whatever token the caller has on hand -- may be a few
    steps stale by the time it runs) and _atomic_reserve_spend (the authoritative, race-free
    check-and-commit run against freshly reloaded state under a lock). Keeping one copy of the
    rules means the two can never silently drift apart."""
    now = time.time()
    if now > data["expires_at"]:
        return {"allowed": False, "reason": f"Mandate {data['mandate_id']} expired at {data['expires_at']}, current time {now}."}
    if data["single_use"] and data["amount_spent_so_far_inr"] > 0:
        return {"allowed": False, "reason": f"Mandate {data['mandate_id']} is single-use and has already been spent."}
    new_total = data["amount_spent_so_far_inr"] + requested_amount_inr
    if new_total > data["max_amount_inr"]:
        return {
            "allowed": False,
            "reason": (
                f"Requested {requested_amount_inr} would bring cumulative spend to "
                f"{new_total}, exceeding mandate limit of {data['max_amount_inr']}."
            ),
        }
    return {"allowed": True, "reason": "Within mandate limits, not expired, single-use constraint satisfied."}


def check_mandate(signed_mandate_token: str, requested_amount_inr: int) -> dict:
    """Fast, advisory pre-check against the token the caller already has -- good enough to
    reject an obviously-blocked purchase early (expired, already over limit) without a wasted
    Razorpay round-trip, but NOT the authoritative gate for actually committing spend: the
    token may be a few steps stale by the time this runs. See _atomic_reserve_spend for the
    race-free check performed right before spend is actually applied."""
    data = mandate_mod.decode_and_verify(signed_mandate_token)
    if data is None:
        result = {"allowed": False, "reason": "Mandate signature is invalid or the token was tampered with."}
        log_event("guardrail", "mandate_check", {"requested_amount_inr": requested_amount_inr}, result, "blocked")
        return result

    result = _validate_mandate_data(data, requested_amount_inr)
    log_event(
        "guardrail", "mandate_check",
        {"mandate_id": data["mandate_id"], "requested_amount_inr": requested_amount_inr},
        result, "ok" if result["allowed"] else "blocked",
    )
    return result


def load_ledger() -> list:
    """Public read access to the real transaction ledger -- used by reconciliation.py to fold
    genuine AI-buyer (and human) purchases into the batch reconciliation report alongside the
    synthetic seed data, so "throughput plus measured accuracy" isn't only ever measured against
    fabricated records."""
    return _load_ledger()


def _load_ledger() -> list:
    if not os.path.exists(LEDGER_PATH):
        return []
    with open(LEDGER_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _load_pending_purchases() -> dict:
    if not os.path.exists(PENDING_PURCHASES_PATH):
        return {}
    with open(PENDING_PURCHASES_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_pending_purchases(data: dict) -> None:
    tmp_path = f"{PENDING_PURCHASES_PATH}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_path, PENDING_PURCHASES_PATH)


def _record_pending_purchase(razorpay_order_id: str, mandate_id: str, product_id: str, amount_inr: int,
                              requesting_customer_id: str) -> None:
    """Recorded the moment initiate_purchase() creates a real order -- everything the payment
    webhook handler (api/routes/webhooks.py) needs to independently confirm this payment later,
    even if the browser that opened Checkout never comes back to call /api/purchase/confirm
    itself (closed the tab right after paying, crashed, lost connectivity). Without this, a
    payment that genuinely succeeded could go permanently unrecorded in Guardrail's ledger."""
    with _mandate_store_lock():  # same lock as ledger/spend -- all guardrail state serialized through one lock
        data = _load_pending_purchases()
        data[razorpay_order_id] = {
            "mandate_id": mandate_id, "product_id": product_id, "amount_inr": amount_inr,
            "requesting_customer_id": requesting_customer_id,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        _save_pending_purchases(data)


def get_pending_purchase(razorpay_order_id: str) -> dict | None:
    """None if no such order was ever initiated through this Guardrail (or it's already been
    cleared after confirmation) -- the webhook handler treats that as nothing to reconcile."""
    return _load_pending_purchases().get(razorpay_order_id)


def _clear_pending_purchase(razorpay_order_id: str) -> None:
    with _mandate_store_lock():
        data = _load_pending_purchases()
        if razorpay_order_id in data:
            del data[razorpay_order_id]
            _save_pending_purchases(data)


def _ledger_entry_for_successful_order(razorpay_order_id: str) -> dict | None:
    """Finds a prior ledger entry that already recorded this exact order as successfully
    verified -- used to make confirm_purchase idempotent. Without this, confirming the same
    razorpay_order_id twice (a double form-submit, or a page reload after success) would apply
    spend against the mandate a second time for one real payment that only happened once."""
    for entry in _load_ledger():
        if entry.get("razorpay_order_id") == razorpay_order_id and entry.get("status") == "success":
            return entry
    return None


def _append_ledger(entry: dict):
    with _mandate_store_lock():  # same lock as spend updates -- ledger and spend must agree
        ledger = _load_ledger()
        ledger.append(entry)
        with open(LEDGER_PATH, "w", encoding="utf-8") as f:
            json.dump(ledger, f, indent=2)


def _reserve_spend_locked(mandate_id: str, requested_amount_inr: int) -> dict:
    """Core of the atomic spend-commit -- reloads the mandate fresh from disk, re-validates,
    and commits if allowed. Factored out so _atomic_confirm_and_reserve can compose it with an
    idempotency check under ONE lock acquisition (see that function for why two separately-
    locked steps aren't enough). Must only be called while already holding _mandate_store_lock()
    -- the lockfile mutex isn't reentrant, so acquiring it twice from the same call stack would
    deadlock, not just be redundant."""
    store = _load_mandate_store()
    token = store.get(mandate_id)
    if token is None:
        return {"allowed": False, "reason": f"Unknown mandate_id: {mandate_id}"}
    data = mandate_mod.decode_and_verify(token)
    if data is None:
        return {"allowed": False, "reason": "Mandate signature is invalid or the token was tampered with."}

    result = _validate_mandate_data(data, requested_amount_inr)
    if not result["allowed"]:
        return result

    data["amount_spent_so_far_inr"] += requested_amount_inr
    store[mandate_id] = mandate_mod.encode_mandate(data)
    _save_mandate_store(store)
    return result


def _atomic_reserve_spend(mandate_id: str, requested_amount_inr: int) -> dict:
    """The authoritative, race-free gate for committing spend against a mandate when there's no
    idempotency concern to compose it with (the mock execute_purchase flow -- each call creates
    a brand-new random order_id, so there's no "same order confirmed twice" scenario for it).
    Every code path that marks a purchase "success" must go through this (or
    _atomic_confirm_and_reserve) immediately before doing so, not the earlier advisory
    check_mandate call."""
    with _mandate_store_lock():
        return _reserve_spend_locked(mandate_id, requested_amount_inr)


def _atomic_confirm_and_reserve(razorpay_order_id: str, mandate_id: str, product_id: str,
                                 amount_inr: int, verification: dict, requesting_customer_id: str = None):
    """The race-free gate for confirm_purchase's success path specifically. Re-checks
    idempotency, reserves spend, AND (only when spend is actually reserved) writes the ledger
    entry -- all as ONE atomic unit under a single lock acquisition.

    This closes a real gap that checking idempotency and reserving spend as two SEPARATELY
    locked steps still left open: two truly concurrent confirms of the exact same
    razorpay_order_id could both pass an idempotency check before either had committed
    anything, then both independently reserve spend for one real payment -- because neither
    lock acquisition, on its own, ever saw the other's result. Folding the ledger write for the
    success case into the SAME critical section closes that window completely: whichever
    confirm reaches this function second always finds the first one's ledger entry already
    there, before it ever gets to touch the mandate's spend.

    Returns (cached_entry, None) if this exact order was already confirmed by an earlier call
    (nothing new to do -- caller should NOT write another ledger entry), or (None,
    reserve_result) otherwise, where reserve_result["allowed"] tells the caller whether spend
    was committed (and the ledger entry already written, atomically, right here) or not (the
    caller handles the refund path and writes its own ledger entry afterward, outside this
    lock -- safe, since no spend was ever reserved in that branch, so there's nothing to
    double-count)."""
    with _mandate_store_lock():
        already = _ledger_entry_for_successful_order(razorpay_order_id)
        if already is not None:
            return already, None

        reserve = _reserve_spend_locked(mandate_id, amount_inr)
        if reserve["allowed"]:
            ledger = _load_ledger()
            ledger.append({
                "mandate_id": mandate_id, "product_id": product_id, "razorpay_order_id": razorpay_order_id,
                "expected_amount_inr": amount_inr, "status": "success", "verification": verification,
                "requesting_customer_id": requesting_customer_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            with open(LEDGER_PATH, "w", encoding="utf-8") as f:
                json.dump(ledger, f, indent=2)
        return None, reserve


def execute_purchase(signed_mandate_token: str, product_id: str, amount_inr: int,
                      simulate_settlement_mismatch: bool = False, requesting_customer_id: str = None) -> dict:
    mandate_check = check_mandate(signed_mandate_token, amount_inr)
    if not mandate_check["allowed"]:
        result = {
            "status": "blocked",
            "razorpay_order_id": None,
            "verification": None,
            "reason": mandate_check["reason"],
        }
        log_event("guardrail", "purchase_forwarded",
                  {"product_id": product_id, "amount_inr": amount_inr, "requesting_customer_id": requesting_customer_id},
                  result, "blocked")
        return result

    data = mandate_mod.decode_and_verify(signed_mandate_token)
    mandate_id = data["mandate_id"]

    if requesting_customer_id is not None and not _owned_by(data, requesting_customer_id):
        # Defense-in-depth: the caller should already have failed to obtain this token via
        # get_mandate_token's own ownership check, but this token could in principle be a step
        # stale (held from earlier) -- never move money on a mandate that isn't the caller's.
        result = {
            "status": "blocked", "razorpay_order_id": None, "verification": None,
            "reason": "This mandate does not belong to the requesting customer.",
        }
        log_event("guardrail", "purchase_forwarded",
                  {"product_id": product_id, "amount_inr": amount_inr, "requesting_customer_id": requesting_customer_id},
                  result, "blocked")
        return result

    order = razorpay_client.create_and_capture_order(product_id, amount_inr)
    log_event("guardrail", "purchase_forwarded",
              {"product_id": product_id, "amount_inr": amount_inr, "mandate_id": mandate_id, "requesting_customer_id": requesting_customer_id},
              order, "ok")

    settlement = razorpay_client.get_settlement(order["razorpay_order_id"], simulate_mismatch=simulate_settlement_mismatch)

    charged = order["amount_charged_inr"]
    expected = amount_inr
    settled = settlement["settled_amount_inr"]

    verification = {
        "amount_charged_inr": charged,
        "amount_expected_inr": expected,
        "amount_settled_inr": settled,
        "all_match": charged == expected == settled,
    }

    if verification["all_match"]:
        # The earlier check_mandate call used whatever token the caller had on hand, which may
        # already be a step stale -- this is the real, race-free gate: reload fresh and commit
        # atomically. In the rare case a concurrent purchase against the same mandate landed
        # first, this can still say no even though check_mandate said yes -- that's the point.
        reserve = _atomic_reserve_spend(mandate_id, amount_inr)
        if not reserve["allowed"]:
            result = {
                "status": "blocked",
                "razorpay_order_id": order["razorpay_order_id"],
                "verification": verification,
                "reason": f"Payment matched, but the mandate no longer allows it: {reserve['reason']}",
            }
            log_event("guardrail", "verification",
                  {"razorpay_order_id": order["razorpay_order_id"], "requesting_customer_id": requesting_customer_id},
                  result, "blocked")
        else:
            result = {
                "status": "success",
                "razorpay_order_id": order["razorpay_order_id"],
                "verification": verification,
                "reason": "Charged, expected, and settled amounts all match.",
            }
            log_event("guardrail", "verification",
                      {"razorpay_order_id": order["razorpay_order_id"], "requesting_customer_id": requesting_customer_id},
                      result, "ok")
    else:
        result = {
            "status": "failed_verification",
            "razorpay_order_id": order["razorpay_order_id"],
            "verification": verification,
            "reason": (
                f"Mismatch: charged={charged}, expected={expected}, settled={settled}. "
                "Not marking as success; mandate spend not updated."
            ),
        }
        log_event("guardrail", "verification",
              {"razorpay_order_id": order["razorpay_order_id"], "requesting_customer_id": requesting_customer_id},
              result, "failed")

    _append_ledger({
        "mandate_id": mandate_id,
        "product_id": product_id,
        "razorpay_order_id": order["razorpay_order_id"],
        "expected_amount_inr": amount_inr,
        "status": result["status"],
        "verification": verification,
        "requesting_customer_id": requesting_customer_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })

    return result


def initiate_purchase(signed_mandate_token: str, product_id: str, amount_inr: int, requesting_customer_id: str = None,
                       razorpay_customer_id: str = None) -> dict:
    """Human-verified purchase, step 1: enforce the mandate (identical rules to
    execute_purchase), then -- if allowed -- create a REAL Razorpay test-mode order and hand
    back the details needed to open real Checkout. Does not verify or update mandate spend;
    that only happens once confirm_purchase() sees an actual captured payment.

    razorpay_customer_id: the caller's real Razorpay Customer id (see
    api.customer_auth.get_or_create_razorpay_customer_id), if any -- passed through to the
    frontend's Checkout config so Razorpay's own widget can offer to save a card/UPI method and
    recognize this same person on a later purchase. Guardrail never sees or stores the actual
    card data either way; this only tells Checkout who's paying, not how."""
    mandate_check = check_mandate(signed_mandate_token, amount_inr)
    if not mandate_check["allowed"]:
        result = {"status": "blocked", "checkout": None, "reason": mandate_check["reason"]}
        log_event("guardrail", "purchase_forwarded",
                  {"product_id": product_id, "amount_inr": amount_inr, "requesting_customer_id": requesting_customer_id},
                  result, "blocked")
        return result

    data = mandate_mod.decode_and_verify(signed_mandate_token)
    mandate_id = data["mandate_id"]

    if requesting_customer_id is not None and not _owned_by(data, requesting_customer_id):
        # Defense-in-depth, same reasoning as execute_purchase's equivalent check.
        result = {"status": "blocked", "checkout": None, "reason": "This mandate does not belong to the requesting customer."}
        log_event("guardrail", "purchase_forwarded",
                  {"product_id": product_id, "amount_inr": amount_inr, "requesting_customer_id": requesting_customer_id},
                  result, "blocked")
        return result

    order = razorpay_rest.create_order(amount_inr, receipt=f"guardrail_{product_id}_{int(time.time())}")
    # Recorded before returning to the caller -- if the payment webhook arrives before the
    # browser ever calls /api/purchase/confirm (or that call never arrives at all), this is
    # what lets the webhook handler independently confirm the same purchase with the same
    # mandate/product/customer context, not just a bare order_id.
    _record_pending_purchase(order["id"], mandate_id, product_id, amount_inr, requesting_customer_id)
    checkout = {
        "razorpay_order_id": order["id"],
        "razorpay_key_id": razorpay_rest.RAZORPAY_KEY_ID,
        "amount_inr": amount_inr,
        "product_id": product_id,
        "mandate_id": mandate_id,
    }
    if razorpay_customer_id:
        checkout["razorpay_customer_id"] = razorpay_customer_id
    result = {
        "status": "checkout_required",
        "checkout": checkout,
        "reason": "Mandate allows this purchase. Complete payment via Razorpay Checkout to proceed.",
    }
    log_event("guardrail", "purchase_forwarded",
              {"product_id": product_id, "amount_inr": amount_inr, "mandate_id": mandate_id,
               "razorpay_order_id": order["id"], "requesting_customer_id": requesting_customer_id},
              result, "ok")
    return result


def resolve_purchase(signed_mandate_token: str, product_id: str, amount_inr: int, requesting_customer_id: str = None,
                      razorpay_customer_id: str = None) -> dict:
    """Single entry point the agent orchestration (agent.py, llm_agent.py) calls for the "buy"
    step: uses the real, human-verified Checkout flow when real Razorpay credentials are
    configured (status "checkout_required" -- payment isn't done yet, a human needs to complete
    real Checkout, then confirm_purchase() finishes it), otherwise the fully-automated mock
    flow (status "success"/"blocked"/"failed_verification", resolved immediately). Same
    mandate enforcement either way."""
    if razorpay_rest.REAL_CHECKOUT_AVAILABLE:
        return initiate_purchase(signed_mandate_token, product_id, amount_inr, requesting_customer_id, razorpay_customer_id)
    return execute_purchase(signed_mandate_token, product_id, amount_inr, requesting_customer_id=requesting_customer_id)


def _refund_uncountable_payment(razorpay_order_id: str, amount_inr: float) -> dict:
    """Issues a real (test-mode) refund for a payment that was genuinely captured but can't be
    counted against its mandate -- see the "reserve not allowed" branch in confirm_purchase.
    Never raises: a refund failure is surfaced in the returned detail, not thrown, since the
    caller still needs to return a coherent, honest result either way."""
    try:
        payment_id = razorpay_rest.captured_payment_id(razorpay_order_id)
        if payment_id is None:
            return {
                "refunded": False, "razorpay_refund_id": None,
                "detail": "Could not locate the captured payment to refund automatically -- manual reconciliation needed.",
            }
        refund = razorpay_rest.refund_payment(payment_id, amount_inr)
        return {
            "refunded": True, "razorpay_refund_id": refund.get("id"),
            "detail": f"Refund issued automatically (refund id {refund.get('id')}).",
        }
    except Exception as e:  # noqa: BLE001 -- a refund failure must never crash the request; report it honestly instead
        return {
            "refunded": False, "razorpay_refund_id": None,
            "detail": f"Automatic refund attempt failed ({e}) -- manual reconciliation needed.",
        }


def confirm_purchase(mandate_id: str, product_id: str, amount_inr: int, razorpay_order_id: str,
                      requesting_customer_id: str = None) -> dict:
    """Human-verified purchase, step 2: called after the human completes real Checkout.
    Independently re-checks the mandate (spend could have moved since initiate_purchase) and
    asks Razorpay itself what actually got captured against this order -- never trusts the
    frontend's say-so that payment succeeded."""
    already = _ledger_entry_for_successful_order(razorpay_order_id)
    if already is not None:
        # This exact order was already confirmed and counted once -- a double form-submit or
        # a page reload after success must not apply spend against the mandate a second time
        # for one real payment that only happened once.
        result = {
            "status": "success", "razorpay_order_id": razorpay_order_id,
            "verification": already.get("verification"),
            "reason": "Already confirmed and verified previously -- returning the original result, not re-applying spend.",
        }
        log_event("guardrail", "verification",
                  {"razorpay_order_id": razorpay_order_id, "requesting_customer_id": requesting_customer_id},
                  result, "ok")
        _clear_pending_purchase(razorpay_order_id)
        return result

    try:
        token = get_mandate_token(mandate_id, requesting_customer_id)
    except KeyError:
        result = {"status": "blocked", "razorpay_order_id": razorpay_order_id, "verification": None,
                   "reason": f"Unknown mandate_id: {mandate_id}"}
        log_event("guardrail", "verification",
                  {"razorpay_order_id": razorpay_order_id, "requesting_customer_id": requesting_customer_id},
                  result, "blocked")
        return result
    mandate_check = check_mandate(token, amount_inr)
    if not mandate_check["allowed"]:
        result = {"status": "blocked", "razorpay_order_id": razorpay_order_id, "verification": None, "reason": mandate_check["reason"]}
        log_event("guardrail", "verification",
                  {"razorpay_order_id": razorpay_order_id, "requesting_customer_id": requesting_customer_id},
                  result, "blocked")
        return result

    try:
        settled = razorpay_rest.captured_amount_inr(razorpay_order_id)
    except Exception as e:  # noqa: BLE001 -- surfaced honestly, never silently marked success
        result = {"status": "failed_verification", "razorpay_order_id": razorpay_order_id, "verification": None,
                   "reason": f"Could not verify with Razorpay: {e}"}
        log_event("guardrail", "verification",
                  {"razorpay_order_id": razorpay_order_id, "requesting_customer_id": requesting_customer_id},
                  result, "failed")
        return result

    verification = {
        "amount_charged_inr": settled,
        "amount_expected_inr": amount_inr,
        "amount_settled_inr": settled,
        "all_match": settled == amount_inr,
    }

    if verification["all_match"]:
        # Real money has already been captured by Razorpay at this point. Idempotency check +
        # spend reservation + (on success) the ledger write all happen atomically together --
        # see _atomic_confirm_and_reserve's docstring for why the idempotency check and the
        # reserve can't be two separately-locked steps without reopening the same race.
        already, reserve = _atomic_confirm_and_reserve(razorpay_order_id, mandate_id, product_id, amount_inr, verification, requesting_customer_id)
        if already is not None:
            # A concurrent (or earlier) call already confirmed this exact order and wrote its
            # ledger entry -- return that result, don't re-apply spend or write a duplicate.
            result = {
                "status": "success", "razorpay_order_id": razorpay_order_id,
                "verification": already.get("verification"),
                "reason": "Already confirmed and verified previously -- returning the original result, not re-applying spend.",
            }
            log_event("guardrail", "verification",
                      {"razorpay_order_id": razorpay_order_id, "requesting_customer_id": requesting_customer_id},
                      result, "ok")
            _clear_pending_purchase(razorpay_order_id)
            return result

        if reserve["allowed"]:
            # Spend was already committed and the ledger entry already written, atomically,
            # inside _atomic_confirm_and_reserve -- nothing left to do but report it.
            result = {"status": "success", "razorpay_order_id": razorpay_order_id, "verification": verification,
                       "reason": "Real Razorpay payment verified -- captured amount matches expected."}
            log_event("guardrail", "verification",
                      {"razorpay_order_id": razorpay_order_id, "requesting_customer_id": requesting_customer_id},
                      result, "ok")
            _clear_pending_purchase(razorpay_order_id)
            return result

        # Reserve failed (e.g. a concurrent purchase against the same mandate landed first) --
        # the payment is real but can't be counted against this mandate. No spend was ever
        # reserved for it, so there's no double-counting risk in writing this ledger entry the
        # normal (separately-locked) way below. Money must not simply stay taken, so a real
        # refund is issued rather than just logging the discrepancy.
        refund_outcome = _refund_uncountable_payment(razorpay_order_id, amount_inr)
        result = {
            "status": "refunded" if refund_outcome["refunded"] else "blocked",
            "razorpay_order_id": razorpay_order_id,
            "verification": verification,
            "refund": refund_outcome,
            "reason": (
                f"Razorpay confirms {settled} was captured, but the mandate no longer allows it: "
                f"{reserve['reason']} {refund_outcome['detail']}"
            ),
        }
        log_event("guardrail", "verification",
                  {"razorpay_order_id": razorpay_order_id, "requesting_customer_id": requesting_customer_id}, result,
                   "ok" if refund_outcome["refunded"] else "blocked")
    else:
        result = {"status": "failed_verification", "razorpay_order_id": razorpay_order_id, "verification": verification,
                   "reason": f"Expected {amount_inr}, Razorpay shows {settled} captured. Not marking as success."}
        log_event("guardrail", "verification",
                  {"razorpay_order_id": razorpay_order_id, "requesting_customer_id": requesting_customer_id},
                  result, "failed")

    _append_ledger({
        "mandate_id": mandate_id, "product_id": product_id, "razorpay_order_id": razorpay_order_id,
        "expected_amount_inr": amount_inr, "status": result["status"], "verification": verification,
        "requesting_customer_id": requesting_customer_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    # Both branches landing here (refunded/blocked-after-capture, and a stable amount-mismatch
    # failed_verification) are final, non-transient determinations against a real, already-known
    # captured amount -- retrying confirm_purchase again would reach the exact same outcome, so
    # there's nothing to preserve the pending record for. (The earlier "could not verify with
    # Razorpay" exception branch deliberately does NOT clear it -- that one's worth retrying.)
    _clear_pending_purchase(razorpay_order_id)

    return result
