# JP Planner v2
Personeels- en logistieke planner in opbouw voor JP Activiteiten.

## Aanwezig
- Smart Event Manager PDF-import
- Excel-import
- Activiteitenherkenning, combinaties en MTWI
- Locatiehint uit omschrijving
- OpenRouteService geocoding in Nederland
- Lokale locatiecache (`data/locations.json`)
- OpenRouteService `driving-car` Directions + Matrix client
- Route-test in de webinterface: locatie A -> locatie B -> km + rijtijd
- Render-configuratie

## Lokaal draaien
```bash
pip install -r requirements.txt
export ORS_API_KEY="jouw_sleutel"
python app.py
```
Open daarna http://127.0.0.1:5000.

## Render
Maak een aparte Render Web Service voor JP Planner en zet minimaal:
- `ORS_API_KEY`
- `FLASK_SECRET_KEY` wordt door `render.yaml` gegenereerd.

Dezelfde ORS-key als Hunted kan technisch worden gebruikt zolang quota/limieten voldoende zijn.

## Belangrijk voor productie
De JSON-locatiecache is voor de MVP. Render-filesystem is niet bedoeld als permanente database. Zodra personeel/logistiek wordt toegevoegd, migreren we de locatiebibliotheek mee naar PostgreSQL.

## Volgende stap
1. Personeelsbestand importeren (beschikbaarheid + kwaliteiten)
2. Logistiekbestand importeren (bussen/auto's + standplaats)
3. Matrix van relevante reiscombinaties genereren
4. Optimalisatie-engine bouwen
