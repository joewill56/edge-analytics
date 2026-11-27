import difflib
import logging
import html as htmllib
import re
import secrets
import threading
import time
import unicodedata
from datetime import datetime, timedelta, timezone

import numpy as np
from flask import Flask, jsonify, make_response, render_template, request, send_from_directory

import db
import gate
import mailer
import markets
import model
import odds_client
import payments
import pipeline
import pricing
import subs
import track
from picks import recommend
from config import ADMIN_TOKEN, ADSTERRA_SNIPPETS, BLEND_WEIGHT, LEAGUES, PLANS, PUBLIC_URL, UNLOCK_GRACE_HOURS
from features import implied_probs

logging.basicConfig(level=logging.INFO)
app = Flask(__name__)
db.init_db()

JOB = {"running": False, "message": "idle"}


def _league(sport_key):
    if sport_key not in LEAGUES:
        return None
    return LEAGUES[sport_key]


def build_predictions(sport_key: str, force=False):
    code = LEAGUES[sport_key]["code"]
    events, meta = odds_client.get_odds(sport_key, force=force)
    teams = model.known_teams(code)
    out, to_save = [], []
    for ev in sorted(events, key=lambda e: e["commence"]):
        odds = np.array([b["odds"] for b in ev["books"]], dtype=float)
        market = np.mean([implied_probs(*o) for o in odds], axis=0)  # de-vigged consensus
        best_idx = odds.argmax(axis=0)
        best = [{"odds": float(odds[best_idx[i], i]), "book": ev["books"][best_idx[i]]["book"]} for i in range(3)]

        h, a = odds_client.resolve_team(ev["home"], teams), odds_client.resolve_team(ev["away"], teams)
        pred = model.predict(code, h, a)
        item = {
            "id": ev["id"], "home": ev["home"], "away": ev["away"], "commence": ev["commence"],
            "books": len(ev["books"]), "market": [float(p) for p in market], "best": best, "model": None,
        }
        if pred:
            raw = pred["probs"]
            # Blend model with bookmaker consensus; EV and picks use the blend
            p = [BLEND_WEIGHT * raw[i] + (1 - BLEND_WEIGHT) * float(market[i]) for i in range(3)]
            item["model"] = {
                **pred, "blend": p,
                "ev": [round(p[i] * best[i]["odds"] - 1, 4) for i in range(3)],  # expected value per unit staked
            }
            g = item["model"].pop("goal", None)
            if g:  # every goal market from one scoreline distribution anchored to the blended 1X2
                item["markets"] = markets.build(g["lh"], g["la"], g["rho"], p)
            item["pick"] = recommend(p, pred["low_data"])
            item["pick"]["market_prob"] = round(float(sum(market[i] for i in {"1": [0], "X": [1], "2": [2], "1X": [0, 1], "X2": [1, 2], "12": [0, 2]}.get(item["pick"]["code"], []))), 4) if item["pick"]["code"] != "SKIP" else None
        out.append(item)
        to_save.append((item, sport_key, code, h, a))
    try:
        track.record_picks(to_save)
    except Exception:
        app.logger.exception("could not record picks")  # never break the page over tracking
    return out, meta


@app.get("/")
def index():
    return render_template("index.html", leagues=LEAGUES)


@app.get("/sw.js")
def service_worker():  # served from the root so it controls the whole site
    r = make_response(send_from_directory(app.static_folder, "sw.js", mimetype="application/javascript"))
    r.headers["Cache-Control"] = "no-cache"
    return r


@app.get("/manifest.webmanifest")
def manifest():
    return send_from_directory(app.static_folder, "manifest.webmanifest", mimetype="application/manifest+json")


