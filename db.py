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
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS location_aliases (
                    alias_key TEXT PRIMARY KEY,
                    alias_text TEXT NOT NULL,
                    location_text TEXT NOT NULL,
                    label TEXT,
                    lat DOUBLE PRECISION,
                    lon DOUBLE PRECISION,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS activity_aliases (
                    alias_key TEXT PRIMARY KEY,
                    alias_text TEXT NOT NULL,
                    canonical_activity TEXT NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS reservation_corrections (
                    reference TEXT PRIMARY KEY,
                    activity TEXT,
                    location_text TEXT,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
        conn.commit()


def get_location_alias(alias_text):
    if not configured() or not alias_text:
        return None
    ensure_schema()
    key = _norm(alias_text)
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM location_aliases WHERE alias_key=%s", (key,))
            return cur.fetchone()


def upsert_location_alias(alias_text, location_text, resolved=None):
    if not configured() or not alias_text or not location_text:
        return
    ensure_schema()
    resolved = resolved or {}
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO location_aliases(alias_key, alias_text, location_text, label, lat, lon, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,NOW())
                ON CONFLICT(alias_key) DO UPDATE SET
                  alias_text=EXCLUDED.alias_text,
                  location_text=EXCLUDED.location_text,
                  label=EXCLUDED.label,
                  lat=EXCLUDED.lat,
                  lon=EXCLUDED.lon,
                  updated_at=NOW()
                """,
                (
                    _norm(alias_text), alias_text, location_text,
                    resolved.get("label"), resolved.get("lat"), resolved.get("lon")
                ),
            )
        conn.commit()


def get_activity_alias(alias_text):
    if not configured() or not alias_text:
        return None
    ensure_schema()
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT canonical_activity FROM activity_aliases WHERE alias_key=%s", (_norm(alias_text),))
            row = cur.fetchone()
            return row["canonical_activity"] if row else None


def upsert_activity_alias(alias_text, canonical_activity):
    if not configured() or not alias_text or not canonical_activity:
        return
    ensure_schema()
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO activity_aliases(alias_key, alias_text, canonical_activity, updated_at)
                VALUES (%s,%s,%s,NOW())
                ON CONFLICT(alias_key) DO UPDATE SET
                  alias_text=EXCLUDED.alias_text,
                  canonical_activity=EXCLUDED.canonical_activity,
                  updated_at=NOW()
                """,
                (_norm(alias_text), alias_text, canonical_activity),
            )
        conn.commit()


def get_reservation_correction(reference):
    if not configured() or not reference:
        return None
    ensure_schema()
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT activity, location_text FROM reservation_corrections WHERE reference=%s", (str(reference),))
            return cur.fetchone()


def upsert_reservation_correction(reference, activity, location_text):
    if not configured() or not reference:
        return
    ensure_schema()
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO reservation_corrections(reference, activity, location_text, updated_at)
                VALUES (%s,%s,%s,NOW())
                ON CONFLICT(reference) DO UPDATE SET
                  activity=EXCLUDED.activity,
                  location_text=EXCLUDED.location_text,
                  updated_at=NOW()
                """,
                (str(reference), activity, location_text),
            )
        conn.commit()
