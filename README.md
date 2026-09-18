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

## v3.2 - Receptielijst als blokken
De parser herkent nu Smart Event Manager `Receptielijst voorzieningen` als reserveringsblokken. Een blok start bij de regel met tijd + klant/locatie + Ref en loopt door tot het volgende blok. Het spel wordt primair bepaald uit de expliciete `Voorziening`-regels en aanvullend uit de reserveringsomschrijving. Vrije notities tellen niet mee als spel, zodat bijvoorbeeld `BIJ SLECHT WEER VR GAME MEE` niet onterecht een extra activiteit maakt.
