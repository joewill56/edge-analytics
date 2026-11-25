import difflib
import logging
import re
import threading
import time
import unicodedata
from datetime import datetime, timedelta, timezone

import numpy as np
from flask import Flask, jsonify, make_response, render_template, request, send_from_directory

import db
import gate
import markets
import model
import odds_client
import pipeline
import pricing
import track
from picks import recommend
from config import ADMIN_TOKEN, ADSTERRA_BANNER_HTML, BLEND_WEIGHT, LEAGUES, PLANS, UNLOCK_GRACE_HOURS
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
             "quota": gate.quota(token), "ad_html": "" if prem else ADSTERRA_BANNER_HTML}
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
    prem = gate.premium_info(request.headers.get("X-Premium"))
    return jsonify(premium=bool(prem), until=prem["expires"] if prem else None)


@app.get("/api/pricing")
def api_pricing():
    return jsonify(pricing.payload())


@app.post("/api/admin/grant")
def api_admin_grant():
    """Owner-only: create a premium code (use for manual/transfer payments until online payment is live)."""
    if not _is_admin():
        return jsonify(error="Unauthorized"), 401
    if not gate.SECRET_IS_SET:
        return jsonify(error="Set SECRET_KEY on the server first, or codes will stop working after a restart."), 400
    d = request.get_json(silent=True) or {}
    plan = PLANS.get(d.get("plan"))
    if not plan:
        return jsonify(error="Unknown plan"), 400
    code, exp = gate.make_premium((d.get("email") or "").strip().lower(), plan["days"], plan["tier"])
    return jsonify(code=code, expires=exp)


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
