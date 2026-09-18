import json
import os
import re
import threading
from pathlib import Path
from difflib import SequenceMatcher

import requests

try:
    import db
except Exception:
    db = None

ORS_BASE = "https://api.heigit.org"
CACHE_PATH = Path(os.getenv("LOCATION_CACHE_PATH", "data/locations.json"))
_LOCK = threading.Lock()


def _norm(text):
    return re.sub(r"\s+", " ", (text or "")).strip(" ,.;:-\t\n")


def _key(text):
    return _norm(text).casefold()


def clean_location_hint(text):
    """Conservative cleanup: remove booking metadata, never invent a place."""
    text = _norm(text)
    text = re.split(r"\s+:\s*INK\d+", text, maxsplit=1, flags=re.I)[0]
    text = re.split(r"\s+kostenplaats\s+\d+", text, maxsplit=1, flags=re.I)[0]
    text = re.sub(r"\s+\(Aantal onbekend\)\s*", " ", text, flags=re.I)
    return _norm(text)


def _query_variants(query):
    """Build increasingly forgiving geocoder queries.

    The planner often receives either a venue name ("De Beren Emmen") or a venue
    name followed by a real postal address ("Beachclub Lemmer Industrieweg 2,
    8531 PA Lemmer"). Pelias is more reliable when we try the most specific
    address/name forms separately instead of sending one long mixed string.
    """
    q = clean_location_hint(query)
    variants = []

    def add(value):
        value = _norm(value)
        if value and value.casefold() not in {v.casefold() for v in variants}:
            variants.append(value)

    add(q)

    # Full Dutch postal address hidden behind a venue/company name.
    # Example: "Beach club Lemmer Industrieweg 2, 8531 PA Lemmer"
    postcode = re.search(r"\b(\d{4}\s?[A-Z]{2})\b", q, re.I)
    street_house = re.search(
        r"\b([A-Za-zÀ-ÿ'\-\. ]+?\s+\d+[A-Za-z0-9\-/]*)\s*,?\s*(\d{4}\s?[A-Z]{2})\s+([A-Za-zÀ-ÿ'\- ]+)\s*$",
        q,
        re.I,
    )
    if street_house:
        street = _norm(street_house.group(1))
        pc = re.sub(r"\s+", " ", street_house.group(2).upper())
        city = _norm(street_house.group(3))
        add(f"{street}, {pc} {city}")
        add(f"{street}, {city}")
        add(f"{pc} {city}")

    # If there is a postcode but the street regex did not match, search from the
    # postcode onward. This strips a potentially confusing venue prefix.
    if postcode and not street_house:
        start = postcode.start()
        tail = _norm(q[start:])
        add(tail)

    # Venue-name queries work better with an explicit country suffix.
    add(f"{q}, Nederland")

    # Common formatting variants: "Beach club" vs "Beachclub".
    if re.search(r"\bbeach\s+club\b", q, re.I):
        add(re.sub(r"\bbeach\s+club\b", "Beachclub", q, flags=re.I))
    if re.search(r"\bbeachclub\b", q, re.I):
        add(re.sub(r"\bbeachclub\b", "Beach club", q, flags=re.I))

    return variants[:6]


def _tokens(text):
    return {
        t.casefold()
        for t in re.findall(r"[A-Za-zÀ-ÿ0-9]+", text or "")
        if len(t) >= 2 and t.casefold() not in {"de", "het", "een", "bij", "van", "en", "nl", "nederland"}
    }


def _score_match(original_query, variant, match):
    """Score a Pelias match; higher means more likely to be the requested venue."""
    label = match.get("label", "")
    name = match.get("name", "")
    locality = match.get("locality", "")
    postalcode = match.get("postalcode", "")
    haystack = " ".join([label, name, locality, postalcode])

    q_tokens = _tokens(original_query)
    h_tokens = _tokens(haystack)
    overlap = len(q_tokens & h_tokens) / max(1, len(q_tokens))

    similarity = SequenceMatcher(None, _norm(variant).casefold(), _norm(label).casefold()).ratio()
    confidence = match.get("confidence")
    try:
        confidence = float(confidence) if confidence is not None else 0.0
    except Exception:
        confidence = 0.0

    # Strongly reward exact postal-code matches when present in the input.
    pc_match = 0.0
    q_pc = re.search(r"\b\d{4}\s?[A-Z]{2}\b", original_query, re.I)
    if q_pc and postalcode:
        pc_match = 1.0 if re.sub(r"\s", "", q_pc.group(0)).casefold() == re.sub(r"\s", "", postalcode).casefold() else 0.0

    return overlap * 4.0 + similarity * 2.0 + confidence + pc_match * 5.0


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

    def geocode(self, query, size=6):
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
        r = requests.get(
            f"{ORS_BASE}/pelias/v1/search",
            params=params,
            headers=headers,
            timeout=(2.5, 5.0),
        )
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

    def _smart_geocode(self, query):
        """Try venue name and postal-address interpretations and pick the best hit."""
        variants = _query_variants(query)
        candidates = []
        last_error = None
        for variant in variants:
            try:
                matches = self.geocode(variant, size=6)
            except Exception as exc:
                last_error = exc
                continue
            for match in matches:
                score = _score_match(query, variant, match)
                candidates.append((score, variant, match))

            # A strong postal-address match is sufficient; avoid unnecessary calls.
            if candidates and max(s for s, _, _ in candidates) >= 8.0:
                break

        if not candidates:
            if last_error:
                raise last_error
            return None

        candidates.sort(key=lambda x: x[0], reverse=True)
        score, variant, best = candidates[0]
        best = dict(best)
        best["search_variant"] = variant
        best["match_score"] = round(score, 3)
        return best

    def resolve(self, query, use_cache=True):
        query = clean_location_hint(query)
        if not query:
            return {"status": "missing", "query": ""}

        k = _key(query)
        if use_cache:
            if db is not None and getattr(db, "configured", lambda: False)():
                try:
                    persistent = db.get_geocode_cache(query)
                    if persistent:
                        return {
                            "status": "db_cached",
                            "query": query,
                            "label": persistent.get("label") or query,
                            "lat": float(persistent["lat"]),
                            "lon": float(persistent["lon"]),
                        }
                except Exception:
                    pass
            cache = load_cache()
            if k in cache:
                item = dict(cache[k])
                item.update({"status": "cached", "query": query})
                return item

        if not self.configured:
            return {"status": "needs_api", "query": query}

        best = self._smart_geocode(query)
        if not best:
            return {"status": "unresolved", "query": query}

        record = {
            "label": best["label"],
            "lon": best["lon"],
            "lat": best["lat"],
            "locality": best.get("locality", ""),
            "region": best.get("region", ""),
            "search_variant": best.get("search_variant", query),
            "match_score": best.get("match_score"),
        }
        if use_cache:
            with _LOCK:
                cache = load_cache()
                cache[k] = record
                save_cache(cache)
            if db is not None and getattr(db, "configured", lambda: False)():
                try:
                    db.save_geocode_cache(query, record.get("label"), record.get("lat"), record.get("lon"))
                except Exception:
                    pass
        result = dict(record)
        result.update({"status": "geocoded", "query": query})
        return result
