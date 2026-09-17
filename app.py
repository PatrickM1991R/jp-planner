import os

from flask import Flask, render_template, request
from dotenv import load_dotenv

from location_service import LocationService, LocationServiceError, clean_location_hint
from ors_client import ORSClient, ORSError
from parser import parse_upload

load_dotenv()
app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET_KEY', 'dev-change-me')


def enrich_rows(rows):
    """Resolve only unique location hints, then copy the result to every matching row."""
    service = LocationService()
    cache = {}
    for row in rows:
        hint = clean_location_hint(row.get('location_hint', ''))
        row['location_hint'] = hint
        if not hint:
            row['location'] = {'status': 'missing', 'query': ''}
            continue
        key = hint.casefold()
        if key not in cache:
            try:
                cache[key] = service.resolve(hint)
            except Exception as e:
                cache[key] = {'status': 'error', 'query': hint, 'error': str(e)}
        row['location'] = cache[key]
    return rows


@app.get('/')
def index():
    return render_template(
        'index.html',
        rows=None,
        error=None,
        route_result=None,
        ors_configured=LocationService().configured,
    )


@app.post('/upload')
def upload():
    f = request.files.get('file')
    if not f or not f.filename:
        return render_template('index.html', rows=None, error='Kies eerst een bestand.', route_result=None, ors_configured=LocationService().configured)
    try:
        rows = enrich_rows(parse_upload(f))
        return render_template('index.html', rows=rows, error=None, route_result=None, ors_configured=LocationService().configured)
    except Exception as e:
        return render_template('index.html', rows=None, error=str(e), route_result=None, ors_configured=LocationService().configured)


@app.post('/route-test')
def route_test():
    origin_text = clean_location_hint(request.form.get('origin', ''))
    destination_text = clean_location_hint(request.form.get('destination', ''))
    try:
        loc = LocationService()
        origin = loc.resolve(origin_text)
        destination = loc.resolve(destination_text)
        if origin.get('status') in {'missing', 'needs_api', 'unresolved', 'error'}:
            raise LocationServiceError(f'Startlocatie niet opgelost: {origin_text}')
        if destination.get('status') in {'missing', 'needs_api', 'unresolved', 'error'}:
            raise LocationServiceError(f'Eindlocatie niet opgelost: {destination_text}')

        route = ORSClient().route_summary(
            [origin['lon'], origin['lat']],
            [destination['lon'], destination['lat']],
        )
        result = {
            'origin': origin,
            'destination': destination,
            'distance_km': route['distance_km'],
            'duration_minutes': route['duration_minutes'],
        }
        return render_template('index.html', rows=None, error=None, route_result=result, ors_configured=True)
    except (LocationServiceError, ORSError, Exception) as e:
        return render_template('index.html', rows=None, error=str(e), route_result=None, ors_configured=LocationService().configured)


@app.get('/api/health')
def health():
    return {
        'ok': True,
        'ors_key_configured': bool(os.getenv('ORS_API_KEY')),
        'routing_profile': 'driving-car',
    }


if __name__ == '__main__':
    app.run(debug=True)
