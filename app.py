import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, render_template, request, redirect, url_for
from dotenv import load_dotenv
import db
from location_service import LocationService, LocationServiceError, clean_location_hint
from ors_client import ORSClient, ORSError
from parser import parse_upload
from planning_engine import build_logistics_plan
from personnel_engine import assign_staff_to_plan
from staff_parser import parse_personnel_workbook

load_dotenv(); app=Flask(__name__); app.secret_key=os.getenv('FLASK_SECRET_KEY','dev-change-me')


@app.after_request
def apply_jp_house_style(response):
    """Load one JP Activiteiten stylesheet on every rendered HTML page.

    This also covers templates such as fleet.html without requiring every
    template to be edited separately.
    """
    ctype=(response.content_type or '').lower()
    if 'text/html' not in ctype:
        return response
    try:
        html=response.get_data(as_text=True)
        if '/static/jp_theme.css' not in html:
            html=html.replace('</head>', '<link rel="stylesheet" href="/static/jp_theme.css?v=8.0"></head>')
        if 'jp-global-brandline' not in html:
            html=html.replace('<body', '<body', 1)
            body_end=html.find('> ', html.find('<body'))
            # Normal templates use <body> or <body ...>; inject immediately after the tag.
            idx=html.find('>', html.find('<body'))
            if idx >= 0:
                html=html[:idx+1]+'<div class="jp-global-brandline" aria-hidden="true"></div>'+html[idx+1:]
        response.set_data(html)
        response.headers['Content-Length']=str(len(response.get_data()))
    except Exception:
        pass
    return response

def db_status():
    try:
        if not db.configured(): return False
        db.ensure_schema(); return True
    except Exception: return False

