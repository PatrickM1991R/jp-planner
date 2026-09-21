import re
from openpyxl import load_workbook

AVAILABILITY_HEADERS = [
    'Ma -17','Ma >17','Di -17','Di >17','Wo -17','Wo >17','Do -17','Do >17',
    'Vr -17','Vr >17','Za -17','Za >17','Zo -17','Zo >17'
]
FIXED_HEADERS = {'Naam','Week','Opmerking','Rijbewijs','Eigen vervoer','Actief', *AVAILABILITY_HEADERS}


def _text(value):
    if value is None:
        return ''
    return re.sub(r'\s+', ' ', str(value)).strip()


def _yes_no_unknown(value):
    text = _text(value).casefold()
    if text in {'ja','j','yes','y','1','true','x','jaj'} or text.startswith('ja'):
        return 'Ja'
    if text in {'nee','n','no','0','false'} or text.startswith('nee'):
        return 'Nee'
    return 'Onbekend'


def _availability(value):
    text = _text(value)
    aliases = {
        '✓':'beschikbaar','✔':'beschikbaar','ja':'beschikbaar','beschikbaar':'beschikbaar',
        '~':'overleg','soms':'overleg','in overleg':'overleg','overleg':'overleg',
        '✕':'niet','×':'niet','x':'niet','nee':'niet','niet beschikbaar':'niet','niet':'niet',
        '?':'onbekend','onbekend':'onbekend','':'onbekend',
    }
    return aliases.get(text.casefold(), aliases.get(text, 'onbekend'))


def parse_personnel_workbook(fileobj):
    """Read the personnel workbook in one streaming pass.

    Important for Render: do not use ws.cell() repeatedly on a read-only worksheet.
    Repeated random access can cause openpyxl to re-scan the worksheet XML and can
    exceed Gunicorn's request timeout. iter_rows(values_only=True) streams it once.
    """
    try:
        wb = load_workbook(fileobj, data_only=True, read_only=True)
    except Exception as exc:
        raise ValueError(f'Excel-bestand kon niet worden geopend: {exc}') from exc

    try:
        if 'Personeel' not in wb.sheetnames:
            raise ValueError("Tabblad 'Personeel' ontbreekt.")

        ws = wb['Personeel']
        iterator = ws.iter_rows(values_only=True)

        header_row = None
        headers = None
        buffered_after_header = []

        # Find the header while streaming only once. The header is expected near
        # the top, but allow a little extra room for future instruction rows.
        for row_number, raw_row in enumerate(iterator, start=1):
            values = [_text(v) for v in raw_row]
            if 'Naam' in values and 'Ma -17' in values and 'Zo >17' in values:
                header_row = row_number
                headers = values
                break
            if row_number >= 30:
                break

        if not header_row or headers is None:
            raise ValueError('Kopregel met Naam en weekbeschikbaarheid niet gevonden.')

        # Remove trailing empty header cells, but preserve column positions up to
        # the last real header.
        last_header_index = max((i for i, h in enumerate(headers) if h), default=-1)
        headers = headers[:last_header_index + 1]

        idx = {h: i for i, h in enumerate(headers) if h}
        missing = [h for h in ['Naam', *AVAILABILITY_HEADERS] if h not in idx]
        if missing:
            raise ValueError('Ontbrekende kolommen: ' + ', '.join(missing))

        skill_headers = [h for h in headers if h and h not in FIXED_HEADERS]
        rows = []

        # Continue from the same iterator; no worksheet re-scan.
        for raw_row in iterator:
            vals = [_text(v) for v in raw_row[:len(headers)]]
            if len(vals) < len(headers):
                vals.extend([''] * (len(headers) - len(vals)))

            name = vals[idx['Naam']] if idx['Naam'] < len(vals) else ''
            if not name:
                continue

            week = vals[idx['Week']].upper() if 'Week' in idx and idx['Week'] < len(vals) else ''
            if week not in {'EVEN', 'ONEVEN'}:
                week = ''

            availability = {
                h: _availability(vals[idx[h]] if idx[h] < len(vals) else '')
                for h in AVAILABILITY_HEADERS
            }
            skills = {
                h: _yes_no_unknown(vals[idx[h]] if idx[h] < len(vals) else '')
                for h in skill_headers
            }

            rows.append({
                'name': name,
                'week_mode': week,
                'notes': vals[idx['Opmerking']] if 'Opmerking' in idx and idx['Opmerking'] < len(vals) else '',
                'driving_license': vals[idx['Rijbewijs']] if 'Rijbewijs' in idx and idx['Rijbewijs'] < len(vals) else '',
                'own_transport': _yes_no_unknown(vals[idx['Eigen vervoer']] if 'Eigen vervoer' in idx and idx['Eigen vervoer'] < len(vals) else ''),
                'active': _yes_no_unknown(vals[idx['Actief']] if 'Actief' in idx and idx['Actief'] < len(vals) else 'Ja') != 'Nee',
                'availability': availability,
                'skills': skills,
            })

        if not rows:
            raise ValueError('Geen medewerkers gevonden in het bestand.')

        return rows, skill_headers
    finally:
        try:
            wb.close()
        except Exception:
            pass
