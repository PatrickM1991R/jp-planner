import re
from datetime import datetime

DAY_PREFIX = {0:'Ma',1:'Di',2:'Wo',3:'Do',4:'Vr',5:'Za',6:'Zo'}


def _norm(text):
    return re.sub(r'\s+', ' ', (text or '')).strip().casefold()


def _dt(date_text, time_text):
    return datetime.fromisoformat(f"{date_text}T{time_text}:00")


def _week_mode(date_text):
    week = datetime.fromisoformat(date_text).isocalendar().week
    return 'EVEN' if week % 2 == 0 else 'ONEVEN'


def _profile(employee, date_text):
    profiles = employee.get('availability') or {}
    parity = _week_mode(date_text)
    if parity in profiles:
        return profiles[parity]
    return profiles.get('', {})


def _availability_level(employee, date_text, start_dt, end_dt):
    profile = _profile(employee, date_text)
    prefix = DAY_PREFIX[start_dt.weekday()]
    slots = []
    split = start_dt.replace(hour=17, minute=0, second=0, microsecond=0)
    if start_dt < split:
        slots.append(f'{prefix} -17')
    if end_dt > split:
        slots.append(f'{prefix} >17')
    if not slots:
        slots.append(f'{prefix} >17' if start_dt.hour >= 17 else f'{prefix} -17')
    values = [profile.get(s, 'onbekend') for s in slots]
    if all(v == 'beschikbaar' for v in values):
        return 'beschikbaar'
    if values and all(v in {'beschikbaar','overleg'} for v in values) and any(v == 'overleg' for v in values):
        return 'overleg'
    return 'niet'


def _activity_parts(activity):
    # The planner stores combinations with ' + '. Keep the individual game names.
    return [p.strip() for p in re.split(r'\s*\+\s*', activity or '') if p.strip()]


def _skill_yes(employee, activity):
    skills = employee.get('skills') or {}
    norm_skills = {_norm(k): _norm(v) for k, v in skills.items()}
    aliases = {
        'alleskunner':'de alleskunner',
        'de alleskunner':'de alleskunner',
        'archery attack':'archery tag',
        'gekke bingo':'crazy bingo',
        'mtwi':'minute to win it',
    }
    for part in _activity_parts(activity):
        key = aliases.get(_norm(part), _norm(part))
        value = norm_skills.get(key)
        if value not in {'ja','yes','j','1','true','jaj'}:
            return False
    return True


def _is_yes(value):
    return _norm(value) in {'ja','yes','j','1','true','automaat'}


def _busy_conflict(intervals, start_dt, end_dt):
    return any(not (end_dt <= s or start_dt >= e) for s, e, _ in intervals)


