import re
from datetime import datetime
import pandas as pd
import pdfplumber

MONTHS_NL = {
    'januari': 1, 'februari': 2, 'maart': 3, 'april': 4, 'mei': 5,
    'juni': 6, 'juli': 7, 'augustus': 8, 'september': 9,
    'oktober': 10, 'november': 11, 'december': 12,
}

ACTIVITY_PATTERNS = [
    ('Ik hou van Holland', [r'\bik hou van holland\b']),
    ('Moordspel', [r'\bmoordspel\b']),
    ('Moorddiner', [r'\bmoorddiner\b']),
    ('Expeditie Robinson', [r'\bexpeditie robinson\b']),
    ('Boogschieten', [r'\bboogschieten\b', r'\bbooschieten\b']),
    ('VR Game La Casa de Papel', [
        r'\bvr game(?:\s+la casa(?:\s+de papel)?)?\b',
        r'\bla casa de papel\b',
    ]),
    ('Hunted', [r'\bhunted\b']),
    ('Action Painting', [r'\baction painting\b']),
    ('Jongens tegen de meisjes', [r'\bjongens tegen de meisjes\b']),
    ('Alles mag vandaag', [r'\balles mag vandaag\b']),
    ('Bubbelvoetbal', [r'\bbubbelvoetbal\b']),
    ('Archery Tag', [r'\barchery tag\b', r'\barchery attack\b']),
    ('Western Games', [r'\bwestern games\b']),
    ('Minute to Win It', [r'\bminute to win it\b', r'\bmtwi\b']),
    ('Wie is de Mol', [r'\bwie is de mol\b']),
    ('Highland Games', [r'\bhighland games\b']),
    ('Alleskunner', [r'\balleskunner\b']),
    ('Pubquiz', [r'\bpubquiz\b']),
    ('Casino avond', [r'\bcasino ?avond\b']),
    ('Robin Hood Arrangement', [r'\brobin hood arrangement\b']),
]

DATE_RE = re.compile(
    r'^(Maandag|Dinsdag|Woensdag|Donderdag|Vrijdag|Zaterdag|Zondag)\s+'
    r'(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})$', re.I
)
ROW_RE = re.compile(r'^(\d{1,2}:\d{2})\s+(\d{1,2}:\d{2})\s+([^\s]+)\s+(.*)$')
REF_RE = re.compile(r'\b(?:Ref:\s*)?(\d{4,6})\s*$', re.I)
TIME_PREFIX_RE = re.compile(r'^(\d{1,2}:\d{2})\s+(\d{1,2}:\d{2})\s+(.*)$')
PROVISION_RE = re.compile(r'^(\d{1,2}:\d{2})\s+(\d{1,2}:\d{2})\s+([0-9]+(?:-[0-9]+)?)\s+(.+)$')
REF_ANY_RE = re.compile(r'\bRef:\s*(\d{4,6})\b', re.I)
PEOPLE_RE = re.compile(r'\bAantal\s+pers\s*:\s*([0-9]+(?:-[0-9]+)?)\b', re.I)


def ns(t):
    return re.sub(r'\s+', ' ', (t or '')).strip()


def _append_unique(target, items):
    for item in items:
        if item and item not in target:
            target.append(item)


def recognize_activities(desc):
    out = []
    low = (desc or '').lower()
    for name, pats in ACTIVITY_PATTERNS:
        if any(re.search(p, low, re.I) for p in pats):
            out.append(name)
    return out


def extract_location_hint(desc):
    text = ns(desc)
    for marker in [
        r'\bbij partner\s+',
        r'\bop eigen locatie in\s+',
        r'\bop locatie bij\s+',
        r'\bop locatie in\s+',
        r'\bop locatie\s+',
        r'\bop inloop bij\s+',
        r'\bbij\s+',
        r'\bin\s+',
    ]:
        matches = list(re.finditer(marker, text, re.I))
        if matches:
            hint = text[matches[-1].end():].strip(' ,.-')
            hint = re.split(r'\s+Aantal\s+pers\s*:', hint, maxsplit=1, flags=re.I)[0]
            hint = re.split(r'\s+:\s*INK\d+', hint, maxsplit=1, flags=re.I)[0]
            hint = re.split(r'\s+kostenplaats\s+\d+', hint, maxsplit=1, flags=re.I)[0]
            return ns(hint)
    return ''


def parse_date_line(line):
    m = DATE_RE.match(ns(line))
    if not m:
        return None
    _, d, mon, y = m.groups()
    mm = MONTHS_NL.get(mon.lower())
    return datetime(int(y), mm, int(d)).date().isoformat() if mm else None


def extract_pdf_text(f):
    with pdfplumber.open(f) as pdf:
        return '\n'.join((p.extract_text(x_tolerance=2, y_tolerance=3) or '') for p in pdf.pages)


def _is_reception_block_start(line):
    """A booking header starts with two times followed by non-numeric text.

    Provision rows also start with two times, but their third field is a numeric
    participant count. Those rows belong to the current booking block.
    """
    m = TIME_PREFIX_RE.match(ns(line))
    if not m:
        return False
    rest = m.group(3).strip()
    if not rest:
        return False
    first = rest.split(' ', 1)[0]
    return not bool(re.fullmatch(r'[0-9]+(?:-[0-9]+)?', first))


