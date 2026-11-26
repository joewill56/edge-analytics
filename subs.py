"""Premium subscriptions and payment records, kept in the persistent database (Postgres when DATABASE_URL is set).

* Every customer gets their own random code (EDGE-XXXX-XXXX-XXXX), stored server-side.
* A code can be rotated (old one stops working) or revoked at any time.
* Buying again with the same email extends the same subscription (same code).
* Payments are recorded by reference, so one payment can only ever create/extend one subscription.
"""
import logging
import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone

import track
from config import PLANS

log = logging.getLogger(__name__)
CODE_RE = re.compile(r"^EDGE-[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}$")
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I to avoid typing mistakes
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,}$")

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS subscriptions (
        id TEXT PRIMARY KEY, email TEXT NOT NULL, plan TEXT NOT NULL, tier TEXT NOT NULL,
        code TEXT NOT NULL UNIQUE, expires_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
        source TEXT NOT NULL, tx_ref TEXT UNIQUE, created_at TEXT NOT NULL, code_rotated_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS payments (
        tx_ref TEXT PRIMARY KEY, email TEXT NOT NULL, plan TEXT NOT NULL, amount REAL NOT NULL,
        currency TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', flw_id TEXT, sub_id TEXT,
        created_at TEXT NOT NULL, verified_at TEXT)""",
]
_lock = threading.RLock()
_ready = False
_cache: dict = {}  # code -> (expires_at_monotonic, info or None)
CACHE_TTL, MISS_TTL = 60, 15


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _ensure():
    global _ready
    if not _ready:
        for stmt in SCHEMA:
            track._run(stmt)
        _ready = True


def _q(sql, params=(), fetch=True):
    _ensure()
    return track._run(sql, params, fetch=fetch)


def new_code() -> str:
    return "EDGE-" + "-".join("".join(secrets.choice(_ALPHABET) for _ in range(4)) for _ in range(3))


def valid_email(e: str) -> bool:
    return bool(e) and bool(EMAIL_RE.match(e))


def _ts(iso: str) -> int:
    return int(datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp())


# ---------------- subscriptions ----------------
def get_sub(sub_id):
    r = _q("SELECT * FROM subscriptions WHERE id=?", (sub_id,))
    return r[0] if r else None


def active_for_email(email):
    r = _q("SELECT * FROM subscriptions WHERE email=? AND status='active' AND expires_at>? ORDER BY expires_at DESC LIMIT 1",
           (email, _iso(_now())))
    return r[0] if r else None


def list_for_email(email):
    return _q("SELECT * FROM subscriptions WHERE email=? ORDER BY created_at DESC LIMIT 20", (email,))


def create_subscription(email: str, plan_key: str, source: str, tx_ref=None):
    """New subscription, or an extension of the customer's current one. Returns the subscription row."""
    plan = PLANS[plan_key]
    email = email.strip().lower()
    with _lock:
        cur = active_for_email(email)
        if cur:  # renewal: add the new period after the current expiry, keep the same code
            expires = datetime.strptime(cur["expires_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) + timedelta(days=plan["days"])
            tier = "full" if "full" in (cur["tier"], plan["tier"]) else "basic"
            _q("UPDATE subscriptions SET expires_at=?, tier=?, plan=? WHERE id=?", (_iso(expires), tier, plan_key, cur["id"]), fetch=False)
            _cache.clear()
            return get_sub(cur["id"])
        sub_id = "sub_" + secrets.token_urlsafe(9)
        _q("INSERT INTO subscriptions (id,email,plan,tier,code,expires_at,status,source,tx_ref,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
           (sub_id, email, plan_key, plan["tier"], new_code(), _iso(_now() + timedelta(days=plan["days"])), "active", source, tx_ref, _iso(_now())),
           fetch=False)
        _cache.clear()
        return get_sub(sub_id)


def lookup(code: str):
    """Access info for a premium code, or None. Cached for a minute so the database is not hit on every request."""
    if not code or not CODE_RE.match(code):
        return None
    hit = _cache.get(code)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    info = None
    try:
        r = _q("SELECT * FROM subscriptions WHERE code=?", (code,))
        if r and r[0]["status"] == "active" and r[0]["expires_at"] > _iso(_now()):
            info = {"email": r[0]["email"], "expires": _ts(r[0]["expires_at"]), "tier": r[0]["tier"], "sid": r[0]["id"]}
    except Exception:
        log.exception("subscription lookup failed")  # fail closed: treat as not premium
        return None
    _cache[code] = (time.monotonic() + (CACHE_TTL if info else MISS_TTL), info)
    return info


def rotate(sub_id):
    code = new_code()
    _q("UPDATE subscriptions SET code=?, code_rotated_at=? WHERE id=?", (code, _iso(_now()), sub_id), fetch=False)
    _cache.clear()
    return code


def revoke(sub_id):
    _q("UPDATE subscriptions SET status='revoked' WHERE id=?", (sub_id,), fetch=False)
    _cache.clear()


# ---------------- payments ----------------
def create_payment(tx_ref, email, plan_key, amount, currency):
    _q("INSERT INTO payments (tx_ref,email,plan,amount,currency,status,created_at) VALUES (?,?,?,?,?,?,?)",
       (tx_ref, email.strip().lower(), plan_key, float(amount), currency, "pending", _iso(_now())), fetch=False)


def get_payment(tx_ref):
    if not tx_ref or len(tx_ref) > 80:
        return None
    r = _q("SELECT * FROM payments WHERE tx_ref=?", (tx_ref,))
    return r[0] if r else None


def mark_failed(tx_ref):
    _q("UPDATE payments SET status='failed' WHERE tx_ref=? AND status='pending'", (tx_ref,), fetch=False)


def fulfill(tx_ref, flw_id):
    """Idempotent: a verified payment creates/extends a subscription exactly once, even if the webhook and
    the browser redirect arrive at the same moment. Returns (subscription row, True if this call did it)."""
    with _lock:
        pay = get_payment(tx_ref)
        if not pay:
            raise KeyError("unknown payment reference")
        if pay["status"] == "successful" and pay["sub_id"]:
            return get_sub(pay["sub_id"]), False
        sub = create_subscription(pay["email"], pay["plan"], "flutterwave", tx_ref)
        _q("UPDATE payments SET status='successful', flw_id=?, sub_id=?, verified_at=? WHERE tx_ref=?",
           (str(flw_id or ""), sub["id"], _iso(_now()), tx_ref), fetch=False)
        return sub, True
