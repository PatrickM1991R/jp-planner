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

OWN_TRANSPORT_PREFIX = 'Eigen vervoer + spelset'


def _norm(text):
    return ' '.join((text or '').lower().split())


def _dt(date_text, time_text):
    return datetime.fromisoformat(f"{date_text}T{time_text}:00")


def _participants(value):
    try:
        return int(str(value).split('-')[0].strip())
    except Exception:
        return 0


def _format_minutes(value):
    if value is None:
        return 'onbekend'
    value = max(0, int(round(value)))
    hours, minutes = divmod(value, 60)
    if hours and minutes:
        return f'{hours} uur {minutes} min'
    if hours:
        return f'{hours} uur'
    return f'{minutes} min'


def _requires_standard_set(activity):
    low = _norm(activity)
    return any(g in low for g in STANDARD_GAMES)


def _special_flags(activity):
    low = _norm(activity)
    return {
        'casino': 'casino' in low,
        'western': 'western games' in low,
    }


def _own_code(depot):
    return f"{OWN_TRANSPORT_PREFIX} {depot['name']}"


def _is_own_transport(code):
    return bool(code) and code.startswith(OWN_TRANSPORT_PREFIX)


def _resolve_many(texts):
    """Resolve locations fast enough for a synchronous Render request.

    Cached locations return immediately. Uncached locations are geocoded in a
    small worker pool with a short HTTP timeout. There are deliberately no
    sleeps or retries here: an unresolved location is marked for manual review
    while the rest of the planning continues.
    """
    unique = list(dict.fromkeys(t for t in texts if t))
    out = {}

    def resolve_one(text):
        try:
            item = LocationService().resolve(text, use_cache=True)
            if item.get('lat') is not None and item.get('lon') is not None:
                return text, item
            return text, {
                'status': item.get('status', 'unresolved'),
                'query': text,
                'error': item.get('error', ''),
            }
        except Exception as exc:
            return text, {'status': 'unresolved', 'query': text, 'error': str(exc)}

    # A small pool prevents a burst while avoiding serial 5s+ waits for many jobs.
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(resolve_one, text) for text in unique]
        for future in as_completed(futures):
            text, item = future.result()
            out[text] = item

    return out