@app.get("/api/predictions")
def api_predictions():
    sport = request.args.get("league", "soccer_epl")
    if not _league(sport):
        return jsonify(error="Unknown league"), 400
    code = LEAGUES[sport]["code"]
    try:
        events, meta = build_predictions(sport, force=request.args.get("force") == "1")
    except odds_client.OddsError as exc:
        return jsonify(error=str(exc), trained=model.is_trained(code)), 502
    except Exception as exc:
        app.logger.exception("prediction failure")
        return jsonify(error=f"Unexpected error: {exc}"), 500
    shown, extra = _gate_events(events)
    if not _is_admin():
        meta = {}  # API credit counts are for the owner only
    return jsonify(events=shown, meta=meta, trained=model.is_trained(code), metrics=model.metrics(code), **extra)


@app.get("/api/track")
def api_track():
    try:
        track.settle_async()  # checks real results in the background (at most hourly per league)
        return jsonify(track.summary())
    except Exception as exc:
        app.logger.exception("track failure")
        return jsonify(error=f"Track record unavailable: {exc}"), 500


def _ad_dims(html):
    w = re.search(r"['\"]width['\"]\s*:\s*(\d+)", html)
    h = re.search(r"['\"]height['\"]\s*:\s*(\d+)", html)
    return {"w": int(w.group(1)) if w else 0, "h": int(h.group(1)) if h else 300}  # w=0 -> full width (native banner)


ADS_META = [_ad_dims(s) for s in ADSTERRA_SNIPPETS]


@app.get("/ad-frame")
def ad_frame():
    """One ad spot as its own page, shown inside a sandboxed iframe. Having a real address (instead of inline
    content) lets the ad network see which site it is running on; the sandbox stops ad scripts from touching
    the main page, its storage, or redirecting it."""
    if not ADSTERRA_SNIPPETS:
        return ("", 204)
    try:
        snippet = ADSTERRA_SNIPPETS[int(request.args.get("i", 0)) % len(ADSTERRA_SNIPPETS)]
    except ValueError:
        snippet = ADSTERRA_SNIPPETS[0]
    r = make_response('<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                      '<style>html,body{margin:0;padding:0;background:transparent}</style></head><body>' + snippet + '</body></html>')
    r.headers["Content-Security-Policy"] = "sandbox allow-scripts allow-popups allow-popups-to-escape-sandbox"
    r.headers["Cache-Control"] = "no-store"
    r.headers["X-Robots-Tag"] = "noindex"
    return r


def _gate_events(events):
    """Apply the user's access level to a list of matches. Returns (events to send, extra response fields)."""
    prem = gate.premium_info(request.headers.get("X-Premium"))
    admin = _is_admin()
    if admin:  # the owner never needs a subscription: full access, no ads
        prem = {"email": "admin", "expires": int(time.time()) + 365 * 86400, "tier": "full"}
    token = request.headers.get("X-Access")
    unlocked = gate.unlocked_ids(token)
    shown = []
    for e in events:
        if prem:
            shown.append(gate.shape(e, prem["tier"]))
        elif e["id"] in unlocked:
            shown.append(gate.shape(e, "free"))
        else:
            shown.append(gate.redact(e))
    extra = {"admin": admin, "premium": bool(prem), "premium_until": prem["expires"] if prem else None,
             "quota": gate.quota(token),
             # ad spots (size only; the ad code itself is served from /ad-frame). Owner can preview with ?adtest=1
             "ads": ADS_META if (not prem or (admin and request.args.get("adtest") == "1")) else []}
    return shown, extra


def _is_admin():
    return bool(ADMIN_TOKEN) and request.headers.get("X-Admin-Token") == ADMIN_TOKEN


@app.post("/api/unlock")
def api_unlock():
    """Free users open up to FREE_DAILY_UNLOCKS matches a day: issue a signed unlock for one match."""
    d = request.get_json(silent=True) or {}
    sport, event_id = d.get("league"), d.get("event_id")
    if sport not in LEAGUES or not event_id:
        return jsonify(error="Bad request"), 400
    try:
        events, _ = odds_client.get_odds(sport)
    except odds_client.OddsError as exc:
        return jsonify(error=str(exc)), 502
    ev = next((e for e in events if e["id"] == event_id), None)
    if not ev:
        return jsonify(error="Match not found"), 404
    kickoff = datetime.fromisoformat(ev["commence"].replace("Z", "+00:00"))
    if kickoff + timedelta(hours=UNLOCK_GRACE_HOURS) < datetime.now(timezone.utc):
        return jsonify(error="Match already finished"), 410
    bundle = request.headers.get("X-Access")
    token = gate.add_unlock(bundle, event_id, (kickoff + timedelta(hours=UNLOCK_GRACE_HOURS)).timestamp())
    if token is None:
        return jsonify(error="Daily free limit reached", quota=gate.quota(bundle)), 429
    return jsonify(access=token, quota=gate.quota(token))


def _norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9 ]+", " ", s).strip()


