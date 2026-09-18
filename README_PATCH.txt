JP Planner v6.4 - locatie/route robuustheid

Overschrijf in GitHub alleen:
- planning_engine.py
- location_service.py
- db.py

Wat is aangepast:
1. Geocodes worden permanent in PostgreSQL opgeslagen (geocode_cache).
2. Locaties worden sequentieel met retries opgelost; geen bulk-threading meer tegen ORS.
3. Een onvindbare locatie blokkeert niet meer de hele planning.
4. Alleen die opdracht krijgt status: LOCATIE CONTROLEREN.
5. De rest van de opdrachten blijft planbaar.
6. Een mislukte ORS Matrix-call geeft een waarschuwing in plaats van een crash.

Na deploy: upload hetzelfde opdrachtenbestand en genereer opnieuw.