def build_logistics_plan(jobs, depots, vehicles, stock, resources, overrides=None):
    overrides = overrides or {}
    active_vehicles = [dict(v) for v in vehicles if v.get('active')]
    depot_map = {d['code']: dict(d) for d in depots if d.get('active', True)}
    stock_map = {(s['depot_code'], s['resource_code']): dict(s) for s in stock}
    resource_map = {r['resource_code']: dict(r) for r in resources}

    # Manual fallback options shown in the planning UI. These are not physical fleet
    # vehicles: they mean a staff member uses own transport and loads material at depot.
    own_transport_options = [
        {
            'code': _own_code(depot),
            'depot_code': depot['code'],
            'vehicle_type': 'eigen vervoer',
            'active': True,
            'standard_game_set': False,
            'game_capacity': 0,
            'notes': f"Eigen vervoer; spelset ophalen uit voorraad {depot['name']}",
        }
        for depot in depot_map.values()
    ]
    selectable_vehicles = active_vehicles + own_transport_options

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

    durations = []
    distances = []
    if coords:
        try:
            matrix = ORSClient().matrix(coords)
            durations = matrix.get('durations') or []
            distances = matrix.get('distances') or []
        except Exception as exc:
            warnings.append(f"Route-matrix kon niet worden berekend: {exc}")
    else:
        warnings.append('Geen enkele locatie kon automatisch worden opgelost. Controleer de gemarkeerde locaties en herbereken.')

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

    # A physical vehicle starts a fresh service from its depot for every date.
    # The key is therefore (vehicle_code, date). This prevents a route from
    # accidentally continuing from Friday into Saturday.
    vehicle_templates = {}
    for v in active_vehicles:
        depot = depot_map.get(v['depot_code'])
        if not depot:
            continue
        vehicle_templates[v['code']] = {'vehicle': v, 'depot': depot}

    states = {}

    def day_state(code, date_text):
        key = (code, date_text)
        if key not in states:
            template = vehicle_templates[code]
            states[key] = {
                'vehicle': template['vehicle'],
                'depot': template['depot'],
                'date': date_text,
                'location': template['depot']['address'],
                'available_at': None,
                'jobs': [],
            }
        return states[key]

    # Tracks depot stock reserved during overlapping work blocks. Company buses use
    # this only for extra sets; own transport uses it for every required set.
    extra_set_usage = {code: [] for code in depot_map}

    def available_standard_sets(depot_code, required_arrival, available_after):
        total = int((stock_map.get((depot_code, 'STANDARD_GAME_SET')) or {}).get('quantity') or 0)
        overlaps = [u for u in extra_set_usage.get(depot_code, [])
                    if not (available_after <= u['start'] or required_arrival >= u['end'])]
        used = sum(u['sets'] for u in overlaps)
        return max(0, total - used), total

    def reserve_standard_sets(depot_code, required_arrival, available_after, sets):
        extra_set_usage.setdefault(depot_code, []).append({
            'start': required_arrival,
            'end': available_after,
            'sets': sets,
        })

    plan_jobs = []
    own_transport_routes = []
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

        # A single unresolved job location must never block the rest of the day.
        # Keep it visible as a manual action item and continue planning all other jobs.
        location_result = resolved.get(location, {})
        if not location or location_result.get('lat') is None or location_result.get('lon') is None:
            reason = f"⚠️ LOCATIE CONTROLEREN: '{location or 'geen locatie'}' kon niet automatisch worden opgelost voor routeberekening."
            plan_jobs.append({
                **job,
                'vehicle_code': override or '',
                'travel_minutes': None,
                'travel_km': None,
                'departure_time': '',
                'arrival_time': required_arrival.strftime('%H:%M'),
                'available_time': available_after.strftime('%H:%M'),
                'status': 'location_problem',
                'warnings': [reason],
                'material_problem': False,
                'location_problem': True,
            })
            warnings.append(f"Ref. {job.get('reference') or '?'}: {reason}")
            continue

        # 1) First try the fixed company fleet, unless own transport was explicitly chosen.
        candidates = []
        if not _is_own_transport(override):
            for code in vehicle_templates:
                if override and code != override:
                    continue
                state = day_state(code, job['date'])
                v = state['vehicle']
                if needs_standard and not v.get('standard_game_set'):
                    continue
                mins, km = travel(state['location'], location)
                if mins is None:
                    continue
                if state['available_at'] is not None:
                    arrival = state['available_at'] + timedelta(minutes=mins)
                    if arrival > required_arrival:
                        continue
                candidates.append((km, mins, code, state))

        chosen_kind = None
        chosen = None

        if candidates:
            candidates.sort(key=lambda x: (x[0], x[1], x[2]))
            chosen_kind = 'fleet'
            chosen = candidates[0]

        # 2) Fallback: own transport + standard game set from depot stock.
        # Only used automatically when the fixed fleet cannot make the assignment.
        own_candidates = []
        if chosen is None and needs_standard:
            required_sets = max(1, math.ceil(max(1, pax) / 50))
            for depot in depot_map.values():
                code = _own_code(depot)
                if override and code != override:
                    continue
                free_sets, total_sets = available_standard_sets(depot['code'], required_arrival, available_after)
                if free_sets < required_sets:
                    continue
                mins, km = travel(depot['address'], location)
                if mins is None:
                    continue
                departure = required_arrival - timedelta(minutes=mins)
                own_candidates.append((km, mins, code, depot, required_sets, departure, total_sets))

            if own_candidates:
                own_candidates.sort(key=lambda x: (x[0], x[1], x[2]))
                chosen_kind = 'own'
                chosen = own_candidates[0]

        if chosen is None:
            if needs_standard:
                required_sets = max(1, math.ceil(max(1, pax) / 50))
                depot_details = []
                for depot in depot_map.values():
                    free_sets, total_sets = available_standard_sets(depot['code'], required_arrival, available_after)
                    depot_details.append(f"{depot['name']}: {free_sets}/{total_sets} vrije set(s)")
                reason = (
                    f"❗ MATERIAALPROBLEEM: geen vaste bus kan deze opdracht passend uitvoeren en "
                    f"eigen vervoer heeft {required_sets} standaard spelset(s) nodig. "
                    + '; '.join(depot_details)
                )
                status = 'material_problem'
            else:
                reason = 'Geen voertuig kan deze opdracht op tijd bereiken.'
                status = 'unplanned'
            if override:
                reason += f" Handmatige keuze: {override}."
            plan_jobs.append({
                **job,
                'vehicle_code': '',
                'travel_minutes': None,
                'travel_km': None,
                'departure_time': '',
                'arrival_time': required_arrival.strftime('%H:%M'),
                'available_time': available_after.strftime('%H:%M'),
                'status': status,
                'warnings': [reason],
                'material_problem': status == 'material_problem',
                'location_problem': False,
            })
            warnings.append(f"Ref. {job.get('reference') or '?'}: {reason}")
            continue

        job_warnings = []

        if chosen_kind == 'own':
            km, mins, code, depot, required_sets, departure, total_sets = chosen
            reserve_standard_sets(depot['code'], required_arrival, available_after, required_sets)
            job_warnings.append(
                f"Eigen vervoer: {required_sets} standaard spelset(s) ophalen uit voorraad {depot['name']} "
                f"({depot['address']})."
            )
            entry = {
                **job,
                'vehicle_code': code,
                'travel_minutes': round(mins),
                'travel_km': round(km, 1),
                'departure_time': departure.strftime('%H:%M'),
                'arrival_time': required_arrival.strftime('%H:%M'),
                'available_time': available_after.strftime('%H:%M'),
                'status': 'planned_own_transport',
                'warnings': job_warnings,
                'material_problem': False,
                'location_problem': False,
                'own_transport': True,
                'pickup_depot_name': depot['name'],
                'pickup_depot_address': depot['address'],
                'sets_from_stock': required_sets,
            }
            plan_jobs.append(entry)

            # Own transport is intentionally independent per job until named staff are
            # introduced. For logistics totals we show pickup -> job -> depot.
            ret_min, ret_km = travel(location, depot['address'])
            route_min = mins + (ret_min or 0)
            route_km = km + (ret_km or 0)
            return_dt = available_after + timedelta(minutes=ret_min) if ret_min is not None else None
            duty_start_dt = departure
            duty_minutes = round((return_dt - duty_start_dt).total_seconds() / 60) if return_dt else None
            own_transport_routes.append({
                'vehicle_code': code,
                'date': job['date'],
                'depot_name': depot['name'],
                'depot_address': depot['address'],
                'jobs': [entry],
                'distance_km': round(route_km, 1),
                'drive_minutes': round(route_min),
                'first_departure': entry['departure_time'],
                'return_time': return_dt.strftime('%H:%M') if return_dt else 'onbekend',
                'duty_minutes': duty_minutes,
                'duty_duration': _format_minutes(duty_minutes),
                'own_transport': True,
            })
            continue

        # Fixed fleet selection.
        km, mins, code, state = chosen
        v = state['vehicle']
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
                free_sets, total_sets = available_standard_sets(depot_code, required_arrival, available_after)
                if free_sets >= extra_sets:
                    reserve_standard_sets(depot_code, required_arrival, available_after, extra_sets)
                    job_warnings.append(
                        f"{extra_sets} extra standaard spelset(s) meenemen uit {state['depot']['name']}."
                    )
                else:
                    # A bus can physically arrive, but the material does not exist.
                    reason = (
                        f"❗ MATERIAALPROBLEEM: {extra_sets} extra standaard spelset(s) nodig; "
                        f"in {state['depot']['name']} zijn er tijdens dit tijdvak slechts {free_sets}/{total_sets} vrij."
                    )
                    plan_jobs.append({
                        **job,
                        'vehicle_code': code,
                        'travel_minutes': round(mins),
                        'travel_km': round(km, 1),
                        'departure_time': departure.strftime('%H:%M'),
                        'arrival_time': required_arrival.strftime('%H:%M'),
                        'available_time': available_after.strftime('%H:%M'),
                        'status': 'material_problem',
                        'warnings': [reason],
                        'material_problem': True,
                        'location_problem': False,
                    })
                    warnings.append(f"Ref. {job.get('reference') or '?'}: {reason}")
                    continue

        if special['casino']:
            qty = int((resource_map.get('CASINO') or {}).get('quantity') or 0)
            job_warnings.append(f"Casino-materiaal apart laden (totaal {qty} set(s) beschikbaar).")
        if special['western']:
            qty = int((resource_map.get('WESTERN_GAMES') or {}).get('quantity') or 0)
            if qty <= 0:
                job_warnings.append('❗ MATERIAALPROBLEEM: Western Games is niet standaard in een voertuig en beschikbare voorraad is nog niet vastgelegd.')
            else:
                job_warnings.append(f"Western Games apart laden ({qty} set(s) beschikbaar).")

        entry = {
            **job,
            'vehicle_code': code,
            'travel_minutes': round(mins),
            'travel_km': round(km, 1),
            'departure_time': departure.strftime('%H:%M'),
            'arrival_time': required_arrival.strftime('%H:%M'),
            'available_time': available_after.strftime('%H:%M'),
            'status': 'planned',
            'warnings': job_warnings,
            'material_problem': any('MATERIAALPROBLEEM' in w for w in job_warnings),
            'location_problem': False,
            'own_transport': False,
        }
        plan_jobs.append(entry)
        state['jobs'].append(entry)
        state['location'] = location
        state['available_at'] = available_after

    # Give every job a stable form index. The grouped service view uses this
    # index so vehicle/timing edits can be posted back and recalculated.
    for form_index, item in enumerate(plan_jobs):
        item['form_index'] = form_index

    vehicle_routes = []
    total_km = 0.0
    total_drive = 0.0
    for (code, date_text), state in states.items():
        if not state['jobs']:
            continue
        route_jobs = state['jobs']
        route_km = sum(float(j.get('travel_km') or 0) for j in route_jobs)
        route_min = sum(float(j.get('travel_minutes') or 0) for j in route_jobs)
        first = route_jobs[0]
        last = route_jobs[-1]
        ret_min, ret_km = travel(last['location_text'], state['depot']['address'])
        first_arrival_dt = _dt(first['date'], first['start']) - timedelta(minutes=int(first.get('setup_minutes', 30) or 0))
        duty_start_dt = first_arrival_dt - timedelta(minutes=float(first.get('travel_minutes') or 0))
        last_available_dt = _dt(last['date'], last['end']) + timedelta(minutes=int(last.get('cleanup_minutes', 30) or 0))
        return_dt = None
        if ret_min is not None:
            route_min += ret_min
            route_km += ret_km
            return_dt = last_available_dt + timedelta(minutes=ret_min)
            return_text = return_dt.strftime('%H:%M')
        else:
            return_text = 'onbekend'
        duty_minutes = round((return_dt - duty_start_dt).total_seconds() / 60) if return_dt else None
        total_km += route_km
        total_drive += route_min
        vehicle_routes.append({
            'vehicle_code': code,
            'date': date_text,
            'depot_name': state['depot']['name'],
            'depot_address': state['depot']['address'],
            'jobs': route_jobs,
            'job_count': len(route_jobs),
            'distance_km': round(route_km, 1),
            'drive_minutes': round(route_min),
            'first_departure': duty_start_dt.strftime('%H:%M'),
            'return_time': return_text,
            'duty_minutes': duty_minutes,
            'duty_duration': _format_minutes(duty_minutes),
            'own_transport': False,
        })

    for route in own_transport_routes:
        total_km += route['distance_km']
        total_drive += route['drive_minutes']
        vehicle_routes.append(route)

    vehicle_routes.sort(key=lambda r: (r.get('date', ''), r.get('first_departure', '99:99'), r.get('vehicle_code', '')))
    for service_number, route in enumerate(vehicle_routes, start=1):
        route['service_number'] = service_number

    unplanned_jobs = [j for j in plan_jobs if j.get('status') in {'unplanned', 'material_problem', 'location_problem'}]

    return {
        'jobs': plan_jobs,
        'vehicle_routes': vehicle_routes,
        'unplanned_jobs': unplanned_jobs,
        'vehicles': selectable_vehicles,
        'warnings': warnings,
        'total_distance_km': round(total_km, 1),
        'total_drive_minutes': round(total_drive),
        'unplanned_count': sum(1 for j in plan_jobs if j.get('status') in {'unplanned', 'material_problem', 'location_problem'}),
        'material_problem_count': sum(1 for j in plan_jobs if j.get('material_problem')),
        'location_problem_count': sum(1 for j in plan_jobs if j.get('location_problem')),
        'resolved_location_count': sum(1 for t in node_texts if resolved.get(t, {}).get('lat') is not None),
    }
