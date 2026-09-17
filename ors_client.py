import os
import requests

BASE = 'https://api.openrouteservice.org'


class ORSError(RuntimeError):
    pass


class ORSClient:
    def __init__(self, api_key=None):
        self.api_key = api_key or os.getenv('ORS_API_KEY')
        if not self.api_key:
            raise ORSError('ORS_API_KEY ontbreekt.')
        self.headers = {'Authorization': self.api_key, 'Content-Type': 'application/json'}

    def directions(self, coordinates):
        r = requests.post(
            f'{BASE}/v2/directions/driving-car/geojson',
            headers=self.headers,
            json={'coordinates': coordinates},
            timeout=30,
        )
        if not r.ok:
            raise ORSError(f'ORS directions fout {r.status_code}: {r.text[:300]}')
        return r.json()

    def route_summary(self, origin, destination):
        data = self.directions([origin, destination])
        features = data.get('features', [])
        if not features:
            raise ORSError('ORS gaf geen autoroute terug.')
        summary = features[0].get('properties', {}).get('summary', {})
        return {
            'distance_km': round(float(summary.get('distance', 0)) / 1000, 1),
            'duration_minutes': round(float(summary.get('duration', 0)) / 60),
            'geojson': data,
        }

    def matrix(self, locations):
        r = requests.post(
            f'{BASE}/v2/matrix/driving-car',
            headers=self.headers,
            json={'locations': locations, 'metrics': ['duration', 'distance'], 'units': 'km'},
            timeout=30,
        )
        if not r.ok:
            raise ORSError(f'ORS matrix fout {r.status_code}: {r.text[:300]}')
        return r.json()
