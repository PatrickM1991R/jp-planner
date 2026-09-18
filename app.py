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


def apply_saved_activity(row):
    original = row.get('activity', '')
    row['original_activity'] = original
    ref_fix = db.get_reservation_correction(row.get('reference')) if db.configured() else None
    if ref_fix and ref_fix.get('activity'):
        row['activity'] = ref_fix['activity']
        row['activity_source'] = 'reference'
        return
    alias = db.get_activity_alias(original) if db.configured() and original and original != 'ONBEKEND' else None
    if alias:
        row['activity'] = alias
        row['activity_source'] = 'database'
    else:
        row['activity_source'] = 'automatic'


def resolve_row_location(row, service):
    hint = clean_location_hint(row.get('location_hint', ''))
    row['location_hint'] = hint
    row['original_location_hint'] = hint

    ref_fix = db.get_reservation_correction(row.get('reference')) if db.configured() else None
    corrected_text = (ref_fix or {}).get('location_text') if ref_fix else None

    alias = db.get_location_alias(hint) if db.configured() and hint else None
    if corrected_text:
        query = corrected_text
        try:
            resolved = service.resolve(query, use_cache=False)
        except Exception as e:
            resolved = {'status': 'error', 'query': query, 'error': str(e)}
        row['location_edit'] = query
        row['location'] = resolved
        row['location_source'] = 'reference'
        return

    if alias:
        row['location_edit'] = alias.get('location_text') or hint
        if alias.get('lat') is not None and alias.get('lon') is not None:
            row['location'] = {
                'status': 'confirmed',
                'query': row['location_edit'],
                'label': alias.get('label') or row['location_edit'],
                'lat': alias.get('lat'),
                'lon': alias.get('lon'),
            }
        else:
            try:
                row['location'] = service.resolve(row['location_edit'], use_cache=False)
            except Exception as e:
                row['location'] = {'status': 'error', 'query': row['location_edit'], 'error': str(e)}
        row['location_source'] = 'database'
        return

    row['location_edit'] = hint
    if not hint:
        row['location'] = {'status': 'missing', 'query': ''}
        row['location_source'] = 'automatic'
        return
    try:
        row['location'] = service.resolve(hint)
    except Exception as e:
        row['location'] = {'status': 'error', 'query': hint, 'error': str(e)}
    row['location_source'] = 'automatic'


def enrich_rows(rows):
    service = LocationService()
    for row in rows:
        apply_saved_activity(row)
        resolve_row_location(row, service)
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
    saved = 0
    warnings = []
    service = LocationService()

    for i in range(count):
        reference = (request.form.get(f'reference_{i}') or '').strip()
        original_activity = (request.form.get(f'original_activity_{i}') or '').strip()
        original_location_hint = (request.form.get(f'original_location_hint_{i}') or '').strip()
        activity = (request.form.get(f'activity_{i}') or '').strip()
        location_text = (request.form.get(f'location_{i}') or '').strip()

        if reference:
            db.upsert_reservation_correction(reference, activity, location_text)

        if original_activity and original_activity != 'ONBEKEND' and activity and activity != original_activity:
            db.upsert_activity_alias(original_activity, activity)

        if original_location_hint and location_text:
            resolved = None
            try:
                candidate = service.resolve(location_text, use_cache=False)
                if candidate.get('status') not in {'missing', 'needs_api', 'unresolved', 'error'}:
                    resolved = candidate
                else:
                    warnings.append(f'{location_text}: niet betrouwbaar geocoded')
            except Exception:
                warnings.append(f'{location_text}: geocoding mislukt')
            db.upsert_location_alias(original_location_hint, location_text, resolved)
        saved += 1

    msg = f'{saved} regels opgeslagen. JP Planner gebruikt deze correcties bij volgende uploads.'
    if warnings:
        msg += ' Let op: ' + '; '.join(warnings[:5])
    return render_home(message=msg)


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