def assign_staff_to_plan(plan, employees, manual_overrides=None):
    """Add automatic named staff assignments to an already-built logistics plan.

    Availability is checked against the actual service segment for the employee:
    travel into the job + setup + game + cleanup. For the final job of a vehicle
    duty, the return drive to the depot is also included.
    """
    manual_overrides = manual_overrides or {}
    active = [dict(e) for e in employees if e.get('active')]
    by_name = {_norm(e['name']): e for e in active}
    busy = {e['id']: [] for e in active}
    last_vehicle = {}
    warnings = list(plan.get('warnings') or [])
    staff_problem_count = 0

    # Give each job its duty-aware busy start/end.
    timing = {}
    for route in plan.get('vehicle_routes', []):
        jobs = route.get('jobs') or []
        for pos, job in enumerate(jobs):
            start_text = job.get('departure_time') or job.get('arrival_time') or job.get('start')
            end_text = job.get('available_time') or job.get('end')
            start_dt = _dt(job['date'], start_text)
            end_dt = _dt(job['date'], end_text)
            if pos == len(jobs) - 1 and route.get('return_time') and route.get('return_time') != 'onbekend':
                end_dt = _dt(job['date'], route['return_time'])
            timing[str(job.get('reference') or job.get('form_index'))] = (start_dt, end_dt, route.get('vehicle_code',''))

    # Unplanned jobs still need staff suggestions based on their work block.
    for job in plan.get('jobs', []):
        key = str(job.get('reference') or job.get('form_index'))
        if key not in timing:
            start_dt = _dt(job['date'], job.get('arrival_time') or job['start'])
            end_dt = _dt(job['date'], job.get('available_time') or job['end'])
            timing[key] = (start_dt, end_dt, job.get('vehicle_code',''))

    for job in sorted(plan.get('jobs', []), key=lambda j: (j.get('date',''), j.get('departure_time') or j.get('arrival_time') or j.get('start',''))):
        ref = str(job.get('reference') or job.get('form_index'))
        start_dt, end_dt, vehicle_code = timing[ref]
        required = max(1, int(job.get('staff_required') or 1))
        manual_names = [n for n in manual_overrides.get(ref, []) if n]
        selected = []
        job_warnings = []

        # Manual choices are authoritative; warn rather than silently replacing them.
        if manual_names:
            for name in manual_names[:required]:
                employee = by_name.get(_norm(name))
                if not employee:
                    job_warnings.append(f"Handmatige medewerker '{name}' bestaat niet (meer) in het personeelsbestand.")
                    continue
                if employee in selected:
                    continue
                if not _skill_yes(employee, job.get('activity','')):
                    job_warnings.append(f"⚠️ {employee['name']} heeft het spel {job.get('activity')} niet als vaardigheid Ja staan.")
                level = _availability_level(employee, job['date'], start_dt, end_dt)
                if level == 'niet':
                    job_warnings.append(f"⚠️ {employee['name']} staat niet beschikbaar voor deze diensttijd.")
                if _busy_conflict(busy[employee['id']], start_dt, end_dt):
                    job_warnings.append(f"⚠️ {employee['name']} heeft een overlappende dienst.")
                selected.append(employee)

        # Fill every unpinned staff slot automatically. This means the user may
        # manually fix only one person and let JP Planner choose the rest.
        if len(selected) < required:
            candidates = []
            selected_ids = {e['id'] for e in selected}
            for e in active:
                if e['id'] in selected_ids:
                    continue
                if not _skill_yes(e, job.get('activity','')):
                    continue
                level = _availability_level(e, job['date'], start_dt, end_dt)
                if level == 'niet':
                    continue
                if _busy_conflict(busy[e['id']], start_dt, end_dt):
                    continue
                score = 100 if level == 'beschikbaar' else 50
                if last_vehicle.get(e['id']) == vehicle_code and vehicle_code:
                    score += 30
                if _is_yes(e.get('driving_license')):
                    score += 5
                if _is_yes(e.get('own_transport')):
                    score += 2
                candidates.append((score, e, level))
            candidates.sort(key=lambda x: (-x[0], x[1]['name'].casefold()))
            needed = required - len(selected)
            auto_picks = candidates[:needed]
            selected.extend(e for _, e, _ in auto_picks)
            for _, e, level in auto_picks:
                if level == 'overleg':
                    job_warnings.append(f"⚠️ {e['name']} is volgens het weekrooster alleen 'soms / in overleg' beschikbaar.")

        # Vehicle-specific staff constraints.
        vehicle_text = str(vehicle_code or '')
        own_transport = vehicle_text.startswith('Eigen vervoer + spelset')
        employee_car = vehicle_text.startswith('Extra auto medewerker')
        rental_car = vehicle_text.startswith('Extra huurauto')
        if selected:
            if (own_transport or employee_car) and not any(_is_yes(e.get('own_transport')) for e in selected):
                job_warnings.append('❗ PERSONEELSPROBLEEM: bij deze vervoerskeuze heeft geen toegewezen medewerker Eigen vervoer = Ja.')
            if rental_car and not any(_is_yes(e.get('driving_license')) for e in selected):
                job_warnings.append('❗ PERSONEELSPROBLEEM: voor de huurauto heeft geen toegewezen medewerker een rijbewijs geregistreerd.')
            if vehicle_text and not own_transport and not employee_car and not rental_car and not any(_is_yes(e.get('driving_license')) for e in selected):
                job_warnings.append('❗ PERSONEELSPROBLEEM: voor deze bus heeft geen toegewezen medewerker een rijbewijs geregistreerd.')

        if len(selected) < required:
            job_warnings.append(f"❗ PERSONEELSPROBLEEM: {required} medewerker(s) nodig, maar slechts {len(selected)} passend ingepland.")

        staff_problem = any('PERSONEELSPROBLEEM' in w for w in job_warnings) or len(selected) < required
        if staff_problem:
            staff_problem_count += 1
            warnings.append(f"Ref. {job.get('reference') or '?'}: " + '; '.join(job_warnings))

        job['staff_names'] = [e['name'] for e in selected]
        job['staff_warnings'] = job_warnings
        job['staff_problem'] = staff_problem
        job['staff_manual'] = bool(manual_names)

        for e in selected:
            busy[e['id']].append((start_dt, end_dt, ref))
            last_vehicle[e['id']] = vehicle_code

    # Route summary: union of all named staff used on that service.
    for route in plan.get('vehicle_routes', []):
        seen = []
        for job in route.get('jobs', []):
            for name in job.get('staff_names', []):
                if name not in seen:
                    seen.append(name)
        route['staff_names'] = seen

    plan['employees'] = active
    plan['staff_problem_count'] = staff_problem_count
    plan['warnings'] = warnings
    return plan
