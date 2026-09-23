import os
import re
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

from personnel_seed import PERSONNEL_SEED, PERSONNEL_SKILLS, AVAILABILITY_SLOTS


def _norm(text):
    return re.sub(r"\s+", " ", (text or "")).strip().casefold()


def configured():
    return bool(os.getenv("DATABASE_URL"))


@contextmanager
def connection():
    url = os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL ontbreekt.")
    with psycopg.connect(url, row_factory=dict_row) as conn:
        yield conn


def ensure_schema():
    if not configured():
        return
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS location_aliases (
                    alias_key TEXT PRIMARY KEY, alias_text TEXT NOT NULL,
                    location_text TEXT NOT NULL, label TEXT, lat DOUBLE PRECISION,
                    lon DOUBLE PRECISION, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )""")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS activity_aliases (
                    alias_key TEXT PRIMARY KEY, alias_text TEXT NOT NULL,
                    canonical_activity TEXT NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )""")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS reservation_corrections (
                    reference TEXT PRIMARY KEY, activity TEXT, location_text TEXT,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )""")
            cur.execute("ALTER TABLE reservation_corrections ADD COLUMN IF NOT EXISTS staff_required INTEGER")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS depots (
                    code TEXT PRIMARY KEY, name TEXT NOT NULL, address TEXT NOT NULL,
                    active BOOLEAN NOT NULL DEFAULT TRUE
                )""")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS vehicles (
                    code TEXT PRIMARY KEY, depot_code TEXT REFERENCES depots(code),
                    vehicle_type TEXT NOT NULL DEFAULT 'bus', active BOOLEAN NOT NULL DEFAULT TRUE,
                    standard_game_set BOOLEAN NOT NULL DEFAULT FALSE,
                    game_capacity INTEGER NOT NULL DEFAULT 0, notes TEXT NOT NULL DEFAULT ''
                )""")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS depot_stock (
                    depot_code TEXT REFERENCES depots(code), resource_code TEXT,
                    resource_name TEXT NOT NULL, quantity INTEGER NOT NULL DEFAULT 0,
                    capacity_per_set INTEGER, notes TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (depot_code, resource_code)
                )""")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS global_resources (
                    resource_code TEXT PRIMARY KEY, resource_name TEXT NOT NULL,
                    quantity INTEGER NOT NULL DEFAULT 0, standard_in_vehicle BOOLEAN NOT NULL DEFAULT FALSE,
                    notes TEXT NOT NULL DEFAULT ''
                )""")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS geocode_cache (
                    location_key TEXT PRIMARY KEY, location_text TEXT NOT NULL,
                    label TEXT, lat DOUBLE PRECISION NOT NULL, lon DOUBLE PRECISION NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )""")
        conn.commit()
    seed_logistics()
    seed_personnel_defaults()


