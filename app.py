import os

from flask import Flask, render_template, request
from dotenv import load_dotenv

import db
from location_service import LocationService, LocationServiceError, clean_location_hint
from ors_client import ORSClient, ORSError
from parser import parse_upload

load_dotenv()
app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET_KEY', 'dev-change-me')


def db_status():
    try:
        if not db.configured():
            return False
        db.ensure_schema()
        return True
    except Exception:
        return False


def enrich_rows(rows):
    """Apply saved corrections without calling ORS for every uploaded row."""
    activities, locations, reservations = db.preload_corrections() if db.configured() else ({}, {}, {})

    for row in rows:
        original_activity = row.get('activity', '')
        row['original_activity'] = original_activity
        reference = str(row.get('reference') or '')
        ref_fix = reservations.get(reference)

        if ref_fix and ref_fix.get('activity'):
            row['activity'] = ref_fix['activity']
            row['activity_source'] = 'reference'
        else:
            alias = activities.get(db.norm_key(original_activity)) if original_activity and original_activity != 'ONBEKEND' else None
            if alias:
                row['activity'] = alias
                row['activity_source'] = 'database'
            else:
                row['activity_source'] = 'automatic'

        hint = clean_location_hint(row.get('location_hint', ''))
        row['location_hint'] = hint
        row['original_location_hint'] = hint
        loc_alias = locations.get(db.norm_key(hint)) if hint else None
        corrected_text = (ref_fix or {}).get('location_text') if ref_fix else None

        if corrected_text:
            row['location_edit'] = corrected_text
            row['location_source'] = 'reference'
            row['location'] = {'status': 'saved', 'query': corrected_text, 'label': corrected_text}
        elif loc_alias:
            row['location_edit'] = loc_alias.get('location_text') or hint
            row['location_source'] = 'database'
            if loc_alias.get('lat') is not None and loc_alias.get('lon') is not None:
                row['location'] = {
                    'status': 'confirmed',
                    'query': row['location_edit'],
                    'label': loc_alias.get('label') or row['location_edit'],
                    'lat': loc_alias.get('lat'),
                    'lon': loc_alias.get('lon'),
                }
            else:
                row['location'] = {'status': 'saved', 'query': row['location_edit'], 'label': row['location_edit']}
        else:
            row['location_edit'] = hint
            row['location_source'] = 'automatic'
            row['location'] = {
                'status': 'needs_review' if hint else 'missing',
                'query': hint,
                'label': '',
            }

    return rows


def render_home(**kwargs):
    defaults = {
        'rows': None,
        'error': None,
        'message': None,
        'route_result': None,
        'ors_configured': LocationService().configured,
        'db_configured': db_status(),
    }
    defaults.update(kwargs)
    return render_template('index.html', **defaults)


@app.get('/')
def index():
    return render_home()


@app.post('/upload')
def upload():
    f = request.files.get('file')
    if not f or not f.filename:
        return render_home(error='Kies eerst een bestand.')
    try:
        rows = enrich_rows(parse_upload(f))
        return render_home(rows=rows)
    except Exception as e:
        return render_home(error=str(e))


@app.post('/save-corrections')
def save_corrections():
    if not db_status():
        return render_home(error='Database is nog niet gekoppeld of bereikbaar.')

    count = int(request.form.get('row_count', '0') or 0)
    items = []
    for i in range(count):
        items.append({
            'reference': (request.form.get(f'reference_{i}') or '').strip(),
            'original_activity': (request.form.get(f'original_activity_{i}') or '').strip(),
            'original_location_hint': (request.form.get(f'original_location_hint_{i}') or '').strip(),
            'activity': (request.form.get(f'activity_{i}') or '').strip(),
            'location_text': (request.form.get(f'location_{i}') or '').strip(),
        })

    try:
        db.save_corrections_batch(items)
        return render_home(message=f'{len(items)} regels opgeslagen. JP Planner gebruikt deze correcties bij volgende uploads.')
    except Exception as e:
        return render_home(error=f'Opslaan mislukt: {e}')


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
        return render_home(route_result=result)
    except (LocationServiceError, ORSError, Exception) as e:
        return render_home(error=str(e))


@app.get('/api/health')
def health():
    return {
        'ok': True,
        'ors_key_configured': bool(os.getenv('ORS_API_KEY')),
        'database_configured': db_status(),
        'routing_profile': 'driving-car',
    }


if __name__ == '__main__':
    app.run(debug=True)
