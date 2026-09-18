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


def preload_corrections():
    """Load all small correction tables in one DB connection for fast uploads."""
    if not configured():
        return {}, {}, {}
    ensure_schema()
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT alias_key, canonical_activity FROM activity_aliases")
            activities = {r["alias_key"]: r["canonical_activity"] for r in cur.fetchall()}
            cur.execute("SELECT alias_key, location_text, label, lat, lon FROM location_aliases")
            locations = {r["alias_key"]: r for r in cur.fetchall()}
            cur.execute("SELECT reference, activity, location_text FROM reservation_corrections")
            reservations = {str(r["reference"]): r for r in cur.fetchall()}
    return activities, locations, reservations


def save_corrections_batch(items):
    """Save all edited rows in one transaction; no external API calls here."""
    if not configured():
        raise RuntimeError("DATABASE_URL ontbreekt.")
    ensure_schema()
    with connection() as conn:
        with conn.cursor() as cur:
            for item in items:
                reference = item.get("reference", "").strip()
                original_activity = item.get("original_activity", "").strip()
                original_location_hint = item.get("original_location_hint", "").strip()
                activity = item.get("activity", "").strip()
                location_text = item.get("location_text", "").strip()

                if reference:
                    cur.execute(
                        """
                        INSERT INTO reservation_corrections(reference, activity, location_text, updated_at)
                        VALUES (%s,%s,%s,NOW())
                        ON CONFLICT(reference) DO UPDATE SET
                          activity=EXCLUDED.activity,
                          location_text=EXCLUDED.location_text,
                          updated_at=NOW()
                        """,
                        (reference, activity, location_text),
                    )

                if original_activity and original_activity != "ONBEKEND" and activity and activity != original_activity:
                    cur.execute(
                        """
                        INSERT INTO activity_aliases(alias_key, alias_text, canonical_activity, updated_at)
                        VALUES (%s,%s,%s,NOW())
                        ON CONFLICT(alias_key) DO UPDATE SET
                          alias_text=EXCLUDED.alias_text,
                          canonical_activity=EXCLUDED.canonical_activity,
                          updated_at=NOW()
                        """,
                        (_norm(original_activity), original_activity, activity),
                    )

                if original_location_hint and location_text:
                    cur.execute(
                        """
                        INSERT INTO location_aliases(alias_key, alias_text, location_text, updated_at)
                        VALUES (%s,%s,%s,NOW())
                        ON CONFLICT(alias_key) DO UPDATE SET
                          alias_text=EXCLUDED.alias_text,
                          location_text=EXCLUDED.location_text,
                          updated_at=NOW()
                        """,
                        (_norm(original_location_hint), original_location_hint, location_text),
                    )
        conn.commit()


def norm_key(text):
    return _norm(text)
