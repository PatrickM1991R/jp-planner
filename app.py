import os
from flask import Flask, render_template, request, redirect, url_for
from dotenv import load_dotenv
import db
from location_service import LocationService, LocationServiceError, clean_location_hint
from ors_client import ORSClient, ORSError
from parser import parse_upload
from planning_engine import build_logistics_plan

load_dotenv(); app=Flask(__name__); app.secret_key=os.getenv('FLASK_SECRET_KEY','dev-change-me')

def db_status():
    try:
        if not db.configured(): return False
        db.ensure_schema(); return True
    except Exception: return False

def default_staff(participants):
    try:
        n=int(str(participants).split('-')[0]); return 2 if n>=30 else 1
    except Exception: return 1

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
    d={'rows':None,'error':None,'message':None,'route_result':None,'ors_configured':LocationService().configured,'db_configured':db_status()}; d.update(kwargs); return render_template('index.html',**d)

@app.get('/')
def index(): return render_home()
@app.post('/upload')
def upload():
    f=request.files.get('file')
    if not f or not f.filename: return render_home(error='Kies eerst een bestand.')
    try: return render_home(rows=enrich_rows(parse_upload(f)))
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
    for i in range(count):
        ref=(form.get(f'reference_{i}') or '').strip()
        jobs.append({
            'date':(form.get(f'date_{i}') or '').strip(),
            'start':(form.get(f'start_{i}') or '').strip(),
            'end':(form.get(f'end_{i}') or '').strip(),
            'participants':(form.get(f'participants_{i}') or '').strip(),
            'staff_required':int(form.get(f'staff_{i}') or 1),
            'setup_minutes':max(0,int(form.get(f'setup_{i}') or 30)),
            'cleanup_minutes':max(0,int(form.get(f'cleanup_{i}') or 30)),
            'activity':(form.get(f'activity_{i}') or '').strip(),
            'location_text':(form.get(f'location_{i}') or '').strip(),
            'reference':ref,
        })
        ov=(form.get(f'override_{i}') or '').strip()
        if ov and ref: overrides[ref]=ov
    return jobs,overrides

@app.post('/generate-logistics')
def generate_logistics():
    try:
        jobs,overrides=_jobs_from_form(request.form)
        if not jobs: return render_home(error='Geen opdrachten ontvangen voor de planning.')
        depots,vehicles,stock,resources=db.get_logistics()
        plan=build_logistics_plan(jobs,depots,vehicles,stock,resources,overrides)
        return render_template('planning.html',plan=plan)
    except Exception as e:
        return render_home(error=f'Planning genereren mislukt: {e}')

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
