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



POLO_FORBIDDEN_RESOURCES = {'HIGHLAND_GAMES','CASINO','WESTERN_GAMES','BOOGSCHIETEN','EXPEDITIE_ROBINSON'}
RUSH_WINDOWS = ((7, 0, 9, 0), (16, 0, 18, 30))
RUSH_NODE_SURCHARGE_MINUTES = 10

def _is_vw_polo(code):
    low = _norm(code)
    return 'vw polo' in low or 'volkswagen polo' in low

def _vehicle_allowed_for_resource(vehicle_code, resource_code):
    return not (_is_vw_polo(vehicle_code) and resource_code in POLO_FORBIDDEN_RESOURCES)

def _in_rush(dt):
    minute = dt.hour * 60 + dt.minute
    return (7*60 <= minute < 9*60) or (16*60 <= minute < 18*60+30)

def _rush_adjust(base_minutes, arrival_dt):
    """Add JP rush allowance for a route leg ending in a rush window.

    A planner route transition is treated as one route node (knooppunt).
    Each such node adds 10 minutes in the agreed rush windows.
    """
    if base_minutes is None:
        return None, 0
    surcharge = RUSH_NODE_SURCHARGE_MINUTES if _in_rush(arrival_dt) else 0
    return base_minutes + surcharge, surcharge

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
                         overrides=None, depot_overrides=None, multi_vehicle_overrides=None):
    """Build services using editable vehicle material and depot stock data.

    v10 rules:
    - A vehicle may contain a configurable capacity per game.
    - Missing/extra material is picked up from the service start depot when stock exists.
    - Only insufficient stock is a material problem.
    - A service can be recalculated from a manually selected depot.
    """
    overrides = overrides or {}
    depot_overrides = depot_overrides or {}
    multi_vehicle_overrides = multi_vehicle_overrides or {}
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
                'location': depot['address'], 'location_label': depot['name'], 'available_at': None, 'jobs': [],
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
        job_key = str(job.get('reference') or job.get('form_key') or '')
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
        override = overrides.get(job_key)
        forced_depot_code = depot_overrides.get(job_key) or job.get('depot_override') or ''

        location_result = resolved.get(location, {})
        if not location or location_result.get('lat') is None or location_result.get('lon') is None:
            reason = f"⚠️ LOCATIE CONTROLEREN: '{location or 'geen locatie'}' kon niet worden opgelost."
            plan_jobs.append({**job, 'vehicle_code': override or '', 'travel_minutes': None, 'travel_km': None,
                              'departure_time': '', 'arrival_time': required_arrival.strftime('%H:%M'),
                              'available_time': available_after.strftime('%H:%M'), 'status': 'location_problem',
                              'warnings': [reason], 'material_problem': False, 'location_problem': True,
                              'pickup_material_required': False, 'pickup_material_label': '',
                              'depot_override': forced_depot_code, 'vehicle_codes': ([override] if override else []),
                              'vehicle_travel_legs': []})
            warnings.append(f"Ref. {ref or '?'}: {reason}")
            continue

        # Fixed company fleet: include material feasibility in candidate selection.
        fixed_candidates = []
        material_snapshots = []
        if not _is_flexible_transport(override):
            for code in vehicle_templates:
                if not _vehicle_allowed_for_resource(code, resource_code):
                    continue
                if override and code != override:
                    continue
                state = day_state(code, job['date'], forced_depot_code)
                mins, km = travel(state['location'], location)
                if mins is None:
                    continue
                rush_reference = state['available_at'] if state['available_at'] is not None else required_arrival
                mins, rush_extra = _rush_adjust(mins, rush_reference)
                if state['available_at'] is not None:
                    arrival = state['available_at'] + timedelta(minutes=mins)
                    if arrival > required_arrival:
                        continue
                mat = required_from_stock(code, state['depot']['code'], resource_code, pax, required_arrival, available_after)
                material_snapshots.append((state['depot'], mat))
                if not mat['enough']:
                    continue
                fixed_candidates.append((km, mins, code, state, mat, state.get('location'), state.get('location_label')))

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
                    mins, rush_extra = _rush_adjust(mins, required_arrival)
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
                              'depot_override': forced_depot_code, 'vehicle_codes': ([override] if override else []),
                              'vehicle_travel_legs': []})
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
                     'pickup_material_label': resource_name if resource_code else '', 'depot_override': depot['code'],
                     'travel_from_address': depot['address'], 'travel_from_label': depot['name'],
                     'vehicle_codes': [code], 'vehicle_travel_legs': []}
            plan_jobs.append(entry)
            ret_min, ret_km = travel(location, depot['address'])
            if ret_min is not None:
                ret_min, return_rush_extra = _rush_adjust(ret_min, available_after)
            else:
                return_rush_extra = 0
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

        km, mins, code, state, mat, travel_from_address, travel_from_label = chosen
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
                 'depot_override': state['depot']['code'],
                 'travel_from_address': travel_from_address, 'travel_from_label': travel_from_label,
                 'vehicle_codes': [code], 'vehicle_travel_legs': []}
        plan_jobs.append(entry)
        state['jobs'].append(entry)
        state['location'] = location
        state['location_label'] = f"{activity} · {location}"
        state['available_at'] = available_after

    # v12.4: multiple vehicles may be attached to one assignment.
    # Rebuild the travel chain per selected vehicle so the route source is explicit:
    # depot -> first booking -> next booking -> ... . This also works when the same
    # vehicle is primary on one job and an extra vehicle on the next job.
    selectable_map = {str(v.get('code')): dict(v) for v in selectable_vehicles}

    # First determine all selected vehicles for every job.
    for item in plan_jobs:
        key = str(item.get('reference') or item.get('form_key') or '')
        primary = str(item.get('vehicle_code') or '')
        requested = list(multi_vehicle_overrides.get(key) or [])
        codes = []
        if primary:
            codes.append(primary)
        for code in requested:
            code = str(code or '').strip()
            if code and code not in codes:
                codes.append(code)
        item['vehicle_codes'] = codes
        item['vehicle_travel_legs'] = []
        item.setdefault('logistics_problem', False)

    assignments = {}
    for item in plan_jobs:
        for code in item.get('vehicle_codes') or []:
            assignments.setdefault(code, []).append(item)

    for code, assigned_jobs in assignments.items():
        vehicle = selectable_map.get(code) or {}
        default_depot_code = vehicle.get('depot_code') or ''
        # State is kept separately per date and chosen service depot.
        route_states = {}
        for item in sorted(assigned_jobs, key=lambda j: (_dt(j['date'], j['start']), str(j.get('reference') or j.get('form_key') or ''))):
            item_resource_code = _resource_code(item.get('activity',''))
            if not _vehicle_allowed_for_resource(code, item_resource_code):
                msg = f"❗ LOGISTIEKPROBLEEM: {code} mag niet worden ingezet voor {item.get('activity') or 'deze activiteit'}."
                item.setdefault('warnings', []).append(msg)
                item['logistics_problem'] = True
                item['vehicle_travel_legs'].append({
                    'vehicle_code': code, 'from_label': 'Niet toegestaan', 'from_address': '',
                    'to_label': item.get('activity') or '', 'to_address': item.get('location_text') or '',
                    'travel_minutes': None, 'rush_extra_minutes': 0, 'travel_km': None, 'departure_time': '',
                    'feasible': False, 'primary': code == item.get('vehicle_code'),
                })
                continue
            forced_depot_code = item.get('depot_override') or ''
            depot = depot_map.get(forced_depot_code) if forced_depot_code else depot_map.get(default_depot_code)
            if not depot:
                msg = f"❗ LOGISTIEKPROBLEEM: vertrekstandplaats voor voertuig {code} is niet bekend."
                item.setdefault('warnings', []).append(msg)
                item['logistics_problem'] = True
                item['vehicle_travel_legs'].append({
                    'vehicle_code': code, 'from_label': 'Vertrekpunt onbekend', 'from_address': '',
                    'to_label': item.get('activity') or '', 'to_address': item.get('location_text') or '',
                    'travel_minutes': None, 'travel_km': None, 'departure_time': '',
                    'feasible': False, 'primary': code == item.get('vehicle_code'),
                })
                continue

            state_key = (item['date'], depot['code'])
            state = route_states.setdefault(state_key, {
                'location': depot['address'],
                'location_label': depot['name'],
                'available_at': None,
            })

            mins, km = travel(state['location'], item.get('location_text') or '')
            start_dt = _dt(item['date'], item['start'])
            setup_minutes = max(0, int(item.get('setup_minutes', 30) or 0))
            cleanup_minutes = max(0, int(item.get('cleanup_minutes', 30) or 0))
            required_arrival = start_dt - timedelta(minutes=setup_minutes)
            available_after = _dt(item['date'], item['end']) + timedelta(minutes=cleanup_minutes)
            rush_reference = state['available_at'] if state['available_at'] is not None else required_arrival
            if mins is not None:
                mins, rush_extra = _rush_adjust(mins, rush_reference)
            else:
                rush_extra = 0
            feasible = mins is not None
            departure_dt = None
            if mins is not None:
                departure_dt = required_arrival - timedelta(minutes=mins) if state['available_at'] is None else state['available_at']
                if state['available_at'] is not None and state['available_at'] + timedelta(minutes=mins) > required_arrival:
                    feasible = False

            leg = {
                'vehicle_code': code,
                'from_label': state['location_label'],
                'from_address': state['location'],
                'to_label': item.get('activity') or item.get('location_text') or '',
                'to_address': item.get('location_text') or '',
                'travel_minutes': round(mins) if mins is not None else None,
                'rush_extra_minutes': rush_extra,
                'travel_km': round(km, 1) if km is not None else None,
                'departure_time': departure_dt.strftime('%H:%M') if departure_dt else '',
                'feasible': feasible,
                'primary': code == item.get('vehicle_code'),
            }
            item['vehicle_travel_legs'].append(leg)

            if leg['primary']:
                item['travel_from_address'] = leg['from_address']
                item['travel_from_label'] = leg['from_label']
                if leg['travel_minutes'] is not None:
                    item['travel_minutes'] = leg['travel_minutes']
                    item['travel_km'] = leg['travel_km']
                    item['departure_time'] = leg['departure_time']

            if not feasible:
                msg = (f"❗ LOGISTIEKPROBLEEM: {code} kan niet op tijd van "
                       f"{state['location_label']} naar {item.get('location_text') or 'de boeking'}.")
                if msg not in item.setdefault('warnings', []):
                    item['warnings'].append(msg)
                item['logistics_problem'] = True

            # Keep the manual assignment even when infeasible. The red problem remains
            # visible and can be acknowledged using the manual resolution workflow.
            state['location'] = item.get('location_text') or state['location']
            state['location_label'] = f"{item.get('activity') or 'Boeking'} · {item.get('location_text') or ''}"
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
        route_vehicle_codes = []
        for route_job in route_jobs:
            for vc in route_job.get('vehicle_codes') or ([route_job.get('vehicle_code')] if route_job.get('vehicle_code') else []):
                if vc and vc not in route_vehicle_codes:
                    route_vehicle_codes.append(vc)
        route_km = sum(float(j.get('travel_km') or 0) for j in route_jobs)
        route_min = sum(float(j.get('travel_minutes') or 0) for j in route_jobs)
        first, last = route_jobs[0], route_jobs[-1]
        ret_min, ret_km = travel(last['location_text'], state['depot']['address'])
        first_arrival_dt = _dt(first['date'], first['start']) - timedelta(minutes=int(first.get('setup_minutes', 30) or 0))
        duty_start_dt = first_arrival_dt - timedelta(minutes=float(first.get('travel_minutes') or 0))
        last_available_dt = _dt(last['date'], last['end']) + timedelta(minutes=int(last.get('cleanup_minutes', 30) or 0))
        return_dt = None
        if ret_min is not None:
            ret_min, return_rush_extra = _rush_adjust(ret_min, last_available_dt)
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
            'own_transport': False, 'vehicle_codes': route_vehicle_codes,
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
        'logistics_problem_count': sum(1 for j in plan_jobs if j.get('logistics_problem')),
        'resolved_location_count': sum(1 for t in node_texts if resolved.get(t, {}).get('lat') is not None),
    }
