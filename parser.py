import re
from datetime import datetime
import pandas as pd
import pdfplumber

MONTHS_NL={'januari':1,'februari':2,'maart':3,'april':4,'mei':5,'juni':6,'juli':7,'augustus':8,'september':9,'oktober':10,'november':11,'december':12}
ACTIVITY_PATTERNS=[
('Ik hou van Holland',[r'\bik hou van holland\b']),('Moordspel',[r'\bmoordspel\b']),('Moorddiner',[r'\bmoorddiner\b']),
('Expeditie Robinson',[r'\bexpeditie robinson\b']),('Boogschieten',[r'\bboogschieten\b',r'\bbooschieten\b']),
('VR Game La Casa de Papel',[r'\bvr game(?: la casa de papel)?\b',r'\bla casa de papel\b']),('Hunted',[r'\bhunted\b']),
('Action Painting',[r'\baction painting\b']),('Jongens tegen de meisjes',[r'\bjongens tegen de meisjes\b']),
('Alles mag vandaag',[r'\balles mag vandaag\b']),('Bubbelvoetbal',[r'\bbubbelvoetbal\b']),('Archery Tag',[r'\barchery tag\b']),
('Western Games',[r'\bwestern games\b']),('Minute to Win It',[r'\bminute to win it\b',r'\bmtwi\b']),
('Wie is de Mol',[r'\bwie is de mol\b']),('Highland Games',[r'\bhighland games\b']),('Alleskunner',[r'\balleskunner\b']),
('Pubquiz',[r'\bpubquiz\b']),('Casino avond',[r'\bcasino ?avond\b']),('Robin Hood Arrangement',[r'\brobin hood arrangement\b'])]
DATE_RE=re.compile(r'^(Maandag|Dinsdag|Woensdag|Donderdag|Vrijdag|Zaterdag|Zondag)\s+(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})$',re.I)
ROW_RE=re.compile(r'^(\d{1,2}:\d{2})\s+(\d{1,2}:\d{2})\s+([^\s]+)\s+(.*)$')
REF_RE=re.compile(r'\b(\d{4,6})\s*$')

def ns(t): return re.sub(r'\s+',' ',(t or '')).strip()

def recognize_activities(desc):
    out=[]; low=(desc or '').lower()
    for name,pats in ACTIVITY_PATTERNS:
        if any(re.search(p,low,re.I) for p in pats): out.append(name)
    return out

def extract_location_hint(desc):
    text=ns(desc)
    for marker in [r'\bbij partner\s+',r'\bop eigen locatie in\s+',r'\bop locatie bij\s+',r'\bop locatie in\s+',r'\bop locatie\s+',r'\bbij\s+',r'\bin\s+']:
        m=list(re.finditer(marker,text,re.I))
        if m:
            hint=text[m[-1].end():].strip(' ,.-')
            hint=re.split(r'\s+:\s*INK\d+',hint,maxsplit=1,flags=re.I)[0]
            hint=re.split(r'\s+kostenplaats\s+\d+',hint,maxsplit=1,flags=re.I)[0]
            return ns(hint)
    return ''

def parse_date_line(line):
    m=DATE_RE.match(ns(line))
    if not m:return None
    _,d,mon,y=m.groups(); mm=MONTHS_NL.get(mon.lower())
    return datetime(int(y),mm,int(d)).date().isoformat() if mm else None

def extract_pdf_text(f):
    with pdfplumber.open(f) as pdf:
        return '\n'.join((p.extract_text(x_tolerance=2,y_tolerance=3) or '') for p in pdf.pages)

def parse_sem_pdf(f):
    lines=[x.rstrip() for x in extract_pdf_text(f).splitlines()]
    rows=[]; curdate=None; cur=None
    def finish():
        nonlocal cur
        if not cur:return
        full=ns(' '.join(cur.pop('_chunks',[])))
        rm=REF_RE.search(full)
        if rm and not cur.get('reference'): cur['reference']=rm.group(1); full=ns(full[:rm.start()])
        desc=ns(cur.get('description_seed') or full)
        acts=recognize_activities(desc)
        cur.update(raw_text=full,description=desc,activities=acts,activity=' + '.join(acts) if acts else 'ONBEKEND',location_hint=extract_location_hint(desc))
        rows.append(cur); cur=None
    for raw in lines:
        line=ns(raw)
        if not line or line=='Reserveringen' or line.startswith('Smart Event Manager'):continue
        d=parse_date_line(line)
        if d: finish(); curdate=d; continue
        if line.lower().startswith('vanaf t/m'):continue
        m=ROW_RE.match(line)
        if m and curdate:
            finish(); start,end,amount,rest=m.groups(); rm=REF_RE.search(rest)
            ref=rm.group(1) if rm else ''; seed=ns(rest[:rm.start()]) if rm else rest
            cur={'date':curdate,'start':start,'end':end,'participants':amount,'reference':ref,'description_seed':seed,'_chunks':[rest]}
        elif cur: cur['_chunks'].append(line)
    finish(); return rows

def parse_excel(f):
    df=pd.read_excel(f); norm={str(c).strip().lower():c for c in df.columns}
    def col(*names):
        for n in names:
            if n in norm:return norm[n]
        return None
    cd,cs,ce,ca,co,cr,cf=col('datum','date'),col('vanaf','start','begintijd'),col('t/m','tm','eind','eindtijd'),col('aantal','deelnemers'),col('omschrijving','description'),col('relatie','relation'),col('ref.','ref','referentie')
    if not co: raise ValueError('Geen kolom Omschrijving gevonden.')
    out=[]
    for _,r in df.iterrows():
        desc=ns(str(r.get(co,''))); acts=recognize_activities(desc)
        dv=r.get(cd,'') if cd else ''
        if hasattr(dv,'date'): dv=dv.date().isoformat()
        out.append({'date':str(dv),'start':str(r.get(cs,'') if cs else ''),'end':str(r.get(ce,'') if ce else ''),'participants':str(r.get(ca,'') if ca else ''),'description':desc,'relation':ns(str(r.get(cr,''))) if cr else '','reference':str(r.get(cf,'') if cf else ''),'activities':acts,'activity':' + '.join(acts) if acts else 'ONBEKEND','location_hint':extract_location_hint(desc)})
    return out

def parse_upload(fs):
    fn=(fs.filename or '').lower()
    if fn.endswith('.pdf'):return parse_sem_pdf(fs.stream)
    if fn.endswith(('.xlsx','.xls')):return parse_excel(fs.stream)
    raise ValueError('Gebruik PDF, XLSX of XLS.')

def to_dataframe(rows):
    clean=[]
    for r in rows:
        x=dict(r); x['activities']=', '.join(x.get('activities',[])); x.pop('description_seed',None); clean.append(x)
    return pd.DataFrame(clean)
