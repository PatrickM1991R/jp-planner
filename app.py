import os
from datetime import datetime
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, render_template, request, redirect, url_for, session
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
            html=html.replace('</head>', '<link rel="stylesheet" href="/static/jp_theme.css?v=10.0"></head>')
        if 'jp-global-brandline' not in html:
            html=html.replace('<body', '<body', 1)
            body_end=html.find('> ', html.find('<body'))
            # Normal templates use <body> or <body ...>; inject immediately after the tag.
            idx=html.find('>', html.find('<body'))
            if idx >= 0:
                html=html[:idx+1]+'<div class="jp-global-brandline" aria-hidden="true"></div>'+html[idx+1:]
        # Hard-fix location status labels across old/new templates.
        # This keeps the UI readable even if Render/browser still serves an older index template.
        if 'jp-location-status-fix' not in html:
            status_script = '''<script id="jp-location-status-fix">
            document.addEventListener('DOMContentLoaded', function () {
              var known = ['db_cached','cached','saved','confirmed','geocoded'];
              document.querySelectorAll('.badge').forEach(function (el) {
                var t = (el.textContent || '').trim().toLowerCase();
                if (known.indexOf(t) !== -1) {
                  el.textContent = 'Locatie herkend';
                  el.classList.remove('b-bad','bad','b-warn','warn');
                  el.classList.add('b-ok');
                  el.style.background = '#dcfce7';
                  el.style.color = '#166534';
                }
              });
            });
            </script>'''
            html=html.replace('</body>', status_script+'</body>')
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

def _personnel_refdata():
    """Reference values used by the personnel editor.

    Keep this helper in app.py so the /personnel page can render reliably
    while db.py remains the source of truth for the actual values.
    """
    return db.personnel_reference_data()


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
            raw_status=result.get('status','geocoded')
            display_status='saved' if raw_status in ('db_cached','cached','saved','confirmed','geocoded') else raw_status
            row['location']={
                'status':display_status,
                'query':q,
                'label':label,
                'lat':result.get('lat'),
                'lon':result.get('lon'),
            }
            row['location_source']='database' if raw_status in ('db_cached','cached') else 'map'
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


def _today_nl():
    return datetime.now(ZoneInfo('Europe/Amsterdam')).date().isoformat()


def _auto_archive():
    if not db.configured():
        return 0
    try:
        # v12.2: clean up legacy duplicate dossiers before showing the archive.
        db.consolidate_duplicate_saved_plans()
        return db.auto_archive_saved_plans(_today_nl())
    except Exception:
        return 0


def _current_plan_id(form=None):
    value=''
    if form is not None:
        value=(form.get('planning_id') or '').strip()
    if not value:
        value=str(session.get('planning_id') or '').strip()
    try:
        return int(value) if value else None
    except (TypeError,ValueError):
        return None


def _merge_source_rows_from_form(plan_id, form):
    """Keep the original SEM source, but persist corrections made on screen."""
    if not plan_id or not db.configured():
        return
    record=db.get_saved_plan(plan_id)
    if not record:
        return
    rows=list(record.get('source_rows') or [])
    try:
        count=int(form.get('row_count','0') or 0)
    except ValueError:
        count=0
    while len(rows)<count:
        rows.append({})
    for i in range(count):
        row=dict(rows[i] or {})
        row.update({
            'date':(form.get(f'date_{i}') or row.get('date') or '').strip(),
            'start':(form.get(f'start_{i}') or row.get('start') or '').strip(),
            'end':(form.get(f'end_{i}') or row.get('end') or '').strip(),
            'participants':(form.get(f'participants_{i}') or row.get('participants') or '').strip(),
            'activity':(form.get(f'activity_{i}') or row.get('activity') or '').strip(),
            'location_edit':(form.get(f'location_{i}') or row.get('location_edit') or '').strip(),
            'location_text':(form.get(f'location_{i}') or row.get('location_text') or '').strip(),
            'reference':(form.get(f'reference_{i}') or row.get('reference') or '').strip(),
        })
        try:
            row['staff_required']=max(1,int(form.get(f'staff_{i}') or row.get('staff_required') or 1))
        except (TypeError,ValueError):
            row['staff_required']=1
        rows[i]=row
    db.update_saved_plan_source(plan_id,rows)


