# Edge — Sports Betting Analytics

Flask app: live odds (The Odds API) + historical results (football-data.co.uk) → Elo/form features → logistic-regression win probabilities → mobile-friendly dashboard with value (EV) flags.

## Run
```bash
pip install -r requirements.txt
cp .env.example .env        # add ODDS_API_KEY (free at the-odds-api.com)
python manage.py init       # download history + train all leagues (or: python manage.py init E0)
python app.py               # http://localhost:5000
```
Production: `gunicorn -w 1 -b 0.0.0.0:8000 app:app` (use 1 worker: the odds cache and retrain job are in-process).

## Layout
- `config.py` settings/leagues · `db.py` SQLite · `pipeline.py` historical ingestion
- `features.py` Elo + rolling form (no look-ahead) · `model.py` train/evaluate/predict
- `odds_client.py` live odds, caching, team-name matching · `app.py` API · `templates/index.html` UI

## Notes
- Model is evaluated on a chronological 80/20 split and compared with de-vigged bookmaker odds (log-loss). Beating the market is hard; expect to roughly match it at best.
- Free Odds API tier = 500 credits/month; responses are cached for `ODDS_CACHE_TTL` seconds.
- Odds are 1X2 football only. Add leagues in `config.LEAGUES`.
