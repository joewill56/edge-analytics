import logging
import threading

import numpy as np
from flask import Flask, jsonify, render_template, request

import db
import model
import odds_client
import pipeline
from picks import recommend
from config import ADMIN_TOKEN, BLEND_WEIGHT, LEAGUES
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
    out = []
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
            item["pick"] = recommend(p, pred["low_data"])
            item["pick"]["market_prob"] = round(float(sum(market[i] for i in {"1": [0], "X": [1], "2": [2], "1X": [0, 1], "X2": [1, 2], "12": [0, 2]}.get(item["pick"]["code"], []))), 4) if item["pick"]["code"] != "SKIP" else None
        out.append(item)
    return out, meta


@app.get("/")
def index():
    return render_template("index.html", leagues=LEAGUES)


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
    return jsonify(events=events, meta=meta, trained=model.is_trained(code), metrics=model.metrics(code))


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
