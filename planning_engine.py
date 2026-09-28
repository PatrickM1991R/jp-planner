import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

from location_service import LocationService
from ors_client import ORSClient

OWN_TRANSPORT_PREFIX = 'Eigen vervoer + spelset'
RENTAL_CAR_PREFIX = 'Extra huurauto'
EMPLOYEE_CAR_PREFIX = 'Extra auto medewerker'

# Match Smart Event Manager activity names to the editable material catalog.
ACTIVITY_RESOURCE_ALIASES = [
    ('expeditie robinson', 'EXPEDITIE_ROBINSON'),
    ('minute to win it', 'MINUTE_TO_WIN_IT'),
    ('ik hou van holland', 'IK_HOU_VAN_HOLLAND'),
    ('alles mag vandaag', 'ALLES_MAG_VANDAAG'),
    ('highland games', 'HIGHLAND_GAMES'),
    ('western games', 'WESTERN_GAMES'),
    ('western game', 'WESTERN_GAMES'),
    ('western avond', 'WESTERN_GAMES'),
    ('de alleskunner', 'ALLESKUNNER'),
    ('alleskunner', 'ALLESKUNNER'),
    ('crazy bingo', 'CRAZY_BINGO'),
    ('gekke bingo', 'CRAZY_BINGO'),
    ('boogschieten', 'BOOGSCHIETEN'),
    ('moordspel', 'MOORDSPEL'),
    ('pubquiz', 'PUBQUIZ'),
    ('casino avond', 'CASINO'),
    ('casino night', 'CASINO'),
    ('casino', 'CASINO'),
    ('vr game la casa de papel', 'VR_GAME'),
    ('vr game la casa', 'VR_GAME'),
    ('vr game', 'VR_GAME'),
    ('la casa de papel', 'VR_GAME'),
    ('hunted', 'HUNTED'),
]



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


def _resource_code(activity):
    low = _norm(activity)
    for alias, code in sorted(ACTIVITY_RESOURCE_ALIASES, key=lambda x: len(x[0]), reverse=True):
        if alias in low:
            return code
    return ''


def _own_code(depot):
    return f"{OWN_TRANSPORT_PREFIX} {depot['name']}"


def _is_own_transport(code):
    return bool(code) and code.startswith(OWN_TRANSPORT_PREFIX)


def _is_rental_car(code):
    return bool(code) and code.startswith(RENTAL_CAR_PREFIX)


def _is_employee_car(code):
    return bool(code) and code.startswith(EMPLOYEE_CAR_PREFIX)


def _is_flexible_transport(code):
    return _is_own_transport(code) or _is_rental_car(code) or _is_employee_car(code)


def _rental_code(depot):
    return f"{RENTAL_CAR_PREFIX} - {depot['name']}"


def _employee_car_code(depot):
    return f"{EMPLOYEE_CAR_PREFIX} - {depot['name']}"


def _resolve_many(texts):
    unique = list(dict.fromkeys(t for t in texts if t))
    out = {}

    def resolve_one(text):
        try:
            item = LocationService().resolve(text, use_cache=True)
            if item.get('lat') is not None and item.get('lon') is not None:
                return text, item
            return text, {'status': item.get('status', 'unresolved'), 'query': text, 'error': item.get('error', '')}
        except Exception as exc:
            return text, {'status': 'unresolved', 'query': text, 'error': str(exc)}

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(resolve_one, text) for text in unique]
        for future in as_completed(futures):
            text, item = future.result()
            out[text] = item
    return out