@app.get("/api/search")
def api_search():
    """Search matches by team name. Only looks inside leagues already loaded in the server cache, so it
    never spends Odds API credits. Results respect the same locking rules as the match list."""
    q = _norm(request.args.get("q", ""))
    tokens = [t for t in q.split() if t not in ("v", "vs")]
    if len(q) < 2 or not tokens:
        return jsonify(events=[], message="short", searched=0)
    pool, searched = [], 0
    for sport, lg in LEAGUES.items():
        if odds_client.cached_events(sport) is None:
            continue
        try:
            events, _ = build_predictions(sport)
        except Exception:
            continue
        searched += 1
        pool += [(sport, lg["name"], e) for e in events]

    def hits(fuzzy):
        out = []
        for sport, name, e in pool:
            hay = _norm(f"{e['home']} {e['away']} {name}")
            words = hay.split()
            if all((t in hay) if not fuzzy else bool(difflib.get_close_matches(t, words, n=1, cutoff=0.8)) for t in tokens):
                out.append((sport, name, e))
        return out

    found = sorted(hits(False) or hits(True), key=lambda x: x[2]["commence"])[:30]
    shown, extra = _gate_events([e for _, _, e in found])
    for item, (sport, name, _) in zip(shown, found):
        item["league"], item["league_key"] = name, sport
    return jsonify(events=shown, message=None if shown else "not_found", searched=searched, **extra)


@app.get("/api/me")
def api_me():
    if _is_admin():
        return jsonify(premium=True, admin=True, until=None)
    prem = gate.premium_info(request.headers.get("X-Premium"))
    return jsonify(premium=bool(prem), until=prem["expires"] if prem else None)


@app.get("/api/pricing")
def api_pricing():
    return jsonify({**pricing.payload(), "pay_enabled": payments.enabled(), "mail_enabled": mailer.enabled()})


@app.post("/api/admin/grant")
def api_admin_grant():
    """Owner-only: give someone premium without Flutterwave (e.g. a bank-transfer sale). Creates a real
    subscription with its own code; granting again to the same email extends it."""
    if not _is_admin():
        return jsonify(error="Unauthorized"), 401
    if track.storage_kind() == "temporary":
        return jsonify(error="Set DATABASE_URL first: without a persistent database the code would be lost on restart."), 400
    d = request.get_json(silent=True) or {}
    email = (d.get("email") or "").strip().lower()
    if d.get("plan") not in PLANS or not subs.valid_email(email):
        return jsonify(error="A valid email and plan are required"), 400
    sub = subs.create_subscription(email, d["plan"], "manual")
    return jsonify(code=sub["code"], expires=subs._ts(sub["expires_at"]))


# ---------------- online payments (Flutterwave) ----------------
_hits: dict = {}


def _limited(key, limit, window):
    """Tiny in-memory rate limiter (one worker process): True when `key` exceeded `limit` hits in `window` seconds."""
    now = time.time()
    recent = [t for t in _hits.get(key, []) if now - t < window]
    recent.append(now)
    _hits[key] = recent
    return len(recent) > limit


def _ip():
    return (request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or request.remote_addr or "?")


def _fmt_date(iso):
    return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").strftime("%d %b %Y")


