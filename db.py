import os
import re
import json
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

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
                CREATE TABLE IF NOT EXISTS vehicle_materials (
                    vehicle_code TEXT REFERENCES vehicles(code) ON DELETE CASCADE,
                    resource_code TEXT REFERENCES global_resources(resource_code) ON DELETE CASCADE,
                    resource_name TEXT NOT NULL,
                    capacity_persons INTEGER NOT NULL DEFAULT 0,
                    active BOOLEAN NOT NULL DEFAULT TRUE,
                    notes TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (vehicle_code, resource_code)
                )""")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS geocode_cache (
                    location_key TEXT PRIMARY KEY, location_text TEXT NOT NULL,
                    label TEXT, lat DOUBLE PRECISION NOT NULL, lon DOUBLE PRECISION NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )""")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS system_migrations (
                    migration_key TEXT PRIMARY KEY,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )""")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS saved_plans (
                    id BIGSERIAL PRIMARY KEY,
                    title TEXT NOT NULL,
                    source_filename TEXT NOT NULL DEFAULT '',
                    start_date DATE,
                    end_date DATE,
                    status TEXT NOT NULL DEFAULT 'concept',
                    archived BOOLEAN NOT NULL DEFAULT FALSE,
                    archived_at TIMESTAMPTZ,
                    source_rows JSONB NOT NULL DEFAULT '[]'::jsonb,
                    plan_snapshot JSONB,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )""")
            cur.execute("ALTER TABLE saved_plans ADD COLUMN IF NOT EXISTS roster_status TEXT NOT NULL DEFAULT 'draft'")
            cur.execute("ALTER TABLE saved_plans ADD COLUMN IF NOT EXISTS roster_finalized_at TIMESTAMPTZ")
            cur.execute("ALTER TABLE saved_plans ADD COLUMN IF NOT EXISTS roster_reopened_at TIMESTAMPTZ")
            cur.execute("CREATE INDEX IF NOT EXISTS saved_plans_active_idx ON saved_plans(archived,start_date DESC,updated_at DESC)")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS saved_plan_issue_resolutions (
                    plan_id BIGINT NOT NULL REFERENCES saved_plans(id) ON DELETE CASCADE,
                    job_key TEXT NOT NULL,
                    issue_type TEXT NOT NULL,
                    resolution_label TEXT NOT NULL DEFAULT '',
                    note TEXT NOT NULL DEFAULT '',
                    active BOOLEAN NOT NULL DEFAULT TRUE,
                    resolved_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY(plan_id,job_key,issue_type)
                )""")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS saved_plan_versions (
                    id BIGSERIAL PRIMARY KEY,
                    plan_id BIGINT NOT NULL REFERENCES saved_plans(id) ON DELETE CASCADE,
                    version_no INTEGER NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    snapshot JSONB,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE(plan_id,version_no)
                )""")
        conn.commit()
    seed_logistics()
    seed_personnel_defaults()


MATERIAL_CATALOG = [
    ('IK_HOU_VAN_HOLLAND', 'Ik hou van Holland'),
    ('ALLESKUNNER', 'De Alleskunner'),
    ('MINUTE_TO_WIN_IT', 'Minute to Win It'),
    ('MOORDSPEL', 'Moordspel'),
    ('CRAZY_BINGO', 'Crazy Bingo'),
    ('PUBQUIZ', 'Pubquiz'),
    ('ALLES_MAG_VANDAAG', 'Alles mag vandaag'),
    ('EXPEDITIE_ROBINSON', 'Expeditie Robinson'),
    ('BOOGSCHIETEN', 'Boogschieten'),
    ('HUNTED', 'Hunted'),
    ('CASINO', 'Casino'),
    ('WESTERN_GAMES', 'Western Games'),
    ('HIGHLAND_GAMES', 'Highland Games'),
    ('VR_GAME', 'VR Game'),
]


BUS_STANDARD_CODES = {
    'IK_HOU_VAN_HOLLAND', 'ALLESKUNNER', 'MINUTE_TO_WIN_IT', 'MOORDSPEL',
    'CRAZY_BINGO', 'PUBQUIZ', 'ALLES_MAG_VANDAAG', 'EXPEDITIE_ROBINSON',
    'BOOGSCHIETEN',
}


