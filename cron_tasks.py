from datetime import datetime
from zoneinfo import ZoneInfo
import db
import mail_service


def run():
    now=datetime.now(ZoneInfo('Europe/Amsterdam'))
    now_iso=now.isoformat()
    sent=0
    failed=0
    for sh in db.shifts_due_for_reminder(now_iso):
        if not sh.get('employee_email'):
            continue
        link=f"{mail_service.base_url()}/staff/hours/{sh['employee_token']}"
        subject=f"JP Activiteiten - controleer je uren van {sh['work_date']}"
        body=(
            f"Hoi {sh['employee_name']},\n\n"
            f"Je dienst van {sh['work_date']} is afgelopen. Controleer binnen 24 uur je uren.\n\n"
            f"Gepland: {sh['planned_start']} - {sh['planned_end']}\n"
            f"Pauze: {sh['planned_break_minutes']} minuten (eigen tijd)\n"
            f"Betaalde werktijd: {sh['planned_net_minutes']} minuten\n\n"
            f"Bevestigen of aanpassen: {link}\n\n"
            "Als je binnen 24 uur niets aanpast, worden de systeemuren automatisch akkoord gezet.\n"
        )
        ok,_=mail_service.send_mail(sh['employee_email'],subject,body)
        if ok:
            db.mark_shift_reminder_sent(sh['id']); sent+=1
        else:
            failed+=1
    approved=db.auto_approve_expired_shifts(now_iso)
    print(f"Urencron klaar: reminders={sent}, mailfouten={failed}, automatisch akkoord={approved}")


if __name__=='__main__':
    run()