def _job_issue_key(job):
    ref=str(job.get('reference') or '').strip()
    if ref:
        return ref
    idx=job.get('form_index')
    return f'idx:{idx}' if idx is not None else ''


def _apply_issue_resolutions(plan, plan_id):
    """Apply persisted operational overrides without erasing the original issue.

    A manually solved issue stops counting as blocking, while the UI keeps a green
    acknowledgement showing how it was solved.
    """
    if not plan or not plan_id or not db.configured():
        return plan
    try:
        rows=db.get_plan_issue_resolutions(plan_id)
    except Exception:
        return plan
    resolutions={(str(r.get('job_key') or ''),str(r.get('issue_type') or '')):r for r in rows}
    if not resolutions:
        plan.setdefault('issue_resolutions',[])
        return plan

    all_jobs=list(plan.get('jobs') or [])
    resolved_refs=[]
    for job in all_jobs:
        key=_job_issue_key(job)
        solved=[]
        for issue_type in ('material','logistics','personnel'):
            row=resolutions.get((key,issue_type))
            if not row:
                continue
            label=row.get('resolution_label') or {
                'material':'Extra materiaal geregeld',
                'logistics':'Externe auto / vervoer geregeld',
                'personnel':'Externe medewerker geregeld',
            }[issue_type]
            solved.append({'type':issue_type,'label':label,'note':row.get('note') or ''})
            if issue_type=='material':
                job['material_problem']=False
                if job.get('status')=='material_problem':
                    job['status']='resolved_external_material'
                job['warnings']=[w for w in (job.get('warnings') or []) if 'MATERIAALPROBLEEM' not in str(w).upper()]
            elif issue_type=='personnel':
                job['staff_problem']=False
                job['staff_warnings']=[w for w in (job.get('staff_warnings') or []) if 'PERSONEELSPROBLEEM' not in str(w).upper()]
            elif issue_type=='logistics':
                if job.get('status')=='unplanned':
                    job['status']='resolved_external_transport'
                job['warnings']=[w for w in (job.get('warnings') or []) if 'GEEN VOERTUIG' not in str(w).upper()]
        if solved:
            job['resolved_issues']=solved
            job['issue_resolved']=True
            resolved_refs.append((key,{x['type'] for x in solved}))

    # Rebuild blocking collections/counters after manual resolutions.
    plan['unplanned_jobs']=[j for j in all_jobs if j.get('status') in {'unplanned','material_problem','location_problem'}]
    plan['unplanned_count']=len(plan['unplanned_jobs'])
    plan['material_problem_count']=sum(1 for j in all_jobs if j.get('material_problem'))
    plan['staff_problem_count']=sum(1 for j in all_jobs if j.get('staff_problem'))
    plan['resolved_issue_count']=sum(len(j.get('resolved_issues') or []) for j in all_jobs)
    plan['issue_resolutions']=rows

    # Hide top-level blocking warning lines for the exact issue that has been acknowledged.
    filtered=[]
    for warning in (plan.get('warnings') or []):
        text=str(warning)
        upper=text.upper()
        remove=False
        for key,types in resolved_refs:
            if key and (f'REF. {key}' in upper or f'REF {key}' in upper):
                if 'material' in types and 'MATERIAALPROBLEEM' in upper:
                    remove=True
                if 'personnel' in types and 'PERSONEELSPROBLEEM' in upper:
                    remove=True
                if 'logistics' in types and 'GEEN VOERTUIG' in upper:
                    remove=True
        if not remove:
            filtered.append(warning)
    plan['warnings']=filtered
    return plan


def render_home(**kwargs):
    _auto_archive()
    active_plans=[]
    if db.configured():
        try:
            active_plans=db.list_saved_plans(False,'',12)
        except Exception:
            active_plans=[]
    current_id=kwargs.pop('planning_id',None) or session.get('planning_id')
    d={'rows':None,'error':None,'message':None,'route_result':None,
       'ors_configured':LocationService().configured,'db_configured':db_status(),
       'personnel_count':db.personnel_count() if db.configured() else 0,
       'active_plans':active_plans,'planning_id':current_id}
    d.update(kwargs)
    return render_template('index.html',**d)

