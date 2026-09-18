import os
import re
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row


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
        conn.commit()
    seed_logistics()


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