def seed_logistics():
    """Seed only missing logistics rows; never overwrite later manual edits."""
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
                ('Z- VW Polo','ASSEN','auto',False,0,'Geen standaard spelmateriaal'),
            ]
            for row in vehicles:
                cur.execute("""INSERT INTO vehicles(code,depot_code,vehicle_type,standard_game_set,game_capacity,notes)
                    VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT(code) DO NOTHING""", row)

            for code, name in MATERIAL_CATALOG:
                cur.execute("""INSERT INTO global_resources(resource_code,resource_name,quantity,standard_in_vehicle,notes)
                    VALUES (%s,%s,0,FALSE,'') ON CONFLICT(resource_code) DO NOTHING""", (code, name))

            # Standard bus content: explicit per vehicle, 50 persons per game.
            cur.execute("SELECT code,vehicle_type FROM vehicles")
            for vehicle in cur.fetchall():
                if str(vehicle['vehicle_type']).lower() != 'bus':
                    continue
                for resource_code, resource_name in MATERIAL_CATALOG:
                    if resource_code not in BUS_STANDARD_CODES:
                        continue
                    cur.execute("""INSERT INTO vehicle_materials(vehicle_code,resource_code,resource_name,capacity_persons,active,notes)
                        VALUES (%s,%s,%s,50,TRUE,'Standaard in deze bus')
                        ON CONFLICT(vehicle_code,resource_code) DO NOTHING""",
                        (vehicle['code'], resource_code, resource_name))

            # Extra stock. These are editable defaults in the fleet screen.
            # Standard games keep the earlier rule: 2 extra sets in Assen and 1 in Hollandscheveld.
            defaults = []
            name_by_code = dict(MATERIAL_CATALOG)
            for resource_code in BUS_STANDARD_CODES:
                resource_name = name_by_code[resource_code]
                defaults.append(('ASSEN', resource_code, resource_name, 2, 50, 'Extra voorraad'))
                defaults.append(('HOLLANDSCHEVELD', resource_code, resource_name, 1, 50, 'Extra voorraad'))
            # Definitieve speciale materialen. Ze liggen NIET standaard in de bus.
            # Deze startwaarden zijn na installatie volledig bewerkbaar via Wagenpark & Logistiek.
            defaults.extend([
                ('ASSEN','HUNTED','Hunted',5,50,'Altijd pakken · 250 personen totaal'),
                ('ASSEN','CASINO','Casino',2,125,'Altijd pakken · 250 personen totaal · 2 sets'),
                ('ASSEN','WESTERN_GAMES','Western Games',1,150,'150 personen totaal · 1 set'),
                ('ASSEN','HIGHLAND_GAMES','Highland Games',2,50,'50 personen per set · 2 sets'),
                ('ASSEN','VR_GAME','VR Game',1,60,'60 personen totaal · 1 set'),
            ])
            for row in defaults:
                cur.execute("""INSERT INTO depot_stock(depot_code,resource_code,resource_name,quantity,capacity_per_set,notes)
                    VALUES (%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(depot_code,resource_code) DO NOTHING""", row)

            # Keep the historical generic standard-stock rows for compatibility,
            # but new planning prefers game-specific stock above.
            for depot, qty in [('ASSEN',2),('HOLLANDSCHEVELD',1)]:
                cur.execute("""INSERT INTO depot_stock(depot_code,resource_code,resource_name,quantity,capacity_per_set,notes)
                    VALUES (%s,'STANDARD_GAME_SET','Extra standaard spelset',%s,50,'Legacy fallback; game-specifieke voorraad heeft voorrang')
                    ON CONFLICT(depot_code,resource_code) DO NOTHING""", (depot, qty))

            # Eenmalige v10.1-correctie van de door JP Activiteiten vastgelegde startvoorraad.
            # Daarna worden handmatige wijzigingen NIET opnieuw overschreven.
            cur.execute("SELECT 1 FROM system_migrations WHERE migration_key='v10_1_material_defaults'")
            if not cur.fetchone():
                special_defaults = [
                    ('ASSEN','HUNTED','Hunted',5,50,'Altijd pakken · 250 personen totaal'),
                    ('ASSEN','CASINO','Casino',2,125,'Altijd pakken · 250 personen totaal · 2 sets'),
                    ('ASSEN','WESTERN_GAMES','Western Games',1,150,'150 personen totaal · 1 set'),
                    ('ASSEN','HIGHLAND_GAMES','Highland Games',2,50,'50 personen per set · 2 sets'),
                    ('ASSEN','VR_GAME','VR Game',1,60,'60 personen totaal · 1 set'),
                ]
                for row in special_defaults:
                    cur.execute("""INSERT INTO depot_stock(depot_code,resource_code,resource_name,quantity,capacity_per_set,notes)
                        VALUES (%s,%s,%s,%s,%s,%s)
                        ON CONFLICT(depot_code,resource_code) DO UPDATE SET
                        resource_name=EXCLUDED.resource_name,quantity=EXCLUDED.quantity,
                        capacity_per_set=EXCLUDED.capacity_per_set,notes=EXCLUDED.notes""", row)

                # Oude v10.0-specials zijn volgens de nieuwe regels geen materiaalregels meer.
                cur.execute("DELETE FROM depot_stock WHERE resource_code IN ('HIDDEN_GAMES','JONGENS_TEGEN_DE_MEISJES')")
                cur.execute("DELETE FROM vehicle_materials WHERE resource_code IN ('HIDDEN_GAMES','JONGENS_TEGEN_DE_MEISJES')")
                cur.execute("INSERT INTO system_migrations(migration_key) VALUES ('v10_1_material_defaults') ON CONFLICT DO NOTHING")
        conn.commit()


def get_logistics():
    ensure_schema()
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT code,name,address,active FROM depots ORDER BY name,code")
            depots = [dict(r) for r in cur.fetchall()]
            cur.execute("SELECT code,depot_code,vehicle_type,active,standard_game_set,game_capacity,notes FROM vehicles ORDER BY depot_code,code")
            vehicles = [dict(r) for r in cur.fetchall()]
            cur.execute("SELECT depot_code,resource_code,resource_name,quantity,capacity_per_set,notes FROM depot_stock ORDER BY depot_code,resource_name")
            stock = [dict(r) for r in cur.fetchall()]
            cur.execute("SELECT resource_code,resource_name,quantity,standard_in_vehicle,notes FROM global_resources ORDER BY resource_name")
            resources = [dict(r) for r in cur.fetchall()]
            cur.execute("SELECT vehicle_code,resource_code,resource_name,capacity_persons,active,notes FROM vehicle_materials ORDER BY vehicle_code,resource_name")
            vehicle_materials = [dict(r) for r in cur.fetchall()]
    return depots, vehicles, stock, resources, vehicle_materials


def save_depot(code, name, address, active=True):
    code = (code or '').strip().upper().replace(' ', '_')
    name = (name or '').strip()
    address = (address or '').strip()
    if not code or not name or not address:
        raise ValueError('Code, naam en adres van de standplaats zijn verplicht.')
    ensure_schema()
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO depots(code,name,address,active) VALUES (%s,%s,%s,%s)
                ON CONFLICT(code) DO UPDATE SET name=EXCLUDED.name,address=EXCLUDED.address,active=EXCLUDED.active""",
                (code, name, address, bool(active)))
        conn.commit()
    return code


def save_vehicle(code, depot_code, vehicle_type, active, standard_game_set=False, game_capacity=0, notes=''):
    code = (code or '').strip()
    if not code:
        raise ValueError('Voertuigcode ontbreekt.')
    ensure_schema()
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO vehicles(code,depot_code,vehicle_type,active,standard_game_set,game_capacity,notes)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(code) DO UPDATE SET depot_code=EXCLUDED.depot_code,vehicle_type=EXCLUDED.vehicle_type,
                active=EXCLUDED.active,standard_game_set=EXCLUDED.standard_game_set,
                game_capacity=EXCLUDED.game_capacity,notes=EXCLUDED.notes""",
                (code, depot_code, vehicle_type or 'bus', bool(active), bool(standard_game_set), int(game_capacity or 0), notes or ''))
        conn.commit()


def save_vehicle_material(vehicle_code, resource_code, capacity_persons, active=True, notes=''):
    ensure_schema()
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT resource_name FROM global_resources WHERE resource_code=%s", (resource_code,))
            resource = cur.fetchone()
            if not resource:
                raise ValueError('Onbekend materiaal/spel.')
            cur.execute("""INSERT INTO vehicle_materials(vehicle_code,resource_code,resource_name,capacity_persons,active,notes)
                VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT(vehicle_code,resource_code) DO UPDATE SET resource_name=EXCLUDED.resource_name,
                capacity_persons=EXCLUDED.capacity_persons,active=EXCLUDED.active,notes=EXCLUDED.notes""",
                (vehicle_code, resource_code, resource['resource_name'], max(0, int(capacity_persons or 0)), bool(active), notes or ''))
        conn.commit()


def save_depot_stock(depot_code, resource_code, quantity, capacity_per_set, notes=''):
    ensure_schema()
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT resource_name FROM global_resources WHERE resource_code=%s", (resource_code,))
            resource = cur.fetchone()
            if not resource:
                raise ValueError('Onbekend materiaal/spel.')
            cur.execute("""INSERT INTO depot_stock(depot_code,resource_code,resource_name,quantity,capacity_per_set,notes)
                VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT(depot_code,resource_code) DO UPDATE SET resource_name=EXCLUDED.resource_name,
                quantity=EXCLUDED.quantity,capacity_per_set=EXCLUDED.capacity_per_set,notes=EXCLUDED.notes""",
                (depot_code, resource_code, resource['resource_name'], max(0, int(quantity or 0)), max(0, int(capacity_per_set or 0)), notes or ''))
        conn.commit()