def _clean_summary_line(line):
    text = ns(line)
    text = PEOPLE_RE.sub('', text)
    return ns(text).strip(' ,-')


def _strip_contact_tail(text):
    """Remove obvious contact/reference tails from relation text when possible."""
    text = ns(text)
    text = REF_ANY_RE.sub('', text)
    return text.strip(' ,-')


def _extract_relation_location(relation, summary):
    """Choose a stable location hint for the editable location field.

    Prefer the booking summary ('... bij Beachclub Lemmer'). If that contains no
    location phrase, fall back to the relation header but remove contact/name tails.
    """
    hint = extract_location_hint(summary)
    if hint:
        return hint

    rel = ns(relation)
    if not rel:
        return ''

    # Common SEM relation header separators: ' - Dhr.', ' - Mevr.', etc.
    rel = re.split(r'\s+-\s+(?:Dhr\.|Mevr\.|De heer|Mw\.|Dhr|Mevr)\b', rel, maxsplit=1, flags=re.I)[0]
    return ns(rel)


def _looks_like_summary(candidate):
    if not candidate:
        return False
    if recognize_activities(candidate):
        return True
    return bool(re.search(
        r'\b(bij partner|op eigen locatie|op locatie|op inloop|bij|in)\b',
        candidate, re.I
    ))


def _build_reception_row(block, curdate):
    if not block:
        return None

    first = ns(block[0])
    hm = TIME_PREFIX_RE.match(first)
    if not hm:
        return None
    start, end, _ = hm.groups()

    # Reference can be on the first line or on a wrapped follow-up line.
    ref_idx = None
    reference = ''
    for i, line in enumerate(block):
        rm = REF_ANY_RE.search(ns(line))
        if rm:
            reference = rm.group(1)
            ref_idx = i
            break

    # Everything through the reference line forms the relation/contact header.
    header_end = ref_idx if ref_idx is not None else 0
    header_parts = []
    for i in range(0, header_end + 1):
        text = ns(block[i])
        if i == 0:
            tm = TIME_PREFIX_RE.match(text)
            text = tm.group(3) if tm else text
        text = _strip_contact_tail(text)
        if text:
            header_parts.append(text)
    relation = ns(' '.join(header_parts))

    # Content begins after the reference line (or after the first header line).
    content_start = (ref_idx + 1) if ref_idx is not None else 1
    content = [ns(x) for x in block[content_start:] if ns(x)]

    # Participant count: explicit 'Aantal pers' first, provision count second.
    participants = ''
    for line in content:
        pm = PEOPLE_RE.search(line)
        if pm:
            participants = pm.group(1)
            break

    # Explicit provision rows are authoritative for activity names.
    provisions = []
    activities = []
    for line in content:
        pmatch = PROVISION_RE.match(line)
        if not pmatch:
            continue
        p_start, p_end, p_count, p_name = pmatch.groups()
        p_name = ns(p_name)
        provisions.append({
            'start': p_start,
            'end': p_end,
            'count': p_count,
            'name': p_name,
        })
        recognized = recognize_activities(p_name)
        _append_unique(activities, recognized if recognized else [p_name])

    if not participants and provisions:
        participants = provisions[0]['count']

    # Find one booking summary line. Notes after that are not allowed to create
    # extra activities (e.g. 'BIJ SLECHT WEER VR GAME MEE').
    summary = ''
    fallback_summary = ''
    for line in content:
        if PROVISION_RE.match(line):
            continue
        if PEOPLE_RE.fullmatch(line):
            continue
        candidate = _clean_summary_line(line)
        if not candidate:
            continue
        if not fallback_summary:
            fallback_summary = candidate
        if _looks_like_summary(candidate):
            summary = candidate
            break
    if not summary:
        summary = fallback_summary

    # The booking summary may name an extra real activity that is not printed as
    # its own provision row (for example 'Boogschieten en Western Games').
    # Add activities from the chosen summary line only; notes are deliberately
    # excluded so text such as 'BIJ SLECHT WEER VR GAME MEE' cannot add a game.
    _append_unique(activities, recognize_activities(summary))

    # Remaining non-provision lines become notes.
    notes = []
    summary_consumed = False
    for line in content:
        if PROVISION_RE.match(line) or PEOPLE_RE.fullmatch(line):
            continue
        candidate = _clean_summary_line(line)
        if not candidate:
            continue
        if not summary_consumed and candidate == summary:
            summary_consumed = True
            continue
        notes.append(candidate)

    location_hint = _extract_relation_location(relation, summary)

    return {
        'date': curdate or '',
        'start': start,
        'end': end,
        'participants': participants,
        'description': summary or relation,
        'relation': relation,
        'reference': reference,
        'activities': activities,
        'activity': ' + '.join(activities) if activities else 'ONBEKEND',
        'location_hint': location_hint,
        'provisions': provisions,
        'raw_text': ns(' '.join(block)),
        'notes': ns(' '.join(notes)),
    }