def build_logistics_plan(jobs, depots, vehicles, stock, resources, vehicle_materials=None,
                         overrides=None, depot_overrides=None):
    """Build services using editable vehicle material and depot stock data.

    v10 rules:
    - A vehicle may contain a configurable capacity per game.
    - Missing/extra material is picked up from the service start depot when stock exists.
    - Only insufficient stock is a material problem.
    - A service can be recalculated from a manually selected depot.
    """
    overrides = overrides or {}
    depot_overrides = depot_overrides or {}
    vehicle_materials = vehicle_materials or []

    active_vehicles = [dict(v) for v in vehicles if v.get('active')]
    depot_map = {d['code']: dict(d) for d in depots if d.get('active', True)}
    stock_map = {(s['depot_code'], s['resource_code']): dict(s) for s in stock}
    resource_map = {r['resource_code']: dict(r) for r in resources}
    vehicle_material_map = {
        (m['vehicle_code'], m['resource_code']): dict(m)
        for m in vehicle_materials if m.get('active', True)
    }

    own_transport_options = []
    extra_transport_options = []
    for depot in depot_map.values():
        own_transport_options.append({
            'code': _own_code(depot), 'depot_code': depot['code'], 'vehicle_type': 'eigen vervoer',
            'active': True, 'notes': f"Eigen vervoer; materiaal ophalen uit voorraad {depot['name']}"
        })
        extra_transport_options.extend([
            {'code': _rental_code(depot), 'depot_code': depot['code'], 'vehicle_type': 'huurauto',
             'active': True, 'notes': f"Extra huurauto; materiaal ophalen uit voorraad {depot['name']}"},
            {'code': _employee_car_code(depot), 'depot_code': depot['code'], 'vehicle_type': 'medewerker_auto',
             'active': True, 'notes': f"Extra auto medewerker; materiaal ophalen uit voorraad {depot['name']}"},
        ])
    selectable_vehicles = active_vehicles + own_transport_options + extra_transport_options

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
    durations, distances = [], []
    if coords:
        try:
            matrix = ORSClient().matrix(coords)
            durations = matrix.get('durations') or []
            distances = matrix.get('distances') or []
        except Exception as exc:
            warnings.append(f"Route-matrix kon niet worden berekend: {exc}")
    else:
        warnings.append('Geen enkele locatie kon automatisch worden opgelost. Controleer de locaties en herbereken.')

    def travel(a, b):
        if a not in idx or b not in idx:
            return None, None
        i, k = idx[a], idx[b]
        try:
            sec, meters = durations[i][k], distances[i][k]
            if sec is None or meters is None:
                return None, None
            return float(sec) / 60.0, float(meters)
        except Exception:
            return None, None

    vehicle_templates = {}
    for v in active_vehicles:
        depot = depot_map.get(v.get('depot_code'))
        if depot:
            vehicle_templates[v['code']] = {'vehicle': v, 'depot': depot}

    states = {}

    def day_state(code, date_text, forced_depot_code=''):
        template = vehicle_templates[code]
        depot = depot_map.get(forced_depot_code) if forced_depot_code else template['depot']
        if not depot:
            depot = template['depot']
        key = (code, date_text, depot['code'])
        if key not in states:
            states[key] = {
                'vehicle': template['vehicle'], 'depot': depot, 'date': date_text,
                'location': depot['address'], 'available_at': None, 'jobs': [],
            }
        return states[key]

    # Reserve stock per depot + game only while job material is away.
    stock_usage = {}

    def stock_info(depot_code, resource_code, start, end):
        row = stock_map.get((depot_code, resource_code))
        if not row:
            return 0, 0, 0
        qty = max(0, int(row.get('quantity') or 0))
        cap = max(0, int(row.get('capacity_per_set') or 0))
        overlaps = [u for u in stock_usage.get((depot_code, resource_code), [])
                    if not (end <= u['start'] or start >= u['end'])]
        used = sum(u['sets'] for u in overlaps)
        return max(0, qty - used), qty, cap

    def reserve_stock(depot_code, resource_code, start, end, sets):
        if sets <= 0:
            return
        stock_usage.setdefault((depot_code, resource_code), []).append({'start': start, 'end': end, 'sets': sets})

    def onboard_capacity(vehicle_code, resource_code):
        row = vehicle_material_map.get((vehicle_code, resource_code))
        return max(0, int((row or {}).get('capacity_persons') or 0))

    def required_from_stock(vehicle_code, depot_code, resource_code, pax, start, end):
        if not resource_code:
            return {'required_sets': 0, 'free_sets': 0, 'total_sets': 0, 'cap_per_set': 0,
                    'onboard_capacity': 0, 'extra_people': 0, 'enough': True}
        onboard = onboard_capacity(vehicle_code, resource_code) if vehicle_code else 0
        extra_people = max(0, max(1, pax) - onboard)
        if extra_people <= 0:
            return {'required_sets': 0, 'free_sets': 0, 'total_sets': 0, 'cap_per_set': 0,
                    'onboard_capacity': onboard, 'extra_people': 0, 'enough': True}
        free_sets, total_sets, cap = stock_info(depot_code, resource_code, start, end)
        if cap <= 0:
            return {'required_sets': 999999, 'free_sets': free_sets, 'total_sets': total_sets,
                    'cap_per_set': cap, 'onboard_capacity': onboard, 'extra_people': extra_people, 'enough': False}
        required_sets = math.ceil(extra_people / cap)
        return {'required_sets': required_sets, 'free_sets': free_sets, 'total_sets': total_sets,
                'cap_per_set': cap, 'onboard_capacity': onboard, 'extra_people': extra_people,
                'enough': free_sets >= required_sets}

    plan_jobs = []
    flexible_routes = []
    sorted_jobs = sorted(jobs, key=lambda j: (_dt(j['date'], j['start']), j.get('reference', '')))

    for job in sorted_jobs:
        ref = str(job.get('reference') or '')
        start_dt = _dt(job['date'], job['start'])
        end_dt = _dt(job['date'], job['end'])
        setup_minutes = max(0, int(job.get('setup_minutes', 30) or 0))
        cleanup_minutes = max(0, int(job.get('cleanup_minutes', 30) or 0))
        required_arrival = start_dt - timedelta(minutes=setup_minutes)
        available_after = end_dt + timedelta(minutes=cleanup_minutes)
        pax = _participants(job.get('participants'))
        activity = job.get('activity', '')
        resource_code = _resource_code(activity)
        resource_name = (resource_map.get(resource_code) or {}).get('resource_name') or activity
        location = job.get('location_text', '')
        override = overrides.get(ref)
        forced_depot_code = depot_overrides.get(ref) or job.get('depot_override') or ''

        location_result = resolved.get(location, {})
        if not location or location_result.get('lat') is None or location_result.get('lon') is None:
            reason = f"⚠️ LOCATIE CONTROLEREN: '{location or 'geen locatie'}' kon niet worden opgelost."
            plan_jobs.append({**job, 'vehicle_code': override or '', 'travel_minutes': None, 'travel_km': None,
                              'departure_time': '', 'arrival_time': required_arrival.strftime('%H:%M'),
                              'available_time': available_after.strftime('%H:%M'), 'status': 'location_problem',
                              'warnings': [reason], 'material_problem': False, 'location_problem': True,
                              'pickup_material_required': False, 'pickup_material_label': '',
                              'depot_override': forced_depot_code})
            warnings.append(f"Ref. {ref or '?'}: {reason}")
            continue

        # Fixed company fleet: include material feasibility in candidate selection.
        fixed_candidates = []
        material_snapshots = []
        if not _is_flexible_transport(override):
            for code in vehicle_templates:
                if override and code != override:
                    continue
                state = day_state(code, job['date'], forced_depot_code)
                mins, km = travel(state['location'], location)
                if mins is None:
                    continue
                if state['available_at'] is not None:
                    arrival = state['available_at'] + timedelta(minutes=mins)
                    if arrival > required_arrival:
                        continue
                mat = required_from_stock(code, state['depot']['code'], resource_code, pax, required_arrival, available_after)
                material_snapshots.append((state['depot'], mat))
                if not mat['enough']:
                    continue
                fixed_candidates.append((km, mins, code, state, mat))

        chosen_kind = None
        chosen = None
        if fixed_candidates:
            fixed_candidates.sort(key=lambda x: (x[0], x[1], x[2]))
            chosen_kind = 'fleet'
            chosen = fixed_candidates[0]

        # Flexible fallback from a selected/default depot.
        flex_candidates = []
        manual_flexible = _is_flexible_transport(override)
        if chosen is None:
            for depot in depot_map.values():
                if forced_depot_code and depot['code'] != forced_depot_code:
                    continue
                mat = required_from_stock('', depot['code'], resource_code, pax, required_arrival, available_after)
                material_snapshots.append((depot, mat))
                if not mat['enough']:
                    continue
                if manual_flexible:
                    if _is_own_transport(override):
                        possible = [(0, 'own', _own_code(depot))]
                    elif _is_employee_car(override):
                        possible = [(1, 'employee_car', _employee_car_code(depot))]
                    elif _is_rental_car(override):
                        possible = [(2, 'rental', _rental_code(depot))]
                    else:
                        possible = []
                elif override:
                    possible = []
                else:
                    possible = [(0, 'own', _own_code(depot)), (1, 'employee_car', _employee_car_code(depot)),
                                (2, 'rental', _rental_code(depot))]
                for priority, flex_kind, code in possible:
                    if override and code != override:
                        continue
                    mins, km = travel(depot['address'], location)
                    if mins is None:
                        continue
                    departure = required_arrival - timedelta(minutes=mins)
                    flex_candidates.append((priority, km, mins, code, depot, mat, departure, flex_kind))
            if flex_candidates:
                flex_candidates.sort(key=lambda x: (x[0], x[1], x[2], x[3]))
                chosen_kind = flex_candidates[0][7]
                chosen = flex_candidates[0]

        if chosen is None:
            # Distinguish transport shortage from true material shortage.
            material_problem = False
            if resource_code:
                enough_somewhere = any(mat.get('enough') for _, mat in material_snapshots)
                if not enough_somewhere:
                    material_problem = True
                    details = []
                    seen = set()
                    for depot, mat in material_snapshots:
                        key = depot['code']
                        if key in seen:
                            continue
                        seen.add(key)
                        details.append(f"{depot['name']}: {mat['free_sets']}/{mat['total_sets']} vrije set(s)")
                    reason = f"❗ MATERIAALPROBLEEM: onvoldoende {resource_name} voor {pax} personen. " + '; '.join(details)
                else:
                    reason = ('Geen vaste bus kan deze opdracht passend uitvoeren. Er is wel materiaal beschikbaar. '
                              'Kies Eigen vervoer, Extra auto medewerker of Extra huurauto en herbereken.')
            else:
                reason = ('Geen vaste bus kan deze opdracht op tijd bereiken. Kies Eigen vervoer, '
                          'Extra auto medewerker of Extra huurauto en herbereken.')
            if forced_depot_code:
                reason += f" Gekozen vertrekstandplaats: {depot_map.get(forced_depot_code, {}).get('name', forced_depot_code)}."
            if override:
                reason += f" Handmatige voertuigkeuze: {override}."
            status = 'material_problem' if material_problem else 'unplanned'
            plan_jobs.append({**job, 'vehicle_code': override or '', 'travel_minutes': None, 'travel_km': None,
                              'departure_time': '', 'arrival_time': required_arrival.strftime('%H:%M'),
                              'available_time': available_after.strftime('%H:%M'), 'status': status,
                              'warnings': [reason], 'material_problem': material_problem, 'location_problem': False,
                              'pickup_material_required': False, 'pickup_material_label': resource_name if resource_code else '',
                              'depot_override': forced_depot_code})
            warnings.append(f"Ref. {ref or '?'}: {reason}")
            continue

        job_warnings = []

        if chosen_kind in {'own', 'rental', 'employee_car'}:
            priority, km, mins, code, depot, mat, departure, flex_kind = chosen
            if resource_code and mat['required_sets'] > 0:
                reserve_stock(depot['code'], resource_code, required_arrival, available_after, mat['required_sets'])
                job_warnings.append(
                    f"⚠️ LET OP: materiaal pakken — {mat['required_sets']} set(s) {resource_name} uit {depot['name']} "
                    f"({depot['address']}); capaciteit {mat['required_sets'] * mat['cap_per_set']} personen."
                )
            transport_label = {'rental': 'Extra huurauto', 'employee_car': 'Extra auto medewerker', 'own': 'Eigen vervoer'}[flex_kind]
            job_warnings.append(f"{transport_label}: vertrekbasis {depot['name']} ({depot['address']}).")
            status = {'rental': 'planned_rental_car', 'employee_car': 'planned_employee_car', 'own': 'planned_own_transport'}[flex_kind]
            entry = {**job, 'vehicle_code': code, 'travel_minutes': round(mins), 'travel_km': round(km, 1),
                     'departure_time': departure.strftime('%H:%M'), 'arrival_time': required_arrival.strftime('%H:%M'),
                     'available_time': available_after.strftime('%H:%M'), 'status': status, 'warnings': job_warnings,
                     'material_problem': False, 'location_problem': False, 'own_transport': flex_kind == 'own',
                     'rental_car': flex_kind == 'rental', 'employee_car': flex_kind == 'employee_car',
                     'pickup_depot_name': depot['name'], 'pickup_depot_address': depot['address'],
                     'sets_from_stock': mat['required_sets'],
                     'pickup_material_required': bool(resource_code and mat['required_sets'] > 0),
                     'pickup_material_label': resource_name if resource_code else '', 'depot_override': depot['code']}
            plan_jobs.append(entry)
            ret_min, ret_km = travel(location, depot['address'])
            route_min = mins + (ret_min or 0)
            route_km = km + (ret_km or 0)
            return_dt = available_after + timedelta(minutes=ret_min) if ret_min is not None else None
            duty_minutes = round((return_dt - departure).total_seconds() / 60) if return_dt else None
            flexible_routes.append({
                'vehicle_code': code, 'date': job['date'], 'depot_code': depot['code'], 'depot_name': depot['name'],
                'depot_address': depot['address'], 'jobs': [entry], 'distance_km': round(route_km, 1),
                'drive_minutes': round(route_min), 'first_departure': entry['departure_time'],
                'return_time': return_dt.strftime('%H:%M') if return_dt else 'onbekend',
                'duty_minutes': duty_minutes, 'duty_duration': _format_minutes(duty_minutes),
                'own_transport': flex_kind == 'own', 'rental_car': flex_kind == 'rental',
                'employee_car': flex_kind == 'employee_car',
            })
            continue

        km, mins, code, state, mat = chosen
        departure = required_arrival - timedelta(minutes=mins) if state['available_at'] is None else state['available_at']
        if resource_code and mat['required_sets'] > 0:
            reserve_stock(state['depot']['code'], resource_code, required_arrival, available_after, mat['required_sets'])
            job_warnings.append(
                f"⚠️ LET OP: materiaal pakken — {mat['required_sets']} set(s) {resource_name} uit {state['depot']['name']} "
                f"({state['depot']['address']}); buscapaciteit {mat['onboard_capacity']} personen, "
                f"extra capaciteit {mat['required_sets'] * mat['cap_per_set']} personen."
            )

        entry = {**job, 'vehicle_code': code, 'travel_minutes': round(mins), 'travel_km': round(km, 1),
                 'departure_time': departure.strftime('%H:%M'), 'arrival_time': required_arrival.strftime('%H:%M'),
                 'available_time': available_after.strftime('%H:%M'), 'status': 'planned', 'warnings': job_warnings,
                 'material_problem': False, 'location_problem': False, 'own_transport': False,
                 'sets_from_stock': mat['required_sets'],
                 'pickup_material_required': bool(resource_code and mat['required_sets'] > 0),
                 'pickup_material_label': resource_name if resource_code else '',
                 'pickup_depot_name': state['depot']['name'], 'pickup_depot_address': state['depot']['address'],
                 'depot_override': state['depot']['code']}
        plan_jobs.append(entry)
        state['jobs'].append(entry)
        state['location'] = location
        state['available_at'] = available_after

    for form_index, item in enumerate(plan_jobs):
        item['form_index'] = form_index

    vehicle_routes = []
    total_km = 0.0
    total_drive = 0.0
    for (code, date_text, depot_code), state in states.items():
        if not state['jobs']:
            continue
        route_jobs = state['jobs']
        route_km = sum(float(j.get('travel_km') or 0) for j in route_jobs)
        route_min = sum(float(j.get('travel_minutes') or 0) for j in route_jobs)
        first, last = route_jobs[0], route_jobs[-1]
        ret_min, ret_km = travel(last['location_text'], state['depot']['address'])
        first_arrival_dt = _dt(first['date'], first['start']) - timedelta(minutes=int(first.get('setup_minutes', 30) or 0))
        duty_start_dt = first_arrival_dt - timedelta(minutes=float(first.get('travel_minutes') or 0))
        last_available_dt = _dt(last['date'], last['end']) + timedelta(minutes=int(last.get('cleanup_minutes', 30) or 0))
        return_dt = None
        if ret_min is not None:
            route_min += ret_min
            route_km += ret_km or 0
            return_dt = last_available_dt + timedelta(minutes=ret_min)
            return_text = return_dt.strftime('%H:%M')
        else:
            return_text = 'onbekend'
        duty_minutes = round((return_dt - duty_start_dt).total_seconds() / 60) if return_dt else None
        total_km += route_km
        total_drive += route_min
        vehicle_routes.append({
            'vehicle_code': code, 'date': date_text, 'depot_code': depot_code,
            'depot_name': state['depot']['name'], 'depot_address': state['depot']['address'],
            'jobs': route_jobs, 'job_count': len(route_jobs), 'distance_km': round(route_km, 1),
            'drive_minutes': round(route_min), 'first_departure': duty_start_dt.strftime('%H:%M'),
            'return_time': return_text, 'duty_minutes': duty_minutes, 'duty_duration': _format_minutes(duty_minutes),
            'own_transport': False,
        })

    for route in flexible_routes:
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
        'depots': list(depot_map.values()),
        'warnings': warnings,
        'total_distance_km': round(total_km, 1),
        'total_drive_minutes': round(total_drive),
        'unplanned_count': sum(1 for j in plan_jobs if j.get('status') in {'unplanned', 'material_problem', 'location_problem'}),
        'material_problem_count': sum(1 for j in plan_jobs if j.get('material_problem')),
        'location_problem_count': sum(1 for j in plan_jobs if j.get('location_problem')),
        'resolved_location_count': sum(1 for t in node_texts if resolved.get(t, {}).get('lat') is not None),
    }