def save_depot_stock_bulk(depot_code, rows):
    """Save all editable stock rows for one depot in a single transaction."""
    depot_code = (depot_code or '').strip()
    if not depot_code:
        raise ValueError('Standplaats ontbreekt.')
    ensure_schema()
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM depots WHERE code=%s", (depot_code,))
            if not cur.fetchone():
                raise ValueError('Onbekende standplaats.')
            for row in rows:
                resource_code = (row.get('resource_code') or '').strip()
                if not resource_code:
                    continue
                cur.execute("SELECT resource_name FROM global_resources WHERE resource_code=%s", (resource_code,))
                resource = cur.fetchone()
                if not resource:
                    continue
                try:
                    quantity = max(0, int(row.get('quantity') or 0))
                except (TypeError, ValueError):
                    quantity = 0
                try:
                    capacity_per_set = max(0, int(row.get('capacity_per_set') or 0))
                except (TypeError, ValueError):
                    capacity_per_set = 0
                notes = (row.get('notes') or '').strip()
                cur.execute("""INSERT INTO depot_stock(depot_code,resource_code,resource_name,quantity,capacity_per_set,notes)
                    VALUES (%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(depot_code,resource_code) DO UPDATE SET
                    resource_name=EXCLUDED.resource_name,quantity=EXCLUDED.quantity,
                    capacity_per_set=EXCLUDED.capacity_per_set,notes=EXCLUDED.notes""",
                    (depot_code, resource_code, resource['resource_name'], quantity, capacity_per_set, notes))
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
            level INTEGER NULL,
            email TEXT NOT NULL DEFAULT '',
            password_hash TEXT NOT NULL DEFAULT '',
            portal_active BOOLEAN NOT NULL DEFAULT TRUE,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""")
    cur.execute("ALTER TABLE employees ADD COLUMN IF NOT EXISTS level INTEGER NULL")
    cur.execute("ALTER TABLE employees ADD COLUMN IF NOT EXISTS email TEXT NOT NULL DEFAULT ''")
    cur.execute("ALTER TABLE employees ADD COLUMN IF NOT EXISTS username TEXT NOT NULL DEFAULT ''")
    cur.execute("ALTER TABLE employees ADD COLUMN IF NOT EXISTS password_hash TEXT NOT NULL DEFAULT ''")
    cur.execute("ALTER TABLE employees ADD COLUMN IF NOT EXISTS portal_active BOOLEAN NOT NULL DEFAULT TRUE")
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS employees_email_unique_idx ON employees(LOWER(email)) WHERE email <> ''")
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS employees_username_unique_idx ON employees(LOWER(username)) WHERE username <> ''")
    cur.execute("""UPDATE employees SET username='willeke.terbraak',email='patrick@tossbv.nl',portal_active=TRUE,updated_at=NOW()
                   WHERE (LOWER(name)='willeke ter braak' OR LOWER(name)='willeke' OR LOWER(name) LIKE 'willeke ter braak%')""")
    cur.execute("UPDATE employees SET level=4 WHERE (LOWER(name) IN ('willeke','dennis','jorian') OR LOWER(name) LIKE 'willeke %' OR LOWER(name) LIKE 'dennis %' OR LOWER(name) LIKE 'jorian %') AND level IS NULL")
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
        CREATE TABLE IF NOT EXISTS personnel_skill_catalog (
            activity TEXT PRIMARY KEY,
            active BOOLEAN NOT NULL DEFAULT TRUE,
            sort_order INTEGER NOT NULL DEFAULT 0,
            notes TEXT NOT NULL DEFAULT '',
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""")
    # Seed only missing catalog items. Existing edits remain authoritative.
    for idx, activity in enumerate(PERSONNEL_SKILLS):
        cur.execute("""
            INSERT INTO personnel_skill_catalog(activity,active,sort_order,notes)
            VALUES (%s,TRUE,%s,'')
            ON CONFLICT(activity) DO NOTHING
        """, (activity, idx))
    cur.execute("""
        CREATE TABLE IF NOT EXISTS employee_shifts (
            id BIGSERIAL PRIMARY KEY,
            plan_id BIGINT REFERENCES saved_plans(id) ON DELETE SET NULL,
            employee_id BIGINT NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
            service_key TEXT NOT NULL,
            work_date DATE NOT NULL,
            activity_summary TEXT NOT NULL DEFAULT '',
            vehicle_code TEXT NOT NULL DEFAULT '',
            location_summary TEXT NOT NULL DEFAULT '',
            planned_start TIME NOT NULL,
            planned_end TIME NOT NULL,
            planned_end_next_day BOOLEAN NOT NULL DEFAULT FALSE,
            planned_gross_minutes INTEGER NOT NULL DEFAULT 0,
            planned_break_minutes INTEGER NOT NULL DEFAULT 0,
            planned_net_minutes INTEGER NOT NULL DEFAULT 0,
            actual_start TIME,
            actual_end TIME,
            actual_end_next_day BOOLEAN NOT NULL DEFAULT FALSE,
            actual_break_minutes INTEGER,
            actual_gross_minutes INTEGER,
            actual_net_minutes INTEGER,
            status TEXT NOT NULL DEFAULT 'scheduled',
            employee_note TEXT NOT NULL DEFAULT '',
            planner_note TEXT NOT NULL DEFAULT '',
            employee_token TEXT NOT NULL DEFAULT '',
            planner_approval_token TEXT NOT NULL DEFAULT '',
            finalized_at TIMESTAMPTZ,
            reminder_sent_at TIMESTAMPTZ,
            response_deadline TIMESTAMPTZ,
            employee_responded_at TIMESTAMPTZ,
            planner_approved_at TIMESTAMPTZ,
            auto_approved_at TIMESTAMPTZ,
            reopened_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE(plan_id, employee_id, service_key)
        )""")
    cur.execute("CREATE INDEX IF NOT EXISTS employee_shifts_employee_date_idx ON employee_shifts(employee_id,work_date)")
    cur.execute("CREATE INDEX IF NOT EXISTS employee_shifts_status_idx ON employee_shifts(status,response_deadline)")
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS employee_shifts_employee_token_idx ON employee_shifts(employee_token) WHERE employee_token <> ''")
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS employee_shifts_planner_token_idx ON employee_shifts(planner_approval_token) WHERE planner_approval_token <> ''")
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
                    cur.execute("SELECT COALESCE(MAX(sort_order),-1)+1 AS n FROM personnel_skill_catalog")
                    nr = cur.fetchone()
                    cur.execute("""
                        INSERT INTO personnel_skill_catalog(activity,active,sort_order,notes)
                        VALUES (%s,TRUE,%s,'Excel import')
                        ON CONFLICT(activity) DO NOTHING
                    """, (activity, int(nr['n']) if nr else 0))
                    cur.execute("""
                        INSERT INTO employee_skills(employee_id,activity,skill_status)
                        VALUES (%s,%s,%s)
                        ON CONFLICT(employee_id,activity) DO UPDATE SET skill_status=EXCLUDED.skill_status
                    """, (employee_id, activity, status))
            cur.execute("""UPDATE employees SET level=4,updated_at=NOW()
                WHERE (LOWER(name) IN ('willeke','dennis','jorian') OR LOWER(name) LIKE 'willeke %' OR LOWER(name) LIKE 'dennis %' OR LOWER(name) LIKE 'jorian %')
                  AND level IS NULL""")
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


