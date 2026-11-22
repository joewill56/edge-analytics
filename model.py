"""Train / load / predict. Per league: a 1X2 classifier + Poisson goal models (with Dixon-Coles rho)."""
import json
import threading
from datetime import datetime, timezone

import joblib
import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression, PoissonRegressor
from sklearn.metrics import accuracy_score, log_loss
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

import db
import markets
from config import BLEND_WEIGHT, MODEL_DIR
from features import FEATURES, build_training_frame, implied_probs

_lock = threading.Lock()
_cache: dict = {}


def _paths(code):
    return MODEL_DIR / f"{code}.joblib", MODEL_DIR / f"{code}.metrics.json", MODEL_DIR / f"{code}.goals.joblib"


def _fit_goals(X, hg, ag):
    mk = lambda: make_pipeline(StandardScaler(), PoissonRegressor(alpha=0.01, max_iter=1000))
    return mk().fit(X, hg), mk().fit(X, ag)


def _lam(pipe, X):
    return np.clip(pipe.predict(X), 0.2, 4.5)


def _fit_rho(lh, la, hg, ag):
    """Dixon-Coles low-score correction by grid-search maximum likelihood."""
    best, best_ll = 0.0, -1e18
    m00, m01, m10, m11 = (hg == 0) & (ag == 0), (hg == 0) & (ag == 1), (hg == 1) & (ag == 0), (hg == 1) & (ag == 1)
    for rho in np.arange(-0.25, 0.101, 0.025):
        tau = np.ones(len(lh))
        tau[m00] = 1 - lh[m00] * la[m00] * rho
        tau[m01] = 1 + lh[m01] * rho
        tau[m10] = 1 + la[m10] * rho
        tau[m11] = 1 - rho
        if (tau <= 0).any():
            continue
        ll = np.log(tau).sum()
        if ll > best_ll:
            best, best_ll = float(rho), ll
    return best


def _ll(y, p):
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def _market_backtest(frame, split, ph, pa, rho, proba):
    """Run the exact live pipeline (blend -> anchor -> score matrix) on the held-out matches."""
    train, test = frame.iloc[:split], frame.iloc[split:]
    defs = [d for d in markets.binary_defs()]
    lh_t, la_t = _lam(ph, test[FEATURES].values), _lam(pa, test[FEATURES].values)
    odds = test[["b365h", "b365d", "b365a"]].values.astype(float)
    P = np.zeros((len(test), len(defs)))
    for i in range(len(test)):
        target = proba[i]
        if not np.isnan(odds[i]).any():
            target = BLEND_WEIGHT * proba[i] + (1 - BLEND_WEIGHT) * implied_probs(*odds[i])
        lh, la = markets.anchor(lh_t[i], la_t[i], rho, target)
        m = markets.score_matrix(lh, la, rho)
        P[i] = [m[mask].sum() for _, _, _, mask in defs]
    hg_t, ag_t = test["hg"].values, test["ag"].values
    hg_r, ag_r = train["hg"].values, train["ag"].values
    out = {}
    for j, (_, key, label, mask) in enumerate(defs):
        y = np.array([markets.outcome(mask, h, a) for h, a in zip(hg_t, ag_t)], dtype=float)
        base = np.mean([markets.outcome(mask, h, a) for h, a in zip(hg_r, ag_r)])
        p = P[:, j]
        hi = p >= 0.70
        out[key] = {"label": label, "n": int(len(y)), "log_loss": round(_ll(y, p), 4),
                    "base_log_loss": round(_ll(y, np.full(len(y), base)), 4),
                    "n70": int(hi.sum()), "hit70": round(float(y[hi].mean()), 4) if hi.any() else None,
                    "avg70": round(float(p[hi].mean()), 4) if hi.any() else None}
    extra = {}
    bo, bu = test["bo"].values.astype(float), test["bu"].values.astype(float)
    ok = ~(np.isnan(bo) | np.isnan(bu))
    if ok.sum() > 50:
        j = next(k for k, d in enumerate(defs) if d[1] == "over_2.5")
        y = (hg_t + ag_t > 2).astype(float)[ok]
        mkt = (1 / bo[ok]) / (1 / bo[ok] + 1 / bu[ok])
        extra = {"n": int(ok.sum()), "model": round(_ll(y, P[ok, j]), 4), "market": round(_ll(y, mkt), 4)}
    return out, extra


