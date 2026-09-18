# JP Planner v3.1

Deze versie voorkomt time-outs bij grote uploads.

## Belangrijkste wijziging
- Uploaden doet **geen geocoding per opdracht** meer.
- Spel en locatie/adres worden direct bewerkbaar getoond.
- Opgeslagen correcties worden in PostgreSQL onthouden.
- Alle correcties worden in één database-transactie opgeslagen.
- OpenRouteService wordt alleen gebruikt voor expliciete routetests (en later voor de aparte route-validatie/planningsstap).

## Environment variables
- `ORS_API_KEY`
- `FLASK_SECRET_KEY`
- `DATABASE_URL`

## Render
Build command:
`pip install -r requirements.txt`

Start command:
`gunicorn app:app`