def personnel_skill_catalog(include_inactive=True):
    """Return the persistent skill/activity catalog."""
    ensure_schema()
    with connection() as conn:
        with conn.cursor() as cur:
            _ensure_personnel_schema(cur)
            sql = "SELECT activity,active,sort_order,notes,updated_at FROM personnel_skill_catalog"
            if not include_inactive:
                sql += " WHERE active=TRUE"
            sql += " ORDER BY sort_order, LOWER(activity)"
            cur.execute(sql)
            return [dict(r) for r in cur.fetchall()]


def personnel_reference_data():
    """Reference values used by the direct personnel editor."""
    catalog = personnel_skill_catalog(include_inactive=True)
    return {
        'skills': [r['activity'] for r in catalog if r.get('active')],
        'skill_catalog': catalog,
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


def save_personnel_skill(activity, active=True, notes=''):
    activity = (activity or '').strip()
    if not activity:
        raise ValueError('Naam van vaardigheid/spel ontbreekt.')
    ensure_schema()
    with connection() as conn:
        with conn.cursor() as cur:
            _ensure_personnel_schema(cur)
            cur.execute("SELECT COALESCE(MAX(sort_order),-1)+1 AS n FROM personnel_skill_catalog")
            row = cur.fetchone()
            next_order = int(row['n']) if row else 0
            cur.execute("""
                INSERT INTO personnel_skill_catalog(activity,active,sort_order,notes,updated_at)
                VALUES (%s,%s,%s,%s,NOW())
                ON CONFLICT(activity) DO UPDATE SET
                    active=EXCLUDED.active,
                    notes=EXCLUDED.notes,
                    updated_at=NOW()
            """, (activity, bool(active), next_order, notes or ''))
            # New skills become visible for every employee as Onbekend.
            cur.execute("""
                INSERT INTO employee_skills(employee_id,activity,skill_status)
                SELECT id,%s,'Onbekend' FROM employees
                ON CONFLICT(employee_id,activity) DO NOTHING
            """, (activity,))
        conn.commit()
    return activity


def set_personnel_skill_active(activity, active):
    activity = (activity or '').strip()
    if not activity:
        raise ValueError('Vaardigheid ontbreekt.')
    ensure_schema()
    with connection() as conn:
        with conn.cursor() as cur:
            _ensure_personnel_schema(cur)
            cur.execute("""
                UPDATE personnel_skill_catalog
                SET active=%s,updated_at=NOW()
                WHERE activity=%s
            """, (bool(active), activity))
            if cur.rowcount != 1:
                raise ValueError('Vaardigheid niet gevonden.')
        conn.commit()

def add_personnel_employee(name, driving_license='Onbekend', own_transport='Onbekend', notes='', level='', email='', username='', password_hash='', portal_active=True):
    name = (name or '').strip()
    if not name:
        raise ValueError('Naam ontbreekt.')
    ensure_schema()
    with connection() as conn:
        with conn.cursor() as cur:
            _ensure_personnel_schema(cur)
            cur.execute("""
                INSERT INTO employees(name,driving_license,own_transport,active,notes,level,email,username,password_hash,portal_active,updated_at)
                VALUES (%s,%s,%s,TRUE,%s,%s,%s,%s,%s,%s,NOW())
                ON CONFLICT(name) DO NOTHING
                RETURNING id
            """, (name, driving_license or 'Onbekend', own_transport or 'Onbekend', notes or '', int(level) if str(level).strip() in {'0','1','2','3','4'} else None, (email or '').strip().lower(), (username or '').strip().lower(), password_hash or '', bool(portal_active)))
            created = cur.fetchone()
            if not created:
                raise ValueError('Deze medewerker bestaat al.')
            employee_id = created['id']
            for slot in AVAILABILITY_SLOTS:
                cur.execute("""INSERT INTO employee_availability(employee_id,week_mode,slot,status)
                    VALUES (%s,'',%s,'onbekend')""", (employee_id, slot))
            cur.execute("SELECT activity FROM personnel_skill_catalog WHERE active=TRUE ORDER BY sort_order,LOWER(activity)")
            for skill_row in cur.fetchall():
                cur.execute("""INSERT INTO employee_skills(employee_id,activity,skill_status)
                    VALUES (%s,%s,'Onbekend') ON CONFLICT(employee_id,activity) DO NOTHING""",
                    (employee_id, skill_row['activity']))
        conn.commit()
    return employee_id


def save_personnel_employee(employee_id, name, driving_license, own_transport, active, notes, level,
                            enabled_week_modes, availability_by_mode, skills, email='', username='', password_hash=None, portal_active=True):
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
            if password_hash is None:
                cur.execute("""
                    UPDATE employees SET name=%s,driving_license=%s,own_transport=%s,
                        active=%s,notes=%s,level=%s,email=%s,username=%s,portal_active=%s,updated_at=NOW() WHERE id=%s
                """, (name, driving_license or 'Onbekend', own_transport or 'Onbekend',
                      bool(active), notes or '', int(level) if str(level).strip() in {'0','1','2','3','4'} else None,
                      (email or '').strip().lower(), (username or '').strip().lower(), bool(portal_active), employee_id))
            else:
                cur.execute("""
                    UPDATE employees SET name=%s,driving_license=%s,own_transport=%s,
                        active=%s,notes=%s,level=%s,email=%s,username=%s,password_hash=%s,portal_active=%s,updated_at=NOW() WHERE id=%s
                """, (name, driving_license or 'Onbekend', own_transport or 'Onbekend',
                      bool(active), notes or '', int(level) if str(level).strip() in {'0','1','2','3','4'} else None,
                      (email or '').strip().lower(), (username or '').strip().lower(), password_hash or '', bool(portal_active), employee_id))
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
            # Preserve inactive catalog skills; update only currently active skills.
            cur.execute("SELECT activity FROM personnel_skill_catalog WHERE active=TRUE ORDER BY sort_order,LOWER(activity)")
            active_activities = [r['activity'] for r in cur.fetchall()]
            for activity in active_activities:
                status = skills.get(activity, 'Onbekend')
                if status not in {'Ja','Nee','Onbekend'}:
                    status = 'Onbekend'
                cur.execute("""
                    INSERT INTO employee_skills(employee_id,activity,skill_status)
                    VALUES (%s,%s,%s)
                    ON CONFLICT(employee_id,activity) DO UPDATE SET skill_status=EXCLUDED.skill_status
                """, (employee_id, activity, status))
        conn.commit()


def get_personnel():
    ensure_schema()
    with connection() as conn:
        with conn.cursor() as cur:
            _ensure_personnel_schema(cur)
            cur.execute("SELECT id,name,driving_license,own_transport,active,notes,level,email,username,portal_active,(password_hash <> '') AS has_password,updated_at FROM employees ORDER BY name")
            employees = [dict(r) for r in cur.fetchall()]
            cur.execute("SELECT employee_id,week_mode,slot,status FROM employee_availability ORDER BY employee_id,week_mode,slot")
            availability = [dict(r) for r in cur.fetchall()]
            cur.execute("""
                SELECT es.employee_id,es.activity,es.skill_status
                FROM employee_skills es
                JOIN personnel_skill_catalog pc ON pc.activity=es.activity
                WHERE pc.active=TRUE
                ORDER BY es.employee_id,pc.sort_order,LOWER(es.activity)
            """)
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


# ---------------------------------------------------------------------------
# Persistent planning archive (v12.0)
# ---------------------------------------------------------------------------

def _json_safe(value):
    """Return a JSON-serialisable copy for PostgreSQL JSONB."""
    return json.loads(json.dumps(value, default=str))


def _plan_dates(rows):
    dates=[]
    for row in rows or []:
        value=str(row.get('date') or '').strip()
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            dates.append(value)
    if not dates:
        return None, None
    return min(dates), max(dates)


def _normalise_source_filename(value):
    return (value or '').strip().lower()


def create_saved_plan(rows, source_filename=''):
    """Get or create one persistent dossier for one imported reception list.

    Re-importing the same source file for the same planning date range updates the
    existing dossier instead of creating a second planning card.
    """
    ensure_schema()
    start_date,end_date=_plan_dates(rows)
    if start_date and start_date==end_date:
        title=f'Planning {start_date}'
    elif start_date and end_date:
        title=f'Planning {start_date} t/m {end_date}'
    else:
        title='Nieuwe planning'
    source_filename=(source_filename or '').strip()
    with connection() as conn:
        with conn.cursor() as cur:
            if source_filename:
                cur.execute("""SELECT id FROM saved_plans
                    WHERE lower(trim(source_filename))=%s
                    AND start_date IS NOT DISTINCT FROM %s::date
                    AND end_date IS NOT DISTINCT FROM %s::date
                    ORDER BY updated_at DESC,id DESC LIMIT 1""",
                    (_normalise_source_filename(source_filename),start_date,end_date))
                existing=cur.fetchone()
            else:
                existing=None
            if existing:
                plan_id=int(existing['id'])
                cur.execute("""UPDATE saved_plans SET source_rows=%s,start_date=%s,end_date=%s,
                    source_filename=%s,updated_at=NOW() WHERE id=%s""",
                    (Jsonb(_json_safe(rows or [])),start_date,end_date,source_filename,plan_id))
            else:
                cur.execute("""INSERT INTO saved_plans(title,source_filename,start_date,end_date,status,source_rows)
                    VALUES (%s,%s,%s,%s,'concept',%s) RETURNING id""",
                    (title,source_filename,start_date,end_date,Jsonb(_json_safe(rows or []))))
                plan_id=cur.fetchone()['id']
        conn.commit()
    return int(plan_id)


def consolidate_duplicate_saved_plans():
    """Merge old duplicate dossier rows without discarding their snapshots/history.

    Duplicates are plans with the same non-empty source filename and identical
    planning date range. The most recently updated row remains the dossier.
    Snapshots and version history from older rows are copied into its history.
    """
    ensure_schema()
    merged=0
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT lower(trim(source_filename)) AS source_key,start_date,end_date,
                       array_agg(id ORDER BY updated_at DESC,id DESC) AS ids
                FROM saved_plans
                WHERE trim(COALESCE(source_filename,''))<>''
                GROUP BY lower(trim(source_filename)),start_date,end_date
                HAVING COUNT(*)>1""")
            groups=cur.fetchall()
            for group in groups:
                ids=list(group['ids'] or [])
                if len(ids)<2:
                    continue
                keeper=int(ids[0])
                for duplicate in [int(x) for x in ids[1:]]:
                    # Preserve every historical version first.
                    cur.execute("SELECT note,snapshot,created_at FROM saved_plan_versions WHERE plan_id=%s ORDER BY version_no",(duplicate,))
                    histories=cur.fetchall()
                    cur.execute("SELECT plan_snapshot,status,archived,archived_at FROM saved_plans WHERE id=%s",(duplicate,))
                    dup=cur.fetchone()
                    if dup and dup.get('plan_snapshot') is not None:
                        histories.append({'note':f'Overgenomen uit samengevoegde planning #{duplicate}',
                                          'snapshot':dup['plan_snapshot'],'created_at':None})
                    for hist in histories:
                        cur.execute("SELECT COALESCE(MAX(version_no),0)+1 AS n FROM saved_plan_versions WHERE plan_id=%s",(keeper,))
                        n=int(cur.fetchone()['n'])
                        cur.execute("""INSERT INTO saved_plan_versions(plan_id,version_no,note,snapshot,created_at)
                            VALUES (%s,%s,%s,%s,COALESCE(%s,NOW()))""",
                            (keeper,n,hist.get('note') or 'Samengevoegde versie',hist.get('snapshot'),hist.get('created_at')))
                    cur.execute("DELETE FROM saved_plans WHERE id=%s",(duplicate,))
                    merged += 1
        conn.commit()
    return merged


def update_saved_plan_source(plan_id, rows, title=None):
    if not plan_id:
        return
    ensure_schema()
    start_date,end_date=_plan_dates(rows)
    with connection() as conn:
        with conn.cursor() as cur:
            if title:
                cur.execute("""UPDATE saved_plans SET title=%s,start_date=%s,end_date=%s,
                    source_rows=%s,updated_at=NOW() WHERE id=%s""",
                    (title,start_date,end_date,Jsonb(_json_safe(rows or [])),int(plan_id)))
            else:
                cur.execute("""UPDATE saved_plans SET start_date=%s,end_date=%s,
                    source_rows=%s,updated_at=NOW() WHERE id=%s""",
                    (start_date,end_date,Jsonb(_json_safe(rows or [])),int(plan_id)))
        conn.commit()


def save_plan_snapshot(plan_id, plan, note='Planning herberekend'):
    """Save current planning and append an immutable history version."""
    if not plan_id:
        return
    ensure_schema()
    snapshot=_json_safe(plan or {})
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""UPDATE saved_plans SET plan_snapshot=%s,status='gepland',updated_at=NOW()
                WHERE id=%s""", (Jsonb(snapshot),int(plan_id)))
            cur.execute("SELECT COALESCE(MAX(version_no),0)+1 AS n FROM saved_plan_versions WHERE plan_id=%s", (int(plan_id),))
            version_no=int(cur.fetchone()['n'])
            cur.execute("""INSERT INTO saved_plan_versions(plan_id,version_no,note,snapshot)
                VALUES (%s,%s,%s,%s)""", (int(plan_id),version_no,note or '',Jsonb(snapshot)))
        conn.commit()