@app.get('/')
def index(): return render_home()
@app.post('/upload')
def upload():
    f=request.files.get('file')
    if not f or not f.filename:
        return render_home(error='Kies eerst een bestand.')
    try:
        filename=f.filename
        rows=enrich_rows(parse_upload(f))
        rows=_expand_location_labels(rows)
        planning_id=None
        if db_status():
            planning_id=db.create_saved_plan(rows,filename)
            session['planning_id']=planning_id
        return render_home(rows=rows,planning_id=planning_id,
                           message='Receptielijst opgeslagen als planningdossier. Je kunt hier later altijd naar terug.')
    except Exception as e:
        return render_home(error=str(e))
@app.post('/save-corrections')
def save_corrections():
    if not db_status(): return render_home(error='Database is nog niet gekoppeld of bereikbaar.')
    count=int(request.form.get('row_count','0') or 0); items=[]
    for i in range(count):
        try: staff=max(1,int(request.form.get(f'staff_{i}','1') or 1))
        except ValueError: staff=1
        items.append({'reference':(request.form.get(f'reference_{i}') or '').strip(),'original_activity':(request.form.get(f'original_activity_{i}') or '').strip(),'original_location_hint':(request.form.get(f'original_location_hint_{i}') or '').strip(),'activity':(request.form.get(f'activity_{i}') or '').strip(),'location_text':(request.form.get(f'location_{i}') or '').strip(),'staff_required':staff})
    try:
        db.save_corrections_batch(items)
        plan_id=_current_plan_id(request.form)
        _merge_source_rows_from_form(plan_id,request.form)
        return render_home(message=f'{len(items)} regels opgeslagen. Ook het aantal medewerkers en planningdossier worden onthouden.',planning_id=plan_id)
    except Exception as e:
        return render_home(error=f'Opslaan mislukt: {e}')


def _jobs_from_form(form):
    count=int(form.get('row_count','0') or 0)
    jobs=[]
    overrides={}
    staff_overrides={}
    depot_overrides={}
    for i in range(count):
        ref=(form.get(f'reference_{i}') or '').strip()
        try:
            staff_required=max(1,int(form.get(f'staff_{i}') or 1))
        except ValueError:
            staff_required=1
        transport_employee=(form.get(f'extra_car_employee_{i}') or '').strip()
        depot_override=(form.get(f'depot_override_{i}') or '').strip()
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
            'depot_override':depot_override,
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
        if depot_override:
            depot_overrides[ref or str(i)] = depot_override
    return jobs,overrides,staff_overrides,depot_overrides

@app.post('/generate-logistics')
def generate_logistics():
    try:
        jobs,overrides,staff_overrides,depot_overrides=_jobs_from_form(request.form)
        if not jobs:
            return render_home(error='Geen opdrachten ontvangen voor de planning.')
        depots,vehicles,stock,resources,vehicle_materials=db.get_logistics()
        plan=build_logistics_plan(jobs,depots,vehicles,stock,resources,vehicle_materials,overrides,depot_overrides)
        employees,_=db.get_personnel() if db.configured() else ([],None)
        plan=assign_staff_to_plan(plan,employees,staff_overrides) if employees else plan
        plan.setdefault('employees',employees)
        plan.setdefault('staff_problem_count',0)
        plan_id=_current_plan_id(request.form)
        plan=_apply_issue_resolutions(plan,plan_id)
        if db.configured():
            if not plan_id:
                rows=[dict(j) for j in jobs]
                plan_id=db.create_saved_plan(rows,'Handmatige planning')
            session['planning_id']=plan_id
            _merge_source_rows_from_form(plan_id,request.form)
            db.save_plan_snapshot(plan_id,plan,'Planning herberekend')
        record=db.get_saved_plan(plan_id) if plan_id and db.configured() else None
        return render_template('planning.html',plan=plan,planning_id=plan_id,planning_record=record)
    except Exception as e:
        return render_home(error=f'Planning genereren mislukt: {e}')




@app.post('/planning/resolve-issue')
def resolve_planning_issue():
    plan_id=_current_plan_id(request.form)
    if not plan_id:
        return render_home(error='Geen opgeslagen planning actief. Open of genereer eerst een planning.')
    value=(request.form.get('issue_resolution') or '').strip()
    parts=value.split('|',2)
    if len(parts)<2:
        return redirect(url_for('open_saved_plan',plan_id=plan_id))
    job_key,issue_type=parts[0].strip(),parts[1].strip().lower()
    labels={
        'material':'Extra materiaal geregeld',
        'logistics':'Externe auto / vervoer geregeld',
        'personnel':'Externe medewerker geregeld',
    }
    try:
        db.save_plan_issue_resolution(plan_id,job_key,issue_type,labels.get(issue_type,'Handmatig opgelost'))
        record=db.get_saved_plan(plan_id)
        if record and record.get('plan_snapshot'):
            plan=_apply_issue_resolutions(record['plan_snapshot'],plan_id)
            db.save_plan_snapshot(plan_id,plan,f'{labels.get(issue_type,"Probleem")} bevestigd')
        return redirect(url_for('open_saved_plan',plan_id=plan_id))
    except Exception as e:
        return render_home(error=f'Probleem als opgelost markeren mislukt: {e}')


