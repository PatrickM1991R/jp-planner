JP Planner v6.5 timeout patch

Overschrijf in GitHub alleen:
- planning_engine.py
- location_service.py

Wijzigingen:
- geen sleep/retry-lussen meer tijdens planning
- maximaal 4 locatie-opzoekingen parallel
- ORS geocode korte connect/read timeout
- mislukte locatie blokkeert planning niet
- succesvolle geocodes blijven in PostgreSQL-cache staan