def get_saved_plan(plan_id):
    ensure_schema()
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM saved_plans WHERE id=%s", (int(plan_id),))
            row=cur.fetchone()
    return dict(row) if row else None


def list_saved_plans(archived=False, search='', limit=100):
    ensure_schema()
    search=(search or '').strip()
    params=[bool(archived)]
    where="archived=%s"
    if search:
        token=f'%{search}%'
        where += " AND (title ILIKE %s OR source_filename ILIKE %s OR source_rows::text ILIKE %s OR COALESCE(plan_snapshot::text,'') ILIKE %s)"
        params.extend([token,token,token,token])
    params.append(int(limit))
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""SELECT id,title,source_filename,start_date,end_date,status,archived,archived_at,
                created_at,updated_at,jsonb_array_length(source_rows) AS job_count
                FROM saved_plans WHERE {where}
                ORDER BY COALESCE(end_date,start_date) DESC NULLS LAST, updated_at DESC LIMIT %s""", params)
            rows=[dict(r) for r in cur.fetchall()]
    return rows


def auto_archive_saved_plans(today_iso):
    """Archive plans on day 8: planning end date + 7 days <= today."""
    ensure_schema()
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""UPDATE saved_plans
                SET archived=TRUE,archived_at=COALESCE(archived_at,NOW()),updated_at=NOW()
                WHERE archived=FALSE AND end_date IS NOT NULL
                AND (end_date + INTERVAL '7 days')::date <= %s::date""", (today_iso,))
            changed=cur.rowcount
        conn.commit()
    return changed