@app.post('/planning/clear-issue-resolution')
def clear_planning_issue_resolution():
    plan_id=_current_plan_id(request.form)
    value=(request.form.get('issue_resolution') or '').strip()
    parts=value.split('|',2)
    if plan_id and len(parts)>=2:
        try:
            db.clear_plan_issue_resolution(plan_id,parts[0].strip(),parts[1].strip().lower())
        except Exception:
            pass
    return redirect(url_for('open_saved_plan',plan_id=plan_id)) if plan_id else redirect(url_for('index'))


@app.get('/plans')
def plans():
    _auto_archive()
    archived=request.args.get('archived','0')=='1'
    q=(request.args.get('q') or '').strip()
    try:
        plan_rows=db.list_saved_plans(archived,q,250) if db.configured() else []
        error=None
    except Exception as e:
        plan_rows=[]
        error=f'Planningen laden mislukt: {e}'
    return render_template(
        'plans.html',
        plans=plan_rows,
        archived=archived,
        q=q,
        error=error,
        message=(request.args.get('message') or '').strip(),
    )


@app.get('/plans/<int:plan_id>')
def open_saved_plan(plan_id):
    _auto_archive()
    record=db.get_saved_plan(plan_id) if db.configured() else None
    if not record:
        return render_home(error='Opgeslagen planning niet gevonden.')
    session['planning_id']=plan_id
    if record.get('plan_snapshot'):
        plan=record['plan_snapshot']
        # Lists/dicts from JSONB are directly usable by Jinja.
        plan=_apply_issue_resolutions(plan,plan_id)
        return render_template('planning.html',plan=plan,planning_id=plan_id,planning_record=record)
    rows=record.get('source_rows') or []
    return render_home(rows=rows,planning_id=plan_id,message=f"{record['title']} geopend. Genereer de logistieke planning om verder te gaan.")


@app.post('/plans/<int:plan_id>/archive')
def archive_saved_plan(plan_id):
    try:
        db.set_saved_plan_archived(plan_id,True)
        if session.get('planning_id')==plan_id:
            session.pop('planning_id',None)
        return redirect(url_for('plans',archived='1',message='Planning naar archief verplaatst.'))
    except Exception as e:
        return redirect(url_for('plans',message=f'Archiveren mislukt: {e}'))


@app.post('/plans/<int:plan_id>/restore')
def restore_saved_plan(plan_id):
    try:
        db.set_saved_plan_archived(plan_id,False)
        return redirect(url_for('plans',message='Planning teruggezet naar actueel.'))
    except Exception as e:
        return redirect(url_for('plans',archived='1',message=f'Terugzetten mislukt: {e}'))


@app.post('/plans/<int:plan_id>/rename')
def rename_saved_plan(plan_id):
    try:
        db.rename_saved_plan(plan_id,request.form.get('title',''))
        return redirect(request.referrer or url_for('plans'))
    except Exception as e:
        return redirect(url_for('plans',message=f'Naam aanpassen mislukt: {e}'))


@app.get('/personnel')
def personnel():
    try:
        employees,last_import=db.get_personnel()
        refdata=_personnel_refdata()
        return render_template('personnel.html',employees=employees,last_import=last_import,refdata=refdata,error=None,message=request.args.get('message'))
    except Exception as e:
        return render_template('personnel.html',employees=[],last_import=None,refdata=_personnel_refdata(),error=str(e),message=None)

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
            for slot in _personnel_refdata()['slots']:
                field=f"availability__{prefix}__{slot}"
                availability[mode][slot]=request.form.get(field,'onbekend')
        skills={activity:request.form.get(f'skill__{activity}','Onbekend')
                for activity in _personnel_refdata()['skills']}
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