def _email_code(sub, renewed=False):
    plan = PLANS.get(sub["plan"], {})
    body = (f"<p>Thank you for subscribing to Edge Analytics.</p>"
            f"<p>Your premium code:</p><p style='font-size:22px;font-weight:bold;letter-spacing:1px'>{htmllib.escape(sub['code'])}</p>"
            f"<p>Valid until <b>{_fmt_date(sub['expires_at'])}</b>.</p>"
            f"<p>To use it: open the site, tap <b>Go ad-free</b>, paste the code under &quot;Have a premium code?&quot; and tap Apply.</p>"
            f"<p>Keep this code private. If it is ever shared or lost, contact us and we will issue a new one.</p>")
    return mailer.send(sub["email"], "Your Edge Analytics premium code", body)


def _paid_page(state, status=200, **kw):
    r = make_response(render_template("paid.html", state=state, **kw), status)
    r.headers["Cache-Control"] = "no-store"
    return r


@app.post("/api/pay/start")
def api_pay_start():
    if not payments.enabled():
        return jsonify(error="Online payment is not available yet."), 503
    if _limited("pay:" + _ip(), 10, 3600):
        return jsonify(error="Too many attempts. Please try again later."), 429
    d = request.get_json(silent=True) or {}
    email, plan_key = (d.get("email") or "").strip().lower(), d.get("plan")
    plan = PLANS.get(plan_key)
    if not plan or not subs.valid_email(email):
        return jsonify(error="Enter a valid email and choose a plan."), 400
    tx_ref = "edge_" + secrets.token_urlsafe(16)
    try:
        subs.create_payment(tx_ref, email, plan_key, plan["ngn"], "NGN")
        link = payments.init_checkout(tx_ref, plan["ngn"], "NGN", email, PUBLIC_URL + "/pay/return",
                                      "Edge Analytics", f"Premium: {plan_key.replace('_', ' ')}")
    except payments.PayError as exc:
        return jsonify(error=str(exc)), 502
    except Exception:
        app.logger.exception("pay start failed")
        return jsonify(error="Could not start the payment."), 500
    return jsonify(link=link)


@app.get("/pay/return")
def pay_return():
    """Where Flutterwave sends the customer back. The URL is NOT trusted: we verify with Flutterwave ourselves."""
    tx_ref, status, tid = request.args.get("tx_ref", ""), request.args.get("status", "").lower(), request.args.get("transaction_id")
    pay = subs.get_payment(tx_ref)
    if not pay:
        return _paid_page("unknown", 404)
    if status not in ("successful", "completed"):
        subs.mark_failed(tx_ref)
        return _paid_page("failed")
    try:
        data = payments.verify(tid, tx_ref)
    except payments.PayError:
        return _paid_page("pending", 202, tx_ref=tx_ref)  # the webhook may still confirm it
    if not payments.check_ok(data, pay):
        return _paid_page("failed")
    sub, created = subs.fulfill(tx_ref, data.get("id"))
    if created:
        _email_code(sub)
    return _paid_page("ok", code=sub["code"], expires=_fmt_date(sub["expires_at"]), tx_ref=tx_ref, emailed=mailer.enabled())


@app.post("/api/pay/webhook")
def pay_webhook():
    """Flutterwave -> us. Protects against closed browsers; verified again with the API before any access is given."""
    if not payments.webhook_ok(request.headers.get("verif-hash")):
        return jsonify(error="bad signature"), 401
    data = (request.get_json(silent=True) or {}).get("data") or {}
    tx_ref = data.get("tx_ref")
    pay = subs.get_payment(tx_ref) if tx_ref else None
    if not pay or str(data.get("status", "")).lower() != "successful":
        return jsonify(ok=True)  # nothing for us to do
    try:
        verified = payments.verify(data.get("id"), tx_ref)
    except payments.PayError:
        return jsonify(error="verification unavailable"), 502  # Flutterwave retries later
    if payments.check_ok(verified, pay):
        sub, created = subs.fulfill(tx_ref, verified.get("id"))
        if created:
            _email_code(sub)
    return jsonify(ok=True)