def set_saved_plan_archived(plan_id, archived=True):
    ensure_schema()
    with connection() as conn:
        with conn.cursor() as cur:
            if archived:
                cur.execute("""UPDATE saved_plans SET archived=TRUE,archived_at=NOW(),updated_at=NOW() WHERE id=%s""", (int(plan_id),))
            else:
                cur.execute("""UPDATE saved_plans SET archived=FALSE,archived_at=NULL,updated_at=NOW() WHERE id=%s""", (int(plan_id),))
        conn.commit()


def rename_saved_plan(plan_id, title):
    title=(title or '').strip()
    if not title:
        raise ValueError('Naam van de planning mag niet leeg zijn.')
    ensure_schema()
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE saved_plans SET title=%s,updated_at=NOW() WHERE id=%s", (title,int(plan_id)))
        conn.commit()


def list_saved_plan_versions(plan_id, limit=20):
    ensure_schema()
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT id,version_no,note,created_at FROM saved_plan_versions
                WHERE plan_id=%s ORDER BY version_no DESC LIMIT %s""", (int(plan_id),int(limit)))
            return [dict(r) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Manual issue resolutions (v12.3)
# ---------------------------------------------------------------------------

def save_plan_issue_resolution(plan_id, job_key, issue_type, resolution_label='', note=''):
    """Persist a manual operational solution for one planning problem.

    issue_type is one of: material, logistics, personnel.
    The underlying warning is not deleted; it is acknowledged as solved externally.
    """
    ensure_schema()
    issue_type=(issue_type or '').strip().lower()
    if issue_type not in {'material','logistics','personnel'}:
        raise ValueError('Onbekend probleemtype.')
    job_key=(job_key or '').strip()
    if not job_key:
        raise ValueError('Klusreferentie ontbreekt.')
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO saved_plan_issue_resolutions
                (plan_id,job_key,issue_type,resolution_label,note,active,resolved_at,updated_at)
                VALUES (%s,%s,%s,%s,%s,TRUE,NOW(),NOW())
                ON CONFLICT(plan_id,job_key,issue_type) DO UPDATE SET
                    resolution_label=EXCLUDED.resolution_label,
                    note=EXCLUDED.note,
                    active=TRUE,resolved_at=NOW(),updated_at=NOW()""",
                (int(plan_id),job_key,issue_type,(resolution_label or '').strip(),(note or '').strip()))
        conn.commit()


def clear_plan_issue_resolution(plan_id, job_key, issue_type):
    ensure_schema()
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""UPDATE saved_plan_issue_resolutions SET active=FALSE,updated_at=NOW()
                WHERE plan_id=%s AND job_key=%s AND issue_type=%s""",
                (int(plan_id),(job_key or '').strip(),(issue_type or '').strip().lower()))
        conn.commit()


def get_plan_issue_resolutions(plan_id):
    if not plan_id:
        return []
    ensure_schema()
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT job_key,issue_type,resolution_label,note,resolved_at
                FROM saved_plan_issue_resolutions
                WHERE plan_id=%s AND active=TRUE
                ORDER BY resolved_at""", (int(plan_id),))
            return [dict(r) for r in cur.fetchall()]

# --- Personnel administration / hours (v13.0) -----------------------------

def get_employee_by_email(email):
    email=(email or '').strip().lower()
    if not email:
        return None
    ensure_schema()
    with connection() as conn:
        with conn.cursor() as cur:
            _ensure_personnel_schema(cur)
            cur.execute("""SELECT id,name,email,username,password_hash,portal_active,active,level
                           FROM employees WHERE LOWER(email)=%s LIMIT 1""", (email,))
            row=cur.fetchone()
    return dict(row) if row else None


def get_employee_by_login(login):
    login=(login or '').strip().lower()
    if not login:
        return None
    ensure_schema()
    with connection() as conn:
        with conn.cursor() as cur:
            _ensure_personnel_schema(cur)
            cur.execute("""SELECT id,name,email,username,password_hash,portal_active,active,level
                           FROM employees
                           WHERE LOWER(username)=%s OR LOWER(email)=%s
                           LIMIT 1""", (login,login))
            row=cur.fetchone()
    return dict(row) if row else None


def set_employee_password(employee_id,password_hash):
    ensure_schema()
    with connection() as conn:
        with conn.cursor() as cur:
            _ensure_personnel_schema(cur)
            cur.execute("UPDATE employees SET password_hash=%s,portal_active=TRUE,updated_at=NOW() WHERE id=%s",
                        (password_hash or '',int(employee_id)))
        conn.commit()


def get_employee_by_id(employee_id):
    ensure_schema()
    with connection() as conn:
        with conn.cursor() as cur:
            _ensure_personnel_schema(cur)
            cur.execute("""SELECT id,name,email,username,password_hash,portal_active,active,level,
                                  driving_license,own_transport,notes
                           FROM employees WHERE id=%s""", (int(employee_id),))
            row=cur.fetchone()
    return dict(row) if row else None