@app.post('/personnel/skill/save')
def personnel_skill_save():
    try:
        activity=db.save_personnel_skill(
            request.form.get('activity',''),
            request.form.get('active','on')=='on',
            request.form.get('notes',''),
        )
        return redirect(url_for('personnel',message=f'Vaardigheid {activity} opgeslagen.'))
    except Exception as e:
        return redirect(url_for('personnel',message=f'Vaardigheid opslaan mislukt: {e}'))

@app.post('/personnel/skill/toggle')
def personnel_skill_toggle():
    try:
        activity=request.form.get('activity','')
        active=request.form.get('active')=='on'
        db.set_personnel_skill_active(activity,active)
        state='actief' if active else 'uitgezet'
        return redirect(url_for('personnel',message=f'Vaardigheid {activity} {state}.'))
    except Exception as e:
        return redirect(url_for('personnel',message=f'Vaardigheid aanpassen mislukt: {e}'))


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
        depots,vehicles,stock,resources,vehicle_materials=db.get_logistics()
        return render_template('fleet.html',depots=depots,vehicles=vehicles,stock=stock,resources=resources,
                               vehicle_materials=vehicle_materials,error=None,message=request.args.get('message'))
    except Exception as e:
        return render_template('fleet.html',depots=[],vehicles=[],stock=[],resources=[],vehicle_materials=[],error=str(e),message=None)

@app.post('/fleet/depot/save')
def fleet_depot_save():
    try:
        code=db.save_depot(request.form.get('code',''),request.form.get('name',''),request.form.get('address',''),request.form.get('active')=='on')
        return redirect(url_for('fleet',message=f'Standplaats {code} opgeslagen.'))
    except Exception as e:
        return redirect(url_for('fleet',message=f'Standplaats opslaan mislukt: {e}'))

@app.post('/fleet/vehicle/save')
@app.post('/fleet/save')
def fleet_save():
    try:
        code=(request.form.get('code') or '').strip()
        db.save_vehicle(code,request.form.get('depot_code',''),request.form.get('vehicle_type','bus'),
                        request.form.get('active')=='on',False,0,request.form.get('notes',''))
        return redirect(url_for('fleet',message=f'{code} opgeslagen.'))
    except Exception as e:
        return redirect(url_for('fleet',message=f'Voertuig opslaan mislukt: {e}'))

@app.post('/fleet/material/save')
def fleet_material_save():
    try:
        db.save_vehicle_material(request.form.get('vehicle_code',''),request.form.get('resource_code',''),
                                 request.form.get('capacity_persons','0'),request.form.get('active')=='on',request.form.get('notes',''))
        return redirect(url_for('fleet',message='Materiaal in voertuig opgeslagen.'))
    except Exception as e:
        return redirect(url_for('fleet',message=f'Materiaal opslaan mislukt: {e}'))

@app.post('/fleet/stock/save')
def fleet_stock_save():
    try:
        db.save_depot_stock(request.form.get('depot_code',''),request.form.get('resource_code',''),
                            request.form.get('quantity','0'),request.form.get('capacity_per_set','0'),request.form.get('notes',''))
        return redirect(url_for('fleet',message='Voorraad opgeslagen.'))
    except Exception as e:
        return redirect(url_for('fleet',message=f'Voorraad opslaan mislukt: {e}'))

@app.post('/fleet/stock/bulk-save')
def fleet_stock_bulk_save():
    try:
        depot_code=(request.form.get('depot_code') or '').strip()
        resource_codes=request.form.getlist('resource_code')
        quantities=request.form.getlist('quantity')
        capacities=request.form.getlist('capacity_per_set')
        notes=request.form.getlist('notes')
        rows=[]
        for idx, resource_code in enumerate(resource_codes):
            rows.append({
                'resource_code': resource_code,
                'quantity': quantities[idx] if idx < len(quantities) else '0',
                'capacity_per_set': capacities[idx] if idx < len(capacities) else '0',
                'notes': notes[idx] if idx < len(notes) else '',
            })
        db.save_depot_stock_bulk(depot_code, rows)
        return redirect(url_for('fleet',message='Extra voorraad voor deze standplaats opgeslagen. Nieuwe en herberekende planningen gebruiken direct deze voorraad.'))
    except Exception as e:
        return redirect(url_for('fleet',message=f'Voorraad opslaan mislukt: {e}'))

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