def train_league(code: str) -> dict:
    df = db.load_matches(code)
    if len(df) < 300:
        raise ValueError(f"Only {len(df)} matches for {code}; run ingestion first.")
    frame, _ = build_training_frame(df)
    X, y = frame[FEATURES].values, frame["y"].values
    hg, ag = frame["hg"].values, frame["ag"].values

    split = int(len(frame) * 0.8)  # chronological split, never shuffled
    make = lambda: make_pipeline(StandardScaler(), LogisticRegression(C=1.0, max_iter=2000))
    probe = make().fit(X[:split], y[:split])
    proba = probe.predict_proba(X[split:])
    test = frame.iloc[split:]

    metrics = {
        "league": code, "matches": int(len(df)), "train_rows": int(split), "test_rows": int(len(test)),
        "accuracy": round(float(accuracy_score(y[split:], proba.argmax(1))), 4),
        "log_loss": round(float(log_loss(y[split:], proba, labels=[0, 1, 2])), 4),
        "trained_at": datetime.now(timezone.utc).isoformat(),
    }
    # Baseline: de-vigged bookmaker closing odds on the same test rows
    mk = test.dropna(subset=["b365h", "b365d", "b365a"])
    if len(mk) > 50:
        mp = np.vstack([implied_probs(r.b365h, r.b365d, r.b365a) for r in mk.itertuples()])
        metrics["market_log_loss"] = round(float(log_loss(mk["y"].values, mp, labels=[0, 1, 2])), 4)
        metrics["market_accuracy"] = round(float(accuracy_score(mk["y"].values, mp.argmax(1))), 4)
        mask = test[["b365h", "b365d", "b365a"]].notna().all(axis=1).values
        blend = BLEND_WEIGHT * proba[mask] + (1 - BLEND_WEIGHT) * mp
        metrics["blend_log_loss"] = round(float(log_loss(mk["y"].values, blend, labels=[0, 1, 2])), 4)
        metrics["blend_accuracy"] = round(float(accuracy_score(mk["y"].values, blend.argmax(1))), 4)

    # Goal models: honest backtest first (train part only), then refit on everything
    ph, pa = _fit_goals(X[:split], hg[:split], ag[:split])
    rho = _fit_rho(_lam(ph, X[:split]), _lam(pa, X[:split]), hg[:split], ag[:split])
    metrics["markets"], over25 = _market_backtest(frame, split, ph, pa, rho, proba)
    if over25:
        metrics["over25_vs_market"] = over25

    final = make().fit(X, y)  # refit on everything for live predictions
    ph_f, pa_f = _fit_goals(X, hg, ag)
    rho_f = _fit_rho(_lam(ph_f, X), _lam(pa_f, X), hg, ag)
    mpath, jpath, gpath = _paths(code)
    joblib.dump(final, mpath)
    joblib.dump({"ph": ph_f, "pa": pa_f, "rho": rho_f}, gpath)
    jpath.write_text(json.dumps(metrics, indent=2))
    with _lock:
        _cache.pop(code, None)
    return metrics


def _load(code):
    with _lock:
        if code in _cache:
            return _cache[code]
        mpath, jpath, gpath = _paths(code)
        if not mpath.exists():
            return None
        _, state = build_training_frame(db.load_matches(code))
        metrics = json.loads(jpath.read_text()) if jpath.exists() else {}
        goals = joblib.load(gpath) if gpath.exists() else None
        _cache[code] = (joblib.load(mpath), state, metrics, goals)
        return _cache[code]


def is_trained(code):
    return _paths(code)[0].exists()


def metrics(code):
    loaded = _load(code)
    return loaded[2] if loaded else None


def known_teams(code):
    loaded = _load(code)
    return sorted(loaded[1].games) if loaded else []


def ratings(code, top=40):
    loaded = _load(code)
    if not loaded:
        return []
    st = loaded[1]
    rows = [{"team": t, "elo": round(e), "games": st.games.get(t, 0)} for t, e in st.elo.items()]
    return sorted(rows, key=lambda r: -r["elo"])[:top]


def predict(code, home, away):
    """Returns 1X2 probs, Elo, and (if goal models exist) expected goals for the markets module."""
    loaded = _load(code)
    if not loaded or home is None or away is None:
        return None
    pipe, state, _, goals = loaded
    feats = state.features(home, away)
    x = pd.DataFrame([feats])[FEATURES].values
    probs = pipe.predict_proba(x)[0]
    out = {
        "probs": [float(p) for p in probs],
        "home_elo": round(feats["home_elo"]), "away_elo": round(feats["away_elo"]),
        "low_data": min(state.games.get(home, 0), state.games.get(away, 0)) < 10,
    }
    if goals:
        out["goal"] = {"lh": float(_lam(goals["ph"], x)[0]), "la": float(_lam(goals["pa"], x)[0]), "rho": goals["rho"]}
    return out