def set_saved_plan_roster_status(plan_id, status, finalized=False, reopened=False):
    ensure_schema()
    with connection() as conn:
        with conn.cursor() as cur:
            sets=["roster_status=%s", "updated_at=NOW()"]
            params=[status]
            if finalized:
                sets.append("roster_finalized_at=NOW()")
                sets.append("roster_reopened_at=NULL")
            if reopened:
                sets.append("roster_reopened_at=NOW()")
            params.append(int(plan_id))
            cur.execute(f"UPDATE saved_plans SET {', '.join(sets)} WHERE id=%s", params)
        conn.commit()


def replace_plan_shifts(plan_id, shifts):
    """Synchronise the finalized roster for one saved plan.

    Existing employee responses are preserved when the same employee/service remains.
    Removed shifts are deleted only while they are not already approved/pending approval.
    """
    ensure_schema()
    keep=[]
    with connection() as conn:
        with conn.cursor() as cur:
            _ensure_personnel_schema(cur)
            for sh in shifts:
                key=(int(plan_id), int(sh['employee_id']), sh['service_key'])
                keep.append((int(sh['employee_id']), sh['service_key']))
                cur.execute("""SELECT id,status FROM employee_shifts
                               WHERE plan_id=%s AND employee_id=%s AND service_key=%s""", key)
                existing=cur.fetchone()
                if existing and existing['status'] in {'pending_planner','approved','auto_approved','planner_approved'}:
                    # Preserve submitted/approved hours. Planner can explicitly reopen/edit later.
                    cur.execute("""UPDATE employee_shifts SET activity_summary=%s,vehicle_code=%s,
                                      location_summary=%s,updated_at=NOW() WHERE id=%s""",
                                (sh.get('activity_summary',''),sh.get('vehicle_code',''),sh.get('location_summary',''),existing['id']))
                    continue
                cur.execute("""
                    INSERT INTO employee_shifts(
                        plan_id,employee_id,service_key,work_date,activity_summary,vehicle_code,location_summary,
                        planned_start,planned_end,planned_end_next_day,planned_gross_minutes,planned_break_minutes,
                        planned_net_minutes,actual_start,actual_end,actual_end_next_day,actual_break_minutes,
                        actual_gross_minutes,actual_net_minutes,status,employee_note,planner_note,employee_token,
                        planner_approval_token,finalized_at,reminder_sent_at,response_deadline,employee_responded_at,
                        planner_approved_at,auto_approved_at,reopened_at,updated_at
                    ) VALUES (
                        %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'scheduled','','',%s,'',NOW(),NULL,%s,NULL,NULL,NULL,NULL,NOW()
                    )
                    ON CONFLICT(plan_id,employee_id,service_key) DO UPDATE SET
                        work_date=EXCLUDED.work_date,activity_summary=EXCLUDED.activity_summary,
                        vehicle_code=EXCLUDED.vehicle_code,location_summary=EXCLUDED.location_summary,
                        planned_start=EXCLUDED.planned_start,planned_end=EXCLUDED.planned_end,
                        planned_end_next_day=EXCLUDED.planned_end_next_day,
                        planned_gross_minutes=EXCLUDED.planned_gross_minutes,
                        planned_break_minutes=EXCLUDED.planned_break_minutes,
                        planned_net_minutes=EXCLUDED.planned_net_minutes,
                        actual_start=EXCLUDED.actual_start,actual_end=EXCLUDED.actual_end,
                        actual_end_next_day=EXCLUDED.actual_end_next_day,
                        actual_break_minutes=EXCLUDED.actual_break_minutes,
                        actual_gross_minutes=EXCLUDED.actual_gross_minutes,actual_net_minutes=EXCLUDED.actual_net_minutes,
                        status='scheduled',employee_note='',planner_note='',employee_token=EXCLUDED.employee_token,
                        planner_approval_token='',finalized_at=NOW(),reminder_sent_at=NULL,
                        response_deadline=EXCLUDED.response_deadline,employee_responded_at=NULL,
                        planner_approved_at=NULL,auto_approved_at=NULL,reopened_at=NULL,updated_at=NOW()
                """, (
                    int(plan_id),int(sh['employee_id']),sh['service_key'],sh['work_date'],sh.get('activity_summary',''),
                    sh.get('vehicle_code',''),sh.get('location_summary',''),sh['planned_start'],sh['planned_end'],
                    bool(sh.get('planned_end_next_day')),int(sh['planned_gross_minutes']),int(sh['planned_break_minutes']),
                    int(sh['planned_net_minutes']),sh['planned_start'],sh['planned_end'],bool(sh.get('planned_end_next_day')),
                    int(sh['planned_break_minutes']),int(sh['planned_gross_minutes']),int(sh['planned_net_minutes']),
                    sh['employee_token'],sh['response_deadline'],
                ))
            if keep:
                clauses=[]; params=[int(plan_id)]
                for emp_id,service_key in keep:
                    clauses.append('(employee_id=%s AND service_key=%s)')
                    params.extend([emp_id,service_key])
                cur.execute(f"""DELETE FROM employee_shifts WHERE plan_id=%s
                                AND status IN ('scheduled','waiting_employee','reopened')
                                AND NOT ({' OR '.join(clauses)})""", params)
            else:
                cur.execute("DELETE FROM employee_shifts WHERE plan_id=%s AND status IN ('scheduled','waiting_employee','reopened')", (int(plan_id),))
        conn.commit()


def list_plan_shifts(plan_id):
    ensure_schema()
    with connection() as conn:
        with conn.cursor() as cur:
            _ensure_personnel_schema(cur)
            cur.execute("""SELECT s.*,e.name AS employee_name,e.email AS employee_email
                           FROM employee_shifts s JOIN employees e ON e.id=s.employee_id
                           WHERE s.plan_id=%s ORDER BY s.work_date,s.planned_start,e.name""", (int(plan_id),))
            rows=[dict(r) for r in cur.fetchall()]
    return rows


def list_employee_shifts(employee_id, start_date=None, end_date=None):
    ensure_schema()
    where=['s.employee_id=%s']; params=[int(employee_id)]
    if start_date:
        where.append('s.work_date >= %s'); params.append(start_date)
    if end_date:
        where.append('s.work_date <= %s'); params.append(end_date)
    with connection() as conn:
        with conn.cursor() as cur:
            _ensure_personnel_schema(cur)
            cur.execute(f"""SELECT s.*,p.title AS plan_title
                            FROM employee_shifts s LEFT JOIN saved_plans p ON p.id=s.plan_id
                            WHERE {' AND '.join(where)}
                            ORDER BY s.work_date,s.planned_start""", params)
            return [dict(r) for r in cur.fetchall()]


