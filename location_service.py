import json
import os
import re
import threading
from pathlib import Path

import requests

ORS_BASE = "https://api.openrouteservice.org"
CACHE_PATH = Path(os.getenv("LOCATION_CACHE_PATH", "data/locations.json"))
_LOCK = threading.Lock()


def _norm(text):
    return re.sub(r"\s+", " ", (text or "")).strip(" ,.;:-\t\n")


def _key(text):
    return _norm(text).casefold()


def clean_location_hint(text):
    """Conservative cleanup: remove obvious booking metadata, never invent a place."""
    text = _norm(text)
    text = re.split(r"\s+:\s*INK\d+", text, maxsplit=1, flags=re.I)[0]
    text = re.split(r"\s+kostenplaats\s+\d+", text, maxsplit=1, flags=re.I)[0]
    text = re.sub(r"\s+\(Aantal onbekend\)\s*", " ", text, flags=re.I)
    return _norm(text)


def load_cache():
    if not CACHE_PATH.exists():
        return {}
    try:
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_cache(cache):
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CACHE_PATH)


class LocationServiceError(RuntimeError):
    pass


class LocationService:
    def __init__(self, api_key=None):
        self.api_key = api_key or os.getenv("ORS_API_KEY")

    @property
    def configured(self):
        return bool(self.api_key)

    def geocode(self, query, size=5):
        query = clean_location_hint(query)
        if not query:
            raise LocationServiceError("Geen locatie opgegeven.")
        if not self.api_key:
            raise LocationServiceError("ORS_API_KEY ontbreekt.")

        params = {
            "text": query,
            "boundary.country": "NLD",
            "size": size,
        }
        headers = {"Authorization": self.api_key}
        r = requests.get(f"{ORS_BASE}/geocode/search", params=params, headers=headers, timeout=30)
        if not r.ok:
            raise LocationServiceError(f"ORS geocode fout {r.status_code}: {r.text[:250]}")
        features = r.json().get("features", [])
        out = []
        for f in features:
            props = f.get("properties", {})
            coords = (f.get("geometry") or {}).get("coordinates") or []
            if len(coords) < 2:
                continue
            out.append({
                "query": query,
                "label": props.get("label") or props.get("name") or query,
                "name": props.get("name") or "",
                "locality": props.get("locality") or props.get("localadmin") or "",
                "region": props.get("region") or "",
                "postalcode": props.get("postalcode") or "",
                "country": props.get("country") or "",
                "lon": float(coords[0]),
                "lat": float(coords[1]),
                "confidence": props.get("confidence"),
            })
        return out

    def resolve(self, query, use_cache=True):
        query = clean_location_hint(query)
        if not query:
            return {"status": "missing", "query": ""}

        k = _key(query)
        if use_cache:
            cache = load_cache()
            if k in cache:
                item = dict(cache[k])
                item.update({"status": "cached", "query": query})
                return item

        if not self.configured:
            return {"status": "needs_api", "query": query}

        matches = self.geocode(query, size=3)
        if not matches:
            return {"status": "unresolved", "query": query}

        best = matches[0]
        record = {
            "label": best["label"],
            "lon": best["lon"],
            "lat": best["lat"],
            "locality": best.get("locality", ""),
            "region": best.get("region", ""),
        }
        if use_cache:
            with _LOCK:
                cache = load_cache()
                cache[k] = record
                save_cache(cache)
        result = dict(record)
        result.update({"status": "geocoded", "query": query})
        return result