def seed_logistics():
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO depots(code,name,address) VALUES
                ('ASSEN','Assen','Beilerstraat 24, Assen'),
                ('HOLLANDSCHEVELD','Hollandscheveld','Marten Kuilerweg 47, Hollandscheveld')
                ON CONFLICT(code) DO NOTHING""")
            vehicles = [
                ('z - BUS 2 (GROENE SLEUTEL)','ASSEN','bus',True,50,''),
                ('Z- BUS 3, ZWART TRAFFIC','ASSEN','bus',True,50,''),
                ('Z-Witte Bus traffic VBV-96-K','ASSEN','bus',True,50,''),
                ('z -HVL BUS 1 (ORANJE SLEUTEL)','HOLLANDSCHEVELD','bus',True,50,''),
                ('Z- VW Polo','ASSEN','auto',False,0,'Geen standaard spelset'),
            ]
            for row in vehicles:
                cur.execute("""INSERT INTO vehicles(code,depot_code,vehicle_type,standard_game_set,game_capacity,notes)
                    VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT(code) DO NOTHING""", row)
            for depot, qty in [('ASSEN',2),('HOLLANDSCHEVELD',1)]:
                cur.execute("""INSERT INTO depot_stock(depot_code,resource_code,resource_name,quantity,capacity_per_set,notes)
                    VALUES (%s,'STANDARD_GAME_SET','Extra standaard spelset',%s,50,'Voor standaard indoor/outdoor/citygames')
                    ON CONFLICT(depot_code,resource_code) DO NOTHING""", (depot, qty))
            cur.execute("""INSERT INTO global_resources(resource_code,resource_name,quantity,standard_in_vehicle,notes)
                VALUES ('CASINO','Casino',2,FALSE,'Niet standaard in een voertuig'),
                       ('WESTERN_GAMES','Western Games',0,FALSE,'Niet standaard in een voertuig; voorraad/aantal sets nog vast te leggen')
                ON CONFLICT(resource_code) DO NOTHING""")
        conn.commit()


def get_logistics():
    ensure_schema()
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT code,name,address,active FROM depots ORDER BY code")
            depots = cur.fetchall()
            cur.execute("SELECT code,depot_code,vehicle_type,active,standard_game_set,game_capacity,notes FROM vehicles ORDER BY depot_code,code")
            vehicles = cur.fetchall()
            cur.execute("SELECT depot_code,resource_code,resource_name,quantity,capacity_per_set,notes FROM depot_stock ORDER BY depot_code,resource_code")
            stock = cur.fetchall()
            cur.execute("SELECT resource_code,resource_name,quantity,standard_in_vehicle,notes FROM global_resources ORDER BY resource_code")
            resources = cur.fetchall()
    return depots, vehicles, stock, resources


def save_vehicle(code, depot_code, vehicle_type, active, standard_game_set, game_capacity, notes):
    ensure_schema()
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""UPDATE vehicles SET depot_code=%s,vehicle_type=%s,active=%s,standard_game_set=%s,
                game_capacity=%s,notes=%s WHERE code=%s""",
                (depot_code, vehicle_type, active, standard_game_set, game_capacity, notes, code))
        conn.commit()


def preload_corrections():
    if not configured(): return {}, {}, {}
    ensure_schema()
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT alias_key, canonical_activity FROM activity_aliases")
            activities = {r['alias_key']: r['canonical_activity'] for r in cur.fetchall()}
            cur.execute("SELECT alias_key, location_text, label, lat, lon FROM location_aliases")
            locations = {r['alias_key']: r for r in cur.fetchall()}
            cur.execute("SELECT reference, activity, location_text, staff_required FROM reservation_corrections")
            reservations = {str(r['reference']): r for r in cur.fetchall()}
    return activities, locations, reservations


def save_corrections_batch(items):
    if not configured(): raise RuntimeError("DATABASE_URL ontbreekt.")
    ensure_schema()
    with connection() as conn:
        with conn.cursor() as cur:
            for item in items:
                reference=item.get('reference','').strip(); original_activity=item.get('original_activity','').strip()
                original_location_hint=item.get('original_location_hint','').strip(); activity=item.get('activity','').strip()
                location_text=item.get('location_text','').strip(); staff_required=item.get('staff_required')
                if reference:
                    cur.execute("""INSERT INTO reservation_corrections(reference,activity,location_text,staff_required,updated_at)
                        VALUES (%s,%s,%s,%s,NOW()) ON CONFLICT(reference) DO UPDATE SET activity=EXCLUDED.activity,
                        location_text=EXCLUDED.location_text,staff_required=EXCLUDED.staff_required,updated_at=NOW()""",
                        (reference,activity,location_text,staff_required))
                if original_activity and original_activity!='ONBEKEND' and activity and activity!=original_activity:
                    cur.execute("""INSERT INTO activity_aliases(alias_key,alias_text,canonical_activity,updated_at)
                        VALUES (%s,%s,%s,NOW()) ON CONFLICT(alias_key) DO UPDATE SET alias_text=EXCLUDED.alias_text,
                        canonical_activity=EXCLUDED.canonical_activity,updated_at=NOW()""", (_norm(original_activity),original_activity,activity))
                if original_location_hint and location_text:
                    cur.execute("""INSERT INTO location_aliases(alias_key,alias_text,location_text,updated_at)
                        VALUES (%s,%s,%s,NOW()) ON CONFLICT(alias_key) DO UPDATE SET alias_text=EXCLUDED.alias_text,
                        location_text=EXCLUDED.location_text,updated_at=NOW()""", (_norm(original_location_hint),original_location_hint,location_text))
        conn.commit()


