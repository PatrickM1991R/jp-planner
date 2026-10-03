import os
import smtplib
from email.message import EmailMessage


def configured():
    return bool(os.getenv('MAIL_HOST') and os.getenv('MAIL_FROM'))


def send_mail(to_address, subject, body):
    to_address=(to_address or '').strip()
    if not to_address:
        return False, 'Geen e-mailadres ingesteld.'
    host=os.getenv('MAIL_HOST','').strip()
    sender=os.getenv('MAIL_FROM','').strip()
    if not host or not sender:
        return False, 'MAIL_HOST of MAIL_FROM ontbreekt.'
    port=int(os.getenv('MAIL_PORT','587') or 587)
    username=os.getenv('MAIL_USERNAME','').strip()
    password=os.getenv('MAIL_PASSWORD','')
    use_tls=(os.getenv('MAIL_USE_TLS','1').strip().lower() not in {'0','false','nee','no'})
    msg=EmailMessage()
    msg['From']=sender
    msg['To']=to_address
    msg['Subject']=subject
    msg.set_content(body)
    try:
        with smtplib.SMTP(host,port,timeout=20) as smtp:
            if use_tls:
                smtp.starttls()
            if username:
                smtp.login(username,password)
            smtp.send_message(msg)
        return True, ''
    except Exception as exc:
        return False, str(exc)


def base_url():
    return (os.getenv('APP_BASE_URL') or 'https://jp-planner-uakc.onrender.com').rstrip('/')


def planner_email():
    return (os.getenv('PLANNER_EMAIL') or '').strip()