def default_staff(participants):
    """Default staffing: 1 staff member per started block of 30 participants.

    1-30 -> 1, 31-60 -> 2, 61-90 -> 3, etc.
    The value remains manually editable and saved per reservation.
    """
    try:
        count = int(participants or 0)
    except (TypeError, ValueError):
        count = 0
    return max(1, (count + 29) // 30)


def _expand_location_labels(rows):
    """Resolve venue names to a full map label during import.

    Examples such as 'Paviljoen de Bloemert' or 'Robin Hood Drouwen' are
    resolved once, cached in PostgreSQL by LocationService, and on subsequent
    uploads loaded from that cache. A weak match is shown for review rather
    than silently treated as certain.
    """
    service=LocationService()
    queries=[]
    for row in rows:
        q=clean_location_hint(row.get('location_edit') or row.get('location_hint') or '')
        if q and q not in queries:
            queries.append(q)
    if not queries:
        return rows

    resolved={}
    def one(q):
        try:
            return q, service.resolve_display(q)
        except Exception:
            return q, None

    # Cached names return immediately. Unknown names are looked up concurrently,
    # avoiding the long sequential geocoding cycle that previously caused timeouts.
    with ThreadPoolExecutor(max_workers=min(4, max(1, len(queries)))) as pool:
        futures=[pool.submit(one,q) for q in queries]
        for fut in as_completed(futures):
            q,result=fut.result()
            if result:
                resolved[q]=result

    for row in rows:
        q=clean_location_hint(row.get('location_edit') or row.get('location_hint') or '')
        result=resolved.get(q)
        if not result:
            continue
        label=(result.get('label') or '').strip()
        if label:
            # Full Pelias label normally contains venue/street, postcode/place and country.
            row['location_edit']=label
            row['location']={
                'status':result.get('status','geocoded'),
                'query':q,
                'label':label,
                'lat':result.get('lat'),
                'lon':result.get('lon'),
            }
            row['location_source']='database' if result.get('status') in ('db_cached','cached') else 'map'
    return rows

def enrich_rows(rows):
    activities,locations,reservations=db.preload_corrections() if db.configured() else ({},{},{})
    for row in rows:
        original_activity=row.get('activity',''); row['original_activity']=original_activity
        reference=str(row.get('reference') or ''); ref_fix=reservations.get(reference)
        if ref_fix and ref_fix.get('activity'): row['activity']=ref_fix['activity']; row['activity_source']='reference'
        else:
            alias=activities.get(db.norm_key(original_activity)) if original_activity and original_activity!='ONBEKEND' else None
            row['activity']=alias or original_activity; row['activity_source']='database' if alias else 'automatic'
        hint=clean_location_hint(row.get('location_hint','')); row['location_hint']=hint; row['original_location_hint']=hint
        loc_alias=locations.get(db.norm_key(hint)) if hint else None; corrected=(ref_fix or {}).get('location_text') if ref_fix else None
        if corrected: row['location_edit']=corrected; row['location_source']='reference'; row['location']={'status':'saved','query':corrected,'label':corrected}
        elif loc_alias:
            row['location_edit']=loc_alias.get('location_text') or hint; row['location_source']='database'; row['location']={'status':'saved','query':row['location_edit'],'label':row['location_edit']}
        else: row['location_edit']=hint; row['location_source']='automatic'; row['location']={'status':'needs_review' if hint else 'missing','query':hint,'label':''}
        saved_staff=(ref_fix or {}).get('staff_required') if ref_fix else None
        row['staff_required']=saved_staff if saved_staff is not None else default_staff(row.get('participants'))
        row['staff_source']='manual' if saved_staff is not None else 'rule'
    return rows

def render_home(**kwargs):
    d={'rows':None,'error':None,'message':None,'route_result':None,'ors_configured':LocationService().configured,'db_configured':db_status(),'personnel_count':db.personnel_count() if db.configured() else 0}; d.update(kwargs); return render_template('index.html',**d)

@app.get('/')
def index(): return render_home()
@app.post('/upload')
def upload():
    f=request.files.get('file')
    if not f or not f.filename: return render_home(error='Kies eerst een bestand.')
    try:
        rows=enrich_rows(parse_upload(f))
        rows=_expand_location_labels(rows)
        return render_home(rows=rows)
    except Exception as e: return render_home(error=str(e))
@app.post('/save-corrections')
def save_corrections():
    if not db_status(): return render_home(error='Database is nog niet gekoppeld of bereikbaar.')
    count=int(request.form.get('row_count','0') or 0); items=[]
    for i in range(count):
        try: staff=max(1,int(request.form.get(f'staff_{i}','1') or 1))
        except ValueError: staff=1
        items.append({'reference':(request.form.get(f'reference_{i}') or '').strip(),'original_activity':(request.form.get(f'original_activity_{i}') or '').strip(),'original_location_hint':(request.form.get(f'original_location_hint_{i}') or '').strip(),'activity':(request.form.get(f'activity_{i}') or '').strip(),'location_text':(request.form.get(f'location_{i}') or '').strip(),'staff_required':staff})
    try: db.save_corrections_batch(items); return render_home(message=f'{len(items)} regels opgeslagen. Ook het aantal medewerkers wordt onthouden.')
    except Exception as e: return render_home(error=f'Opslaan mislukt: {e}')


def _jobs_from_form(form):
    count=int(form.get('row_count','0') or 0)
    jobs=[]
    overrides={}
    staff_overrides={}
    for i in range(count):
        ref=(form.get(f'reference_{i}') or '').strip()
        try:
            staff_required=max(1,int(form.get(f'staff_{i}') or 1))
        except ValueError:
            staff_required=1
        transport_employee=(form.get(f'extra_car_employee_{i}') or '').strip()
        jobs.append({
            'date':(form.get(f'date_{i}') or '').strip(),
            'start':(form.get(f'start_{i}') or '').strip(),
            'end':(form.get(f'end_{i}') or '').strip(),
            'participants':(form.get(f'participants_{i}') or '').strip(),
            'staff_required':staff_required,
            'setup_minutes':max(0,int(form.get(f'setup_{i}') or 30)),
            'cleanup_minutes':max(0,int(form.get(f'cleanup_{i}') or 30)),
            'activity':(form.get(f'activity_{i}') or '').strip(),
            'location_text':(form.get(f'location_{i}') or '').strip(),
            'reference':ref,
            'transport_employee':transport_employee,
        })
        ov=(form.get(f'override_{i}') or '').strip()
        if ov and ref: overrides[ref]=ov
        manual_names=[]
        for n in range(staff_required):
            name=(form.get(f'staff_person_{i}_{n}') or '').strip()
            if name:
                manual_names.append(name)
        # When an employee supplies the extra car, that person is pinned to the
        # assignment as a staff member as well. The personnel engine still checks
        # availability, skills, overlap and Own transport = Ja and shows warnings.
        if ov.startswith('Extra auto medewerker') and transport_employee:
            if transport_employee not in manual_names:
                manual_names.insert(0, transport_employee)
        if manual_names:
            staff_overrides[ref or str(i)] = manual_names
    return jobs,overrides,staff_overrides

@app.post('/generate-logistics')
def generate_logistics():
    try:
        jobs,overrides,staff_overrides=_jobs_from_form(request.form)
        if not jobs: return render_home(error='Geen opdrachten ontvangen voor de planning.')
        depots,vehicles,stock,resources=db.get_logistics()
        plan=build_logistics_plan(jobs,depots,vehicles,stock,resources,overrides)
        employees,_=db.get_personnel() if db.configured() else ([],None)
        plan=assign_staff_to_plan(plan,employees,staff_overrides) if employees else plan
        plan.setdefault('employees',employees)
        plan.setdefault('staff_problem_count',0)
        return render_template('planning.html',plan=plan)
    except Exception as e:
        return render_home(error=f'Planning genereren mislukt: {e}')


@app.get('/personnel')
def personnel():
    try:
        employees,last_import=db.get_personnel()
        refdata=db.personnel_reference_data()
        return render_template('personnel.html',employees=employees,last_import=last_import,refdata=refdata,error=None,message=request.args.get('message'))
    except Exception as e:
        return render_template('personnel.html',employees=[],last_import=None,refdata=db.personnel_reference_data(),error=str(e),message=None)

@app.post('/personnel/add')
def personnel_add():
    try:
        db.add_personnel_employee(
            request.form.get('name',''),
            request.form.get('driving_license','Onbekend'),
            request.form.get('own_transport','Onbekend'),
            request.form.get('notes',''),
        )
        return redirect(url_for('personnel',message='Nieuwe medewerker toegevoegd.'))
    except Exception as e:
        return redirect(url_for('personnel',message=f'Toevoegen mislukt: {e}'))

@app.post('/personnel/save')
def personnel_save():
    try:
        employee_id=int(request.form['employee_id'])
        enabled=[]
        for mode, key in [('', 'profile_fixed'), ('EVEN','profile_even'), ('ONEVEN','profile_odd')]:
            if request.form.get(key)=='on': enabled.append(mode)
        availability={}
        for mode, prefix in [('', 'fixed'), ('EVEN','even'), ('ONEVEN','odd')]:
            availability[mode]={}
            for slot in db.personnel_reference_data()['slots']:
                field=f"availability__{prefix}__{slot}"
                availability[mode][slot]=request.form.get(field,'onbekend')
        skills={activity:request.form.get(f'skill__{activity}','Onbekend')
                for activity in db.personnel_reference_data()['skills']}
        db.save_personnel_employee(
            employee_id=employee_id,
            name=request.form.get('name',''),
            driving_license=request.form.get('driving_license','Onbekend'),
            own_transport=request.form.get('own_transport','Onbekend'),
            active=request.form.get('active')=='on',
            notes=request.form.get('notes',''),
            enabled_week_modes=enabled,
            availability_by_mode=availability,
            skills=skills,
        )
        return redirect(url_for('personnel',message=f"{request.form.get('name','Medewerker')} opgeslagen."))
    except Exception as e:
        return redirect(url_for('personnel',message=f'Opslaan mislukt: {e}'))

@app.post('/personnel/upload')
def personnel_upload():
    f=request.files.get('file')
    if not f or not f.filename:
        return redirect(url_for('personnel',message='Kies eerst een Excel-bestand.'))
    try:
        rows,_=parse_personnel_workbook(f.stream)
        unique=db.save_personnel_import(rows,f.filename)
        return redirect(url_for('personnel',message=f'{unique} medewerker(s) bijgewerkt. Nieuwe gegevens gelden voor nieuwe en herberekende planningen.'))
    except Exception as e:
        return redirect(url_for('personnel',message=f'Import mislukt: {e}'))

@app.get('/fleet')
def fleet():
    try:
        depots,vehicles,stock,resources=db.get_logistics(); return render_template('fleet.html',depots=depots,vehicles=vehicles,stock=stock,resources=resources,error=None,message=request.args.get('message'))
    except Exception as e: return render_template('fleet.html',depots=[],vehicles=[],stock=[],resources=[],error=str(e),message=None)
@app.post('/fleet/save')
def fleet_save():
    try:
        code=request.form['code']; db.save_vehicle(code,request.form['depot_code'],request.form['vehicle_type'],request.form.get('active')=='on',request.form.get('standard_game_set')=='on',int(request.form.get('game_capacity') or 0),request.form.get('notes',''))
        return redirect(url_for('fleet',message=f'{code} opgeslagen.'))
    except Exception as e: return redirect(url_for('fleet',message=f'Opslaan mislukt: {e}'))
@app.post('/route-test')
def route_test():
    origin_text=clean_location_hint(request.form.get('origin','')); destination_text=clean_location_hint(request.form.get('destination',''))
    try:
        loc=LocationService(); origin=loc.resolve(origin_text); destination=loc.resolve(destination_text)
        if origin.get('status') in {'missing','needs_api','unresolved','error'}: raise LocationServiceError(f'Startlocatie niet opgelost: {origin_text}')
        if destination.get('status') in {'missing','needs_api','unresolved','error'}: raise LocationServiceError(f'Eindlocatie niet opgelost: {destination_text}')
        route=ORSClient().route_summary([origin['lon'],origin['lat']],[destination['lon'],destination['lat']]); return render_home(route_result={'origin':origin,'destination':destination,'distance_km':route['distance_km'],'duration_minutes':route['duration_minutes']})
    except (LocationServiceError,ORSError,Exception) as e: return render_home(error=str(e))
@app.get('/api/health')
def health(): return {'ok':True,'ors_key_configured':bool(os.getenv('ORS_API_KEY')),'database_configured':db_status(),'routing_profile':'driving-car'}
if __name__=='__main__': app.run(debug=True)