@app.post("/api/restore")
def api_restore():
    """Lost your code? Either email + payment reference (shows it now) or email only (we email it to you)."""
    if _limited("restore:" + _ip(), 8, 3600):
        return jsonify(error="Too many attempts. Please try again later."), 429
    d = request.get_json(silent=True) or {}
    email, tx_ref = (d.get("email") or "").strip().lower(), (d.get("tx_ref") or "").strip()
    if not subs.valid_email(email):
        return jsonify(error="Enter a valid email."), 400
    if tx_ref:
        pay = subs.get_payment(tx_ref)
        sub = subs.get_sub(pay["sub_id"]) if pay and pay["email"] == email and pay["status"] == "successful" and pay["sub_id"] else None
        if sub and sub["status"] == "active" and sub["expires_at"] > subs._iso(subs._now()):
            return jsonify(code=sub["code"], expires=subs._ts(sub["expires_at"]))
        return jsonify(error="No active plan found for that email and reference."), 404
    sub = subs.active_for_email(email)
    if sub and mailer.enabled():
        _email_code(sub)
    return jsonify(ok=True, emailed=mailer.enabled())  # same answer whether or not the email has a plan


@app.get("/api/admin/subs")
def api_admin_subs():
    if not _is_admin():
        return jsonify(error="Unauthorized"), 401
    email = (request.args.get("email") or "").strip().lower()
    rows = subs.list_for_email(email) if email else []
    return jsonify(subs=[{k: r[k] for k in ("id", "email", "plan", "tier", "code", "expires_at", "status", "source")} for r in rows])


@app.post("/api/admin/subs/<sub_id>/<action>")
def api_admin_sub_action(sub_id, action):
    """Owner-only: rotate (new code, old one stops working), revoke, or resend the code email."""
    if not _is_admin():
        return jsonify(error="Unauthorized"), 401
    sub = subs.get_sub(sub_id)
    if not sub:
        return jsonify(error="Not found"), 404
    if action == "rotate":
        subs.rotate(sub_id)
        sub = subs.get_sub(sub_id)
        return jsonify(code=sub["code"], emailed=_email_code(sub) if mailer.enabled() else False)
    if action == "revoke":
        subs.revoke(sub_id)
        return jsonify(ok=True)
    if action == "resend":
        return jsonify(emailed=_email_code(sub) if mailer.enabled() else False)
    return jsonify(error="Unknown action"), 400


@app.get("/admin")
def admin_page():
    return render_template("admin.html", plans=PLANS)


@app.get("/api/ratings")
def api_ratings():
    sport = request.args.get("league", "soccer_epl")
    if not _league(sport):
        return jsonify(error="Unknown league"), 400
    return jsonify(ratings=model.ratings(LEAGUES[sport]["code"]))


def _run_refresh(codes):
    JOB.update(running=True, message="Downloading historical data…")
    try:
        for code in codes:
            JOB["message"] = f"Ingesting {code}…"
            pipeline.ingest_league(code)
            JOB["message"] = f"Training {code}…"
            model.train_league(code)
        JOB["message"] = "Done"
    except Exception as exc:
        app.logger.exception("refresh failed")
        JOB["message"] = f"Failed: {exc}"
    finally:
        JOB["running"] = False


@app.post("/api/refresh")
def api_refresh():
    if ADMIN_TOKEN and request.headers.get("X-Admin-Token") != ADMIN_TOKEN:
        return jsonify(error="Unauthorized"), 401
    if JOB["running"]:
        return jsonify(error="A refresh is already running"), 409
    sport = request.args.get("league")
    codes = [LEAGUES[sport]["code"]] if sport in LEAGUES else [m["code"] for m in LEAGUES.values()]
    threading.Thread(target=_run_refresh, args=(codes,), daemon=True).start()
    return jsonify(started=True)


@app.get("/api/status")
def api_status():
    return jsonify(JOB)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
