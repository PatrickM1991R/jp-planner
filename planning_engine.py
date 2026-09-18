import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

from location_service import LocationService
from ors_client import ORSClient

STANDARD_GAMES = {
    'ik hou van holland', 'de alleskunner', 'alleskunner', 'minute to win it',
    'moordspel', 'crazy bingo', 'gekke bingo', 'boogschieten', 'pubquiz',
    'alles mag vandaag', 'hunted', 'expeditie robinson', 'archery tag',
    'archery attack', 'bubbelvoetbal'
}


def _norm(text):
    return ' '.join((text or '').lower().split())


def _dt(date_text, time_text):
    return datetime.fromisoformat(f"{date_text}T{time_text}:00")


def _participants(value):
    try:
        return int(str(value).split('-')[0].strip())
    except Exception:
        return 0


def _requires_standard_set(activity):
    low = _norm(activity)
    return any(g in low for g in STANDARD_GAMES)


def _special_flags(activity):
    low = _norm(activity)
    return {
        'casino': 'casino' in low,
        'western': 'western games' in low,
    }


def _resolve_many(texts):
    service = LocationService()
    unique = list(dict.fromkeys(t for t in texts if t))
    out = {}
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(unique)))) as pool:
        futures = {pool.submit(service.resolve, text): text for text in unique}
        for fut in as_completed(futures):
            text = futures[fut]
            try:
                item = fut.result()
                if item.get('lat') is not None and item.get('lon') is not None:
                    out[text] = item
                else:
                    out[text] = {'status': item.get('status', 'unresolved'), 'query': text}
            except Exception as exc:
                out[text] = {'status': 'error', 'query': text, 'error': str(exc)}
    return out