def parse_reception_text(text):
    """Parse Smart Event Manager 'Receptielijst voorzieningen' by booking block.

    One booking starts at a header row (two times + non-numeric text) and keeps
    consuming wrapped lines, notes, 'Aantal pers', and one or more provision rows
    until the next booking header or date line.
    """
    lines = [ns(x) for x in (text or '').splitlines()]
    rows = []
    curdate = None
    block = []

    def finish_block():
        nonlocal block
        if block:
            row = _build_reception_row(block, curdate)
            if row:
                rows.append(row)
        block = []

    for line in lines:
        if not line:
            continue
        if line == 'Receptielijst voorzieningen':
            continue
        if line.startswith('Smart Event Manager'):
            # A booking can continue over a PDF page break.
            continue

        d = parse_date_line(line)
        if d:
            finish_block()
            curdate = d
            continue

        if line.lower().startswith('vanaf t/m'):
            continue

        if _is_reception_block_start(line):
            finish_block()
            block = [line]
        elif block:
            block.append(line)

    finish_block()
    return rows


def parse_sem_text(text):
    """Parse older Smart Event Manager 'Reserveringen per dag' report."""
    lines = [x.rstrip() for x in (text or '').splitlines()]
    rows = []
    curdate = None
    cur = None

    def finish():
        nonlocal cur
        if not cur:
            return
        full = ns(' '.join(cur.pop('_chunks', [])))
        rm = REF_RE.search(full)
        if rm and not cur.get('reference'):
            cur['reference'] = rm.group(1)
            full = ns(full[:rm.start()])
        desc = ns(cur.get('description_seed') or full)
        acts = recognize_activities(desc)
        cur.update(
            raw_text=full,
            description=desc,
            activities=acts,
            activity=' + '.join(acts) if acts else 'ONBEKEND',
            location_hint=extract_location_hint(desc),
        )
        rows.append(cur)
        cur = None

    for raw in lines:
        line = ns(raw)
        if not line or line == 'Reserveringen' or line.startswith('Smart Event Manager'):
            continue
        d = parse_date_line(line)
        if d:
            finish()
            curdate = d
            continue
        if line.lower().startswith('vanaf t/m'):
            continue
        m = ROW_RE.match(line)
        if m and curdate:
            finish()
            start, end, amount, rest = m.groups()
            rm = REF_RE.search(rest)
            ref = rm.group(1) if rm else ''
            seed = ns(rest[:rm.start()]) if rm else rest
            cur = {
                'date': curdate,
                'start': start,
                'end': end,
                'participants': amount,
                'reference': ref,
                'description_seed': seed,
                '_chunks': [rest],
            }
        elif cur:
            cur['_chunks'].append(line)
    finish()
    return rows


def parse_pdf(f):
    """Detect Smart Event Manager report type from one extracted text pass."""
    text = extract_pdf_text(f)
    low = text.lower()
    is_reception = (
        'receptielijst voorzieningen' in low
        or 'aantal pers:' in low
        or ('voorziening' in low and 'ref:' in low)
    )
    if is_reception:
        return parse_reception_text(text)
    return parse_sem_text(text)


def parse_excel(f):
    df = pd.read_excel(f)
    norm = {str(c).strip().lower(): c for c in df.columns}

    def col(*names):
        for n in names:
            if n in norm:
                return norm[n]
        return None

    cd = col('datum', 'date')
    cs = col('vanaf', 'start', 'begintijd')
    ce = col('t/m', 'tm', 'eind', 'eindtijd')
    ca = col('aantal', 'deelnemers')
    co = col('omschrijving', 'description')
    cr = col('relatie', 'relation')
    cf = col('ref.', 'ref', 'referentie')
    if not co:
        raise ValueError('Geen kolom Omschrijving gevonden.')

    out = []
    for _, r in df.iterrows():
        desc = ns(str(r.get(co, '')))
        acts = recognize_activities(desc)
        dv = r.get(cd, '') if cd else ''
        if hasattr(dv, 'date'):
            dv = dv.date().isoformat()
        out.append({
            'date': str(dv),
            'start': str(r.get(cs, '') if cs else ''),
            'end': str(r.get(ce, '') if ce else ''),
            'participants': str(r.get(ca, '') if ca else ''),
            'description': desc,
            'relation': ns(str(r.get(cr, ''))) if cr else '',
            'reference': str(r.get(cf, '') if cf else ''),
            'activities': acts,
            'activity': ' + '.join(acts) if acts else 'ONBEKEND',
            'location_hint': extract_location_hint(desc),
        })
    return out


def parse_upload(fs):
    fn = (fs.filename or '').lower()
    if fn.endswith('.pdf'):
        return parse_pdf(fs.stream)
    if fn.endswith(('.xlsx', '.xls')):
        return parse_excel(fs.stream)
    raise ValueError('Gebruik PDF, XLSX of XLS.')


def to_dataframe(rows):
    clean = []
    for r in rows:
        x = dict(r)
        x['activities'] = ', '.join(x.get('activities', []))
        x.pop('description_seed', None)
        clean.append(x)
    return pd.DataFrame(clean)