def norm_key(text): return _norm(text)


def get_geocode_cache(location_text):
    """Return a persisted geocode result for an exact normalized location text."""
    if not configured() or not location_text:
        return None
    ensure_schema()
    key = _norm(location_text)
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT location_text,label,lat,lon FROM geocode_cache WHERE location_key=%s",
                (key,),
            )
            row = cur.fetchone()
    return dict(row) if row else None


def save_geocode_cache(location_text, label, lat, lon):
    """Persist a successful geocode so future planning runs do not need ORS geocoding."""
    if not configured() or not location_text or lat is None or lon is None:
        return
    ensure_schema()
    key = _norm(location_text)
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO geocode_cache(location_key,location_text,label,lat,lon,updated_at)
                   VALUES (%s,%s,%s,%s,%s,NOW())
                   ON CONFLICT(location_key) DO UPDATE SET
                     location_text=EXCLUDED.location_text,label=EXCLUDED.label,
                     lat=EXCLUDED.lat,lon=EXCLUDED.lon,updated_at=NOW()""",
                (key, location_text.strip(), label or location_text.strip(), float(lat), float(lon)),
            )
        conn.commit()


def geocode_cache_count():
    if not configured():
        return 0
    ensure_schema()
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM geocode_cache")
            row = cur.fetchone()
    return int(row['n']) if row else 0

# --- Personnel -------------------------------------------------------------

def _ensure_personnel_schema(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS employees (
            id BIGSERIAL PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            driving_license TEXT NOT NULL DEFAULT '',
            own_transport TEXT NOT NULL DEFAULT 'Onbekend',
            active BOOLEAN NOT NULL DEFAULT TRUE,
            notes TEXT NOT NULL DEFAULT '',
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS employee_availability (
            employee_id BIGINT REFERENCES employees(id) ON DELETE CASCADE,
            week_mode TEXT NOT NULL DEFAULT '',
            slot TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'onbekend',
            PRIMARY KEY(employee_id, week_mode, slot)
        )""")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS employee_skills (
            employee_id BIGINT REFERENCES employees(id) ON DELETE CASCADE,
            activity TEXT NOT NULL,
            skill_status TEXT NOT NULL DEFAULT 'Onbekend',
            PRIMARY KEY(employee_id, activity)
        )""")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS personnel_imports (
            id BIGSERIAL PRIMARY KEY,
            imported_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            filename TEXT,
            row_count INTEGER NOT NULL DEFAULT 0
        )""")


def save_personnel_import(rows, filename=''):
    ensure_schema()
    with connection() as conn:
        with conn.cursor() as cur:
            _ensure_personnel_schema(cur)
            imported_names = set()
            for item in rows:
                name = (item.get('name') or '').strip()
                if not name:
                    continue
                imported_names.add(name.casefold())
                cur.execute("""
                    INSERT INTO employees(name,driving_license,own_transport,active,notes,updated_at)
                    VALUES (%s,%s,%s,%s,%s,NOW())
                    ON CONFLICT(name) DO UPDATE SET
                        driving_license=EXCLUDED.driving_license,
                        own_transport=EXCLUDED.own_transport,
                        active=EXCLUDED.active,
                        notes=EXCLUDED.notes,
                        updated_at=NOW()
                    RETURNING id
                """, (name, item.get('driving_license',''), item.get('own_transport','Onbekend'),
                      bool(item.get('active', True)), item.get('notes','')))
                employee_id = cur.fetchone()['id']
                week_mode = (item.get('week_mode') or '').upper()
                # One imported profile replaces that employee's same week profile.
                cur.execute("DELETE FROM employee_availability WHERE employee_id=%s AND week_mode=%s", (employee_id, week_mode))
                for slot, status in (item.get('availability') or {}).items():
                    cur.execute("""
                        INSERT INTO employee_availability(employee_id,week_mode,slot,status)
                        VALUES (%s,%s,%s,%s)
                    """, (employee_id, week_mode, slot, status))
                # Skills are employee-level. A later row (e.g. EVEN/ONEVEN) safely upserts the same values.
                for activity, status in (item.get('skills') or {}).items():
                    cur.execute("""
                        INSERT INTO employee_skills(employee_id,activity,skill_status)
                        VALUES (%s,%s,%s)
                        ON CONFLICT(employee_id,activity) DO UPDATE SET skill_status=EXCLUDED.skill_status
                    """, (employee_id, activity, status))
            cur.execute("INSERT INTO personnel_imports(filename,row_count) VALUES (%s,%s)", (filename, len(rows)))
        conn.commit()
    return len(imported_names)



