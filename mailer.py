# mailer.py
from flask import current_app
from flask_mail import Message
from app import mail

def send_email(subject: str, recipients: list[str], html: str):
    try:
        msg = Message(subject=subject, recipients=recipients)
        msg.html = html
        mail.send(msg)
        return True
    except Exception as e:
        # Dev fallback: print the email so you can copy link
        print("\n=== EMAIL (DEV FALLBACK) ===")
        print("To:", recipients)
        print("Subj:", subject)
        print(html)
        print("=== END EMAIL ===\n")
        return False