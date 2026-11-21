"""Turn model probabilities into a pick that avoids guesswork.

- Confident (top outcome >= SINGLE_MIN): straight pick  -> 1 or 2 (or X)
- Unsure: best double chance                              -> 1X, X2 or 12
- Still too uncertain (best double chance < DC_MIN)       -> Skip
"""
from config import DC_MIN, SINGLE_MIN

NAMES = {"1": "Home win", "X": "Draw", "2": "Away win",
         "1X": "Home or Draw", "X2": "Away or Draw", "12": "Home or Away (no draw)"}


def recommend(probs, low_data=False):
    h, d, a = probs
    single = {"1": h, "X": d, "2": a}
    top_code, top_p = max(single.items(), key=lambda kv: kv[1])

    if top_p >= SINGLE_MIN and not low_data:
        code, p = top_code, top_p
    else:
        dc = {"1X": h + d, "X2": d + a, "12": h + a}
        code, p = max(dc.items(), key=lambda kv: kv[1])
        if p < DC_MIN or (low_data and p < DC_MIN + 0.05):
            return {"code": "SKIP", "label": "Skip - too uncertain", "prob": round(p, 4),
                    "fair_odds": None, "level": "none"}

    level = "high" if p >= 0.70 else "medium"
    return {"code": code, "label": NAMES[code], "prob": round(p, 4),
            "fair_odds": round(1 / p, 2), "level": level}