def seed_personnel_defaults():
    """Seed the approved personnel register only when the personnel table is empty.

    After the first seed, PostgreSQL is authoritative. Manual edits and later Excel
    imports therefore survive deployments and are never overwritten by the code.
    """
    if not configured():
        return
    with connection() as conn:
        with conn.cursor() as cur:
            _ensure_personnel_schema(cur)
            cur.execute("SELECT COUNT(*) AS n FROM employees")
            row = cur.fetchone()
            if row and int(row['n']) > 0:
                return
            for item in PERSONNEL_SEED:
                name = (item.get('name') or '').strip()
                if not name:
                    continue
                cur.execute("""
                    INSERT INTO employees(name,driving_license,own_transport,active,notes,updated_at)
                    VALUES (%s,%s,%s,%s,%s,NOW()) RETURNING id
                """, (name, item.get('driving_license',''), item.get('own_transport','Onbekend'),
                      bool(item.get('active', True)), item.get('notes','')))
                employee_id = cur.fetchone()['id']
                week_mode = (item.get('week_mode') or '').upper()
                for slot, status in (item.get('availability') or {}).items():
                    cur.execute("""
                        INSERT INTO employee_availability(employee_id,week_mode,slot,status)
                        VALUES (%s,%s,%s,%s)
                    """, (employee_id, week_mode, slot, status))
                for activity, status in (item.get('skills') or {}).items():
                    cur.execute("""
                        INSERT INTO employee_skills(employee_id,activity,skill_status)
                        VALUES (%s,%s,%s)
                    """, (employee_id, activity, status))
            cur.execute("INSERT INTO personnel_imports(filename,row_count) VALUES (%s,%s)",
                        ('Standaard personeelsregister', len(PERSONNEL_SEED)))
        conn.commit()


def personnel_reference_data():
    """Return stable values needed by the direct personnel editor."""
    return {
        'skills': list(PERSONNEL_SKILLS),
        'slots': list(AVAILABILITY_SLOTS),
        'week_modes': ['', 'EVEN', 'ONEVEN'],
        'availability_choices': [
            ('beschikbaar', '✓ Beschikbaar'),
            ('overleg', '~ Soms / in overleg'),
            ('niet', '✕ Niet beschikbaar'),
            ('onbekend', '? Onbekend'),
        ],
        'skill_choices': ['Ja', 'Nee', 'Onbekend'],
    }


def add_personnel_employee(name, driving_license='Onbekend', own_transport='Onbekend', notes=''):
    name = (name or '').strip()
    if not name:
        raise ValueError('Naam ontbreekt.')
    ensure_schema()
    with connection() as conn:
        with conn.cursor() as cur:
            _ensure_personnel_schema(cur)
            cur.execute("""
                INSERT INTO employees(name,driving_license,own_transport,active,notes,updated_at)
                VALUES (%s,%s,%s,TRUE,%s,NOW())
                ON CONFLICT(name) DO NOTHING
                RETURNING id
            """, (name, driving_license or 'Onbekend', own_transport or 'Onbekend', notes or ''))
            created = cur.fetchone()
            if not created:
                raise ValueError('Deze medewerker bestaat al.')
            employee_id = created['id']
            for slot in AVAILABILITY_SLOTS:
                cur.execute("""INSERT INTO employee_availability(employee_id,week_mode,slot,status)
                    VALUES (%s,'',%s,'onbekend')""", (employee_id, slot))
            for activity in PERSONNEL_SKILLS:
                cur.execute("""INSERT INTO employee_skills(employee_id,activity,skill_status)
                    VALUES (%s,%s,'Onbekend')""", (employee_id, activity))
        conn.commit()
    return employee_id


