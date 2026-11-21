"""Train / load / predict. One multinomial logistic regression per league."""
import json
import threading
from datetime import datetime, timezone

import joblib
import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, log_loss
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

import db
from config import MODEL_DIR
from features import FEATURES, build_training_frame, implied_probs

_lock = threading.Lock()
_cache: dict = {}


def _paths(code):
    return MODEL_DIR / f"{code}.joblib", MODEL_DIR / f"{code}.metrics.json"


def train_league(code: str) -> dict:
    df = db.load_matches(code)
    if len(df) < 300:
        raise ValueError(f"Only {len(df)} matches for {code}; run ingestion first.")
    frame, _ = build_training_frame(df)
    X, y = frame[FEATURES].values, frame["y"].values

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

    final = make().fit(X, y)  # refit on everything for live predictions
    mpath, jpath = _paths(code)
    joblib.dump(final, mpath)
    jpath.write_text(json.dumps(metrics, indent=2))
    with _lock:
        _cache.pop(code, None)
    return metrics


def _load(code):
    with _lock:
        if code in _cache:
            return _cache[code]
        mpath, jpath = _paths(code)
        if not mpath.exists():
            return None
        _, state = build_training_frame(db.load_matches(code))
        metrics = json.loads(jpath.read_text()) if jpath.exists() else {}
        _cache[code] = (joblib.load(mpath), state, metrics)
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
    """Returns {'probs':[H,D,A], 'home_elo':..., 'away_elo':...} or None."""
    loaded = _load(code)
    if not loaded or home is None or away is None:
        return None
    pipe, state, _ = loaded
    feats = state.features(home, away)
    probs = pipe.predict_proba(pd.DataFrame([feats])[FEATURES].values)[0]
    return {
        "probs": [float(p) for p in probs],
        "home_elo": round(feats["home_elo"]), "away_elo": round(feats["away_elo"]),
        "low_data": min(state.games.get(home, 0), state.games.get(away, 0)) < 10,
    }
