"""Access control: signed tokens for free daily unlocks and premium (ad-free) passes.

Tokens are signed with SECRET_KEY, so the browser can store them but cannot forge or edit them.
If SECRET_KEY is not set, a random key is used: everything still works, but tokens stop
working after each restart (set SECRET_KEY in production!).

Levels of detail a match can be shown at:
  free  - main pick + probabilities + best odds (10 matches/day, chosen by the user)
  basic - + expected goals, EV and the safest extra markets (2-week plan)
  full  - everything (monthly plan)
"""
import logging
import secrets
import time
from datetime import datetime, timedelta, timezone

from itsdangerous import URLSafeSerializer

from config import FREE_DAILY_UNLOCKS, SECRET_KEY

log = logging.getLogger(__name__)
SECRET_IS_SET = bool(SECRET_KEY)
if not SECRET_IS_SET:
    log.warning("SECRET_KEY is not set: using a temporary key. Unlocks and premium codes reset on every restart.")
_secret = SECRET_KEY or secrets.token_hex(32)
_unlock = URLSafeSerializer(_secret, salt="edge-unlock2")
_premium = URLSafeSerializer(_secret, salt="edge-premium")


def _today() -> str:
    """Calendar day in Nigeria (UTC+1), the day the free allowance resets."""
    return (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%d")


def _load(ser, token):
    if not token:
        return None
    try:
        return ser.loads(token)
    except Exception:  # bad signature, garbage, wrong type...
        return None


def _bundle(token) -> dict:
    data, now, today = _load(_unlock, token), time.time(), _today()
    if not isinstance(data, dict):
        return {"u": {}, "day": today, "n": 0}
    unlocked = {e: x for e, x in (data.get("u") or {}).items() if isinstance(x, (int, float)) and x > now}
    used = int(data.get("n", 0)) if data.get("day") == today else 0
    return {"u": unlocked, "day": today, "n": used}


def unlocked_ids(token) -> dict:
    return _bundle(token)["u"]


def quota(token) -> dict:
    return {"used": _bundle(token)["n"], "limit": FREE_DAILY_UNLOCKS}


def add_unlock(token, event_id, expires_ts):
    """New bundle token, or None when today's free allowance is used up. Re-opening a match is free."""
    b = _bundle(token)
    if event_id not in b["u"]:
        if b["n"] >= FREE_DAILY_UNLOCKS:
            return None
        b["n"] += 1
    b["u"][event_id] = int(expires_ts)
    return _unlock.dumps(b)


def make_premium(email: str, days: int, tier: str = "full"):
    exp = int(time.time() + days * 86400)
    return _premium.dumps({"e": email, "x": exp, "t": tier}), exp


def premium_info(token):
    data = _load(_premium, token)
    if isinstance(data, dict) and data.get("x", 0) > time.time():
        tier = data.get("t") if data.get("t") in ("basic", "full") else "full"  # older codes count as full
        return {"email": data.get("e", ""), "expires": int(data["x"]), "tier": tier}
    return None


def redact(item: dict) -> dict:
    """What a locked user sees: fixture only, no odds, no predictions."""
    return {**{k: item[k] for k in ("id", "home", "away", "commence", "books")}, "locked": True}


def shape(item: dict, level: str) -> dict:
    """Strip an unlocked match down to what the user's level includes. Done on the server."""
    out = {**item, "locked": False, "level": level}
    m = out.get("model")
    if level == "free":
        out.pop("markets", None)
        if m:
            out["model"] = {k: v for k, v in m.items() if k != "ev"}
    elif level == "basic":
        mk = out.get("markets")
        if mk:
            out["markets"] = {"xg": mk["xg"], "total_xg": mk["total_xg"], "top": mk["top"]}
    return out