def get_shift_by_employee_token(token):
    token=(token or '').strip()
    if not token: return None
    ensure_schema()
    with connection() as conn:
        with conn.cursor() as cur:
            _ensure_personnel_schema(cur)
            cur.execute("""SELECT s.*,e.name AS employee_name,e.email AS employee_email,p.title AS plan_title
                           FROM employee_shifts s JOIN employees e ON e.id=s.employee_id
                           LEFT JOIN saved_plans p ON p.id=s.plan_id
                           WHERE s.employee_token=%s LIMIT 1""", (token,))
            row=cur.fetchone()
    return dict(row) if row else None


def get_shift_by_planner_token(token):
    token=(token or '').strip()
    if not token: return None
    ensure_schema()
    with connection() as conn:
        with conn.cursor() as cur:
            _ensure_personnel_schema(cur)
            cur.execute("""SELECT s.*,e.name AS employee_name,e.email AS employee_email,p.title AS plan_title
                           FROM employee_shifts s JOIN employees e ON e.id=s.employee_id
                           LEFT JOIN saved_plans p ON p.id=s.plan_id
                           WHERE s.planner_approval_token=%s LIMIT 1""", (token,))
            row=cur.fetchone()
    return dict(row) if row else None


def submit_employee_hours(shift_id, actual_start, actual_end, end_next_day, break_minutes,
                          gross_minutes, net_minutes, note, changed, planner_token=''):
    ensure_schema()
    status='pending_planner' if changed else 'approved'
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""UPDATE employee_shifts SET actual_start=%s,actual_end=%s,actual_end_next_day=%s,
                           actual_break_minutes=%s,actual_gross_minutes=%s,actual_net_minutes=%s,
                           employee_note=%s,status=%s,planner_approval_token=%s,
                           employee_responded_at=NOW(),planner_approved_at=CASE WHEN %s THEN NULL ELSE NOW() END,
                           updated_at=NOW() WHERE id=%s""",
                        (actual_start,actual_end,bool(end_next_day),int(break_minutes),int(gross_minutes),int(net_minutes),
                         note or '',status,planner_token or '',bool(changed),int(shift_id)))
        conn.commit()


def approve_shift_by_planner(shift_id, planner_note=''):
    ensure_schema()
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""UPDATE employee_shifts SET status='planner_approved',planner_note=%s,
                           planner_approved_at=NOW(),planner_approval_token='',updated_at=NOW() WHERE id=%s""",
                        (planner_note or '',int(shift_id)))
        conn.commit()


def reopen_shift(shift_id, response_deadline, note=''):
    ensure_schema()
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""UPDATE employee_shifts SET status='reopened',planner_note=%s,reopened_at=NOW(),
                           response_deadline=%s,reminder_sent_at=NULL,employee_responded_at=NULL,
                           planner_approved_at=NULL,auto_approved_at=NULL,planner_approval_token='',updated_at=NOW()
                           WHERE id=%s""", (note or '',response_deadline,int(shift_id)))
        conn.commit()


def planner_update_shift(shift_id, actual_start, actual_end, end_next_day, break_minutes,
                         gross_minutes, net_minutes, note='', approve=True):
    ensure_schema()
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""UPDATE employee_shifts SET actual_start=%s,actual_end=%s,actual_end_next_day=%s,
                           actual_break_minutes=%s,actual_gross_minutes=%s,actual_net_minutes=%s,
                           planner_note=%s,status=%s,planner_approved_at=CASE WHEN %s THEN NOW() ELSE planner_approved_at END,
                           updated_at=NOW() WHERE id=%s""",
                        (actual_start,actual_end,bool(end_next_day),int(break_minutes),int(gross_minutes),int(net_minutes),
                         note or '', 'planner_approved' if approve else 'reopened', bool(approve), int(shift_id)))
        conn.commit()


def shifts_due_for_reminder(now_iso):
    ensure_schema()
    with connection() as conn:
        with conn.cursor() as cur:
            _ensure_personnel_schema(cur)
            cur.execute("""SELECT s.*,e.name AS employee_name,e.email AS employee_email
                           FROM employee_shifts s JOIN employees e ON e.id=s.employee_id
                           WHERE s.status IN ('scheduled','reopened') AND s.reminder_sent_at IS NULL
                             AND (((s.work_date + s.planned_end) AT TIME ZONE 'Europe/Amsterdam')
                                  + CASE WHEN s.planned_end_next_day THEN INTERVAL '1 day' ELSE INTERVAL '0 day' END) <= %s::timestamptz
                           ORDER BY s.work_date,s.planned_end""", (now_iso,))
            return [dict(r) for r in cur.fetchall()]


def mark_shift_reminder_sent(shift_id):
    ensure_schema()
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""UPDATE employee_shifts SET reminder_sent_at=NOW(),status='waiting_employee',updated_at=NOW()
                           WHERE id=%s AND status IN ('scheduled','reopened')""", (int(shift_id),))
        conn.commit()


def auto_approve_expired_shifts(now_iso):
    ensure_schema()
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""UPDATE employee_shifts SET status='auto_approved',auto_approved_at=NOW(),updated_at=NOW()
                           WHERE status IN ('scheduled','waiting_employee','reopened')
                             AND response_deadline IS NOT NULL AND response_deadline <= %s::timestamptz
                           RETURNING id""", (now_iso,))
            rows=cur.fetchall()
        conn.commit()
    return len(rows)


def personnel_hours_month(year, month):
    ensure_schema()
    start=f"{int(year):04d}-{int(month):02d}-01"
    if int(month)==12:
        end=f"{int(year)+1:04d}-01-01"
    else:
        end=f"{int(year):04d}-{int(month)+1:02d}-01"
    with connection() as conn:
        with conn.cursor() as cur:
            _ensure_personnel_schema(cur)
            cur.execute("""SELECT s.*,e.name AS employee_name,e.email AS employee_email
                           FROM employee_shifts s JOIN employees e ON e.id=s.employee_id
                           WHERE s.work_date >= %s AND s.work_date < %s
                           ORDER BY e.name,s.work_date,s.planned_start""", (start,end))
            rows=[dict(r) for r in cur.fetchall()]
    totals={}
    for r in rows:
        t=totals.setdefault(r['employee_id'],{'employee_id':r['employee_id'],'name':r['employee_name'],'gross_minutes':0,'break_minutes':0,'net_minutes':0,'days':set(),'shifts':0})
        gross=r['actual_gross_minutes'] if r['actual_gross_minutes'] is not None else r['planned_gross_minutes']
        brk=r['actual_break_minutes'] if r['actual_break_minutes'] is not None else r['planned_break_minutes']
        net=r['actual_net_minutes'] if r['actual_net_minutes'] is not None else r['planned_net_minutes']
        t['gross_minutes']+=int(gross or 0); t['break_minutes']+=int(brk or 0); t['net_minutes']+=int(net or 0)
        t['days'].add(str(r['work_date'])); t['shifts']+=1
    out=[]
    for t in totals.values():
        t['days']=len(t['days']); out.append(t)
    out.sort(key=lambda x:x['name'].casefold())
    return rows,out
