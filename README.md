# JP Planner v3

Deze versie voegt permanente PostgreSQL-opslag toe voor handmatige correcties na een Smart Event Manager import.

## Render environment variables
- `ORS_API_KEY`
- `FLASK_SECRET_KEY`
- `DATABASE_URL` (Internal Database URL van de Render Postgres database)

## Nieuwe flow
1. Upload PDF/XLSX.
2. Controleer de bewerkbare kolommen **Spel** en **Locatie / adres**.
3. Pas fouten handmatig aan.
4. Klik **Correcties opslaan**.
5. Correcties worden in PostgreSQL bewaard en bij volgende uploads opnieuw gebruikt.

De tabellen worden automatisch aangemaakt bij eerste gebruik.