def build_logistics_plan(jobs, depots, vehicles, stock, resources, overrides=None):
    overrides = overrides or {}
    active_vehicles = [dict(v) for v in vehicles if v.get('active')]
    depot_map = {d['code']: dict(d) for d in depots if d.get('active', True)}
    stock_map = {(s['depot_code'], s['resource_code']): dict(s) for s in stock}
    resource_map = {r['resource_code']: dict(r) for r in resources}

    # Resolve only unique addresses, concurrently, then make one ORS matrix call.
    node_texts = [d['address'] for d in depot_map.values()]
    node_texts += [j.get('location_text', '') for j in jobs]
    resolved = _resolve_many(node_texts)

    warnings = []
    for text, item in resolved.items():
        if item.get('lat') is None or item.get('lon') is None:
            warnings.append(f"Locatie niet opgelost: {text}")

    valid_texts = [t for t in dict.fromkeys(node_texts) if resolved.get(t, {}).get('lat') is not None]
    idx = {t: i for i, t in enumerate(valid_texts)}
    coords = [[resolved[t]['lon'], resolved[t]['lat']] for t in valid_texts]

    if not coords:
        raise RuntimeError('Geen enkele locatie kon worden opgelost voor routeberekening.')

    matrix = ORSClient().matrix(coords)
    durations = matrix.get('durations') or []
    distances = matrix.get('distances') or []

    def travel(a, b):
        if a not in idx or b not in idx:
            return None, None
        i, k = idx[a], idx[b]
        try:
            sec = durations[i][k]
            km = distances[i][k]
            if sec is None or km is None:
                return None, None
            return float(sec) / 60.0, float(km)
        except Exception:
            return None, None

    states = {}
    for v in active_vehicles:
        depot = depot_map.get(v['depot_code'])
        if not depot:
            continue
        states[v['code']] = {
            'vehicle': v,
            'depot': depot,
            'location': depot['address'],
            'available_at': None,
            'jobs': [],
        }

    # Track simultaneous use of extra standard sets per depot.
    extra_set_usage = {code: [] for code in depot_map}

    plan_jobs = []
    sorted_jobs = sorted(jobs, key=lambda j: (_dt(j['date'], j['start']), j.get('reference', '')))

    for job in sorted_jobs:
        start_dt = _dt(job['date'], job['start'])
        end_dt = _dt(job['date'], job['end'])
        setup_minutes = max(0, int(job.get('setup_minutes', 30) or 0))
        cleanup_minutes = max(0, int(job.get('cleanup_minutes', 30) or 0))
        required_arrival = start_dt - timedelta(minutes=setup_minutes)
        available_after = end_dt + timedelta(minutes=cleanup_minutes)
        pax = _participants(job.get('participants'))
        activity = job.get('activity', '')
        needs_standard = _requires_standard_set(activity)
        special = _special_flags(activity)
        location = job.get('location_text', '')
        override = overrides.get(str(job.get('reference') or ''))

        candidates = []
        for code, state in states.items():
            if override and code != override:
                continue
            v = state['vehicle']

            # A normal standard game should travel with a bus that already contains a set.
            # For >50 pax a bus is still preferred, with extra depot stock flagged below.
            if needs_standard and not v.get('standard_game_set'):
                continue

            mins, km = travel(state['location'], location)
            if mins is None:
                continue

            if state['available_at'] is not None:
                arrival = state['available_at'] + timedelta(minutes=mins)
                if arrival > required_arrival:
                    continue

            # Greedy cost: actual repositioning km, plus a tiny penalty for taking a bus
            # from another depot when distances are effectively equal.
            score = km
            candidates.append((score, mins, km, code, state))

        if not candidates:
            reason = 'Geen voertuig kan deze opdracht op tijd bereiken'
            if override:
                reason += f" met handmatige keuze {override}"
            if needs_standard:
                reason += ' met een standaard spelset'
            plan_jobs.append({**job, 'vehicle_code': '', 'travel_minutes': None, 'travel_km': None,
                              'departure_time': '', 'status': 'unplanned', 'warnings': [reason]})
            warnings.append(f"Ref. {job.get('reference') or '?'}: {reason}.")
            continue

        candidates.sort(key=lambda x: (x[0], x[1], x[3]))
        _, mins, km, code, state = candidates[0]
        v = state['vehicle']
        job_warnings = []

        if state['available_at'] is None:
            departure = required_arrival - timedelta(minutes=mins)
        else:
            departure = state['available_at']

        extra_sets = 0
        if needs_standard:
            base_capacity = int(v.get('game_capacity') or 0) if v.get('standard_game_set') else 0
            extra_sets = max(0, math.ceil(max(0, pax - base_capacity) / 50))
            if extra_sets:
                depot_code = v['depot_code']
                available_sets = int((stock_map.get((depot_code, 'STANDARD_GAME_SET')) or {}).get('quantity') or 0)
                overlaps = [u for u in extra_set_usage[depot_code]
                            if not (available_after <= u['start'] or required_arrival >= u['end'])]
                used = sum(u['sets'] for u in overlaps)
                if used + extra_sets <= available_sets:
                    extra_set_usage[depot_code].append({'start': required_arrival, 'end': available_after, 'sets': extra_sets})
                    job_warnings.append(f"{extra_sets} extra standaard spelset(s) meenemen uit {state['depot']['name']}.")
                else:
                    job_warnings.append(
                        f"WAARSCHUWING: {extra_sets} extra set(s) nodig; in {state['depot']['name']} zijn er tijdens dit tijdvak onvoldoende vrije extra sets."
                    )

        if special['casino']:
            qty = int((resource_map.get('CASINO') or {}).get('quantity') or 0)
            job_warnings.append(f"Casino-materiaal apart laden (totaal {qty} set(s) beschikbaar).")
        if special['western']:
            qty = int((resource_map.get('WESTERN_GAMES') or {}).get('quantity') or 0)
            if qty <= 0:
                job_warnings.append('Western Games is niet standaard in een voertuig; beschikbare set(s) zijn nog niet vastgelegd.')
            else:
                job_warnings.append(f"Western Games apart laden ({qty} set(s) beschikbaar).")

        entry = {**job, 'vehicle_code': code, 'travel_minutes': round(mins), 'travel_km': round(km, 1),
                 'departure_time': departure.strftime('%H:%M'),
                 'arrival_time': required_arrival.strftime('%H:%M'),
                 'available_time': available_after.strftime('%H:%M'),
                 'status': 'planned', 'warnings': job_warnings}
        plan_jobs.append(entry)

        state['jobs'].append(entry)
        state['location'] = location
        state['available_at'] = available_after

    # Build vehicle route summaries including return to depot after final job.
    vehicle_routes = []
    total_km = 0.0
    total_drive = 0.0
    for code, state in states.items():
        if not state['jobs']:
            continue
        route_jobs = state['jobs']
        route_km = sum(float(j.get('travel_km') or 0) for j in route_jobs)
        route_min = sum(float(j.get('travel_minutes') or 0) for j in route_jobs)
        last = route_jobs[-1]
        ret_min, ret_km = travel(last['location_text'], state['depot']['address'])
        if ret_min is not None:
            route_min += ret_min
            route_km += ret_km
            return_time = _dt(last['date'], last['end']) + timedelta(minutes=int(last.get('cleanup_minutes', 30) or 0) + ret_min)
            return_text = return_time.strftime('%H:%M')
        else:
            return_text = 'onbekend'
        total_km += route_km
        total_drive += route_min
        vehicle_routes.append({
            'vehicle_code': code,
            'depot_name': state['depot']['name'],
            'depot_address': state['depot']['address'],
            'jobs': route_jobs,
            'distance_km': round(route_km, 1),
            'drive_minutes': round(route_min),
            'first_departure': route_jobs[0]['departure_time'],
            'return_time': return_text,
        })

    return {
        'jobs': plan_jobs,
        'vehicle_routes': vehicle_routes,
        'vehicles': active_vehicles,
        'warnings': warnings,
        'total_distance_km': round(total_km, 1),
        'total_drive_minutes': round(total_drive),
        'unplanned_count': sum(1 for j in plan_jobs if j['status'] != 'planned'),
    }