def save_personnel_employee(employee_id, name, driving_license, own_transport, active, notes,
                            enabled_week_modes, availability_by_mode, skills):
    ensure_schema()
    employee_id = int(employee_id)
    name = (name or '').strip()
    if not name:
        raise ValueError('Naam ontbreekt.')
    enabled = set(enabled_week_modes or [])
    if not enabled:
        enabled = {''}
    with connection() as conn:
        with conn.cursor() as cur:
            _ensure_personnel_schema(cur)
            cur.execute("""
                UPDATE employees SET name=%s,driving_license=%s,own_transport=%s,
                    active=%s,notes=%s,updated_at=NOW() WHERE id=%s
            """, (name, driving_license or 'Onbekend', own_transport or 'Onbekend',
                  bool(active), notes or '', employee_id))
            if cur.rowcount != 1:
                raise ValueError('Medewerker niet gevonden.')
            cur.execute("DELETE FROM employee_availability WHERE employee_id=%s", (employee_id,))
            for mode in ['', 'EVEN', 'ONEVEN']:
                if mode not in enabled:
                    continue
                mode_values = availability_by_mode.get(mode, {})
                for slot in AVAILABILITY_SLOTS:
                    status = mode_values.get(slot, 'onbekend')
                    if status not in {'beschikbaar','overleg','niet','onbekend'}:
                        status = 'onbekend'
                    cur.execute("""
                        INSERT INTO employee_availability(employee_id,week_mode,slot,status)
                        VALUES (%s,%s,%s,%s)
                    """, (employee_id, mode, slot, status))
            cur.execute("DELETE FROM employee_skills WHERE employee_id=%s", (employee_id,))
            for activity in PERSONNEL_SKILLS:
                status = skills.get(activity, 'Onbekend')
                if status not in {'Ja','Nee','Onbekend'}:
                    status = 'Onbekend'
                cur.execute("""
                    INSERT INTO employee_skills(employee_id,activity,skill_status)
                    VALUES (%s,%s,%s)
                """, (employee_id, activity, status))
        conn.commit()


def get_personnel():
    ensure_schema()
    with connection() as conn:
        with conn.cursor() as cur:
            _ensure_personnel_schema(cur)
            cur.execute("SELECT id,name,driving_license,own_transport,active,notes,updated_at FROM employees ORDER BY name")
            employees = [dict(r) for r in cur.fetchall()]
            cur.execute("SELECT employee_id,week_mode,slot,status FROM employee_availability ORDER BY employee_id,week_mode,slot")
            availability = [dict(r) for r in cur.fetchall()]
            cur.execute("SELECT employee_id,activity,skill_status FROM employee_skills ORDER BY employee_id,activity")
            skills = [dict(r) for r in cur.fetchall()]
            cur.execute("SELECT imported_at,filename,row_count FROM personnel_imports ORDER BY imported_at DESC LIMIT 1")
            last_import = cur.fetchone()
    avail_map = {}
    for r in availability:
        avail_map.setdefault(r['employee_id'], {}).setdefault(r['week_mode'], {})[r['slot']] = r['status']
    skill_map = {}
    for r in skills:
        skill_map.setdefault(r['employee_id'], {})[r['activity']] = r['skill_status']
    for e in employees:
        e['availability'] = avail_map.get(e['id'], {})
        e['skills'] = skill_map.get(e['id'], {})
    return employees, (dict(last_import) if last_import else None)


def personnel_count():
    if not configured():
        return 0
    ensure_schema()
    with connection() as conn:
        with conn.cursor() as cur:
            _ensure_personnel_schema(cur)
            cur.execute("SELECT COUNT(*) AS n FROM employees WHERE active=TRUE")
            row = cur.fetchone()
    return int(row['n']) if row else 0
