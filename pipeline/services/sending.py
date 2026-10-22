"""Send queued emails from your own mailbox over SMTP, each at its send time.

Queueing emails on the Outbox page starts this as a background run (it can also be run by hand: `python manage.py
send_emails`). It takes the queued email with the earliest send time, waits until that time, sends it, records it
(outreach.record_sent: Sent, the lead moves on the pipeline, the next follow-up is written for review) and goes on.
When nothing is queued any more it stops; nothing runs on a timer. Emails queued while it waits are picked up.

The mailbox is set in .env: SMTP_HOST, SMTP_PORT, SMTP_USERNAME, SMTP_PASSWORD, SMTP_SECURITY (starttls, ssl or
none), SENDER_EMAIL (the From address) and SENDER_NAME. A follow-up is sent as a reply to the email before it
(In-Reply-To), so it lands in the same thread. Every email carries a List-Unsubscribe header (a reply to the sender).
"""
import smtplib
import time
from email.utils import formataddr, make_msgid

from django.conf import settings
from django.core.mail import EmailMultiAlternatives, get_connection
from django.core.management.base import CommandError

from pipeline.management.base import PARTIAL
from pipeline.models import Email
from pipeline.services import outreach

POLL_SECONDS = 30  # while waiting for the next send time: look again this often (new emails may be queued earlier)
MAX_WAIT_NOTE_EVERY = 600  # say again that it is waiting at most this often


def configured() -> bool:
    return bool(settings.EMAIL_HOST and settings.SENDER_EMAIL)


def missing_settings() -> str:
    names = [name for name, value in (("SMTP_HOST", settings.EMAIL_HOST), ("SENDER_EMAIL", settings.SENDER_EMAIL))
             if not value]
    return ", ".join(names)


def from_address() -> str:
    return formataddr((settings.SENDER_NAME, settings.SENDER_EMAIL)) if settings.SENDER_NAME else settings.SENDER_EMAIL


def build_message(email: Email, connection=None) -> EmailMultiAlternatives:
    """The email as it goes out: text and HTML (both with the footer), threaded under the previous email."""
    domain = settings.SENDER_EMAIL.rpartition("@")[2] or None
    headers = {
        "Message-ID": make_msgid(domain=domain),
        "List-Unsubscribe": f"<mailto:{settings.SENDER_EMAIL}?subject=unsubscribe>",
    }
    previous = (Email.objects.filter(prospect_id=email.prospect_id, sequence_step=email.sequence_step - 1)
                .values_list("message_id", flat=True).first())
    if previous:
        headers["In-Reply-To"] = previous
        headers["References"] = previous
    message = EmailMultiAlternatives(subject=email.subject, body=email.body_text, from_email=from_address(),
                                     to=[email.recipient], headers=headers, connection=connection)
    message.attach_alternative(email.body_html, "text/html")
    return message


def send_one(email: Email, connection) -> str:
    """Send one email. Returns its Message-ID."""
    message = build_message(email, connection)
    message.send(fail_silently=False)
    return message.extra_headers["Message-ID"]


def add_arguments(parser) -> None:
    parser.add_argument("--once", action="store_true",
                        help="Send only the queued emails whose time has come, then stop (do not wait for later ones).")


def recover_interrupted() -> int:
    """Emails a stopped sender left "sending": whether they went out is unknown, so they are marked failed (check
    your Sent folder before queueing them again)."""
    return Email.objects.filter(status="sending").update(
        status="failed", send_error="The sender stopped while sending it; check your Sent folder before queueing it "
                                    "again.")


def run(options: dict) -> int:
    if not configured():
        raise CommandError(f"Sending is not set up: set {missing_settings()} (and the other SMTP_ settings) in .env.")
    interrupted = recover_interrupted()
    if interrupted:
        print(f"{interrupted} email(s) were left half-sent by an earlier sender: marked failed (check your Sent folder).")
    sent = failed = 0
    connection = None
    waiting_for, last_note = None, 0.0
    try:
        while True:
            email = outreach.next_queued()
            if email is None:
                break
            wait = (email.scheduled_at - outreach.now()).total_seconds() if email.scheduled_at else 0
            if wait > 0:
                if options.get("once"):
                    break
                if connection is not None:  # do not keep an idle SMTP connection open for minutes
                    connection.close()
                    connection = None
                if waiting_for != email.pk or time.monotonic() - last_note > MAX_WAIT_NOTE_EVERY:
                    print(f"Waiting until {outreach.local_time(email)} for email {email.pk} to {email.recipient}.",
                          flush=True)
                    waiting_for, last_note = email.pk, time.monotonic()
                time.sleep(min(wait, POLL_SECONDS))
                continue
            if not Email.objects.filter(pk=email.pk, status="queued").update(status="sending"):
                continue  # unqueued or edited meanwhile
            email = Email.objects.select_related("prospect", "business__discovery_campaign").get(pk=email.pk)
            reason = outreach.can_be_sent(email)
            if reason:
                Email.objects.filter(pk=email.pk).update(status="failed", send_error=f"Not sent: {reason}.")
                failed += 1
                print(f"Email {email.pk} to {email.recipient}: not sent, {reason}.", flush=True)
                continue
            if connection is None:
                connection = get_connection(fail_silently=False)
                connection.open()
            try:
                message_id = send_one(email, connection)
            except smtplib.SMTPAuthenticationError as error:
                Email.objects.filter(pk=email.pk).update(status="queued")  # never left the machine: still queued
                raise CommandError(f"The mail server refused the login (SMTP_USERNAME / SMTP_PASSWORD): {error}") from error
            except (smtplib.SMTPException, OSError) as error:
                Email.objects.filter(pk=email.pk).update(status="failed", send_error=f"{type(error).__name__}: {error}"[:2000])
                failed += 1
                print(f"Email {email.pk} to {email.recipient}: failed ({type(error).__name__}: {error}).", flush=True)
                if connection is not None:
                    connection.close()
                    connection = None
                continue
            result = outreach.record_sent(email, message_id, settings.SENDER_EMAIL)
            sent += 1
            print(f"Sent email {email.pk} to {email.recipient}: {email.subject} (next: {result['next']}"
                  + (f"; {result['follow_up']}" if result.get("follow_up") else "") + ").", flush=True)
    finally:
        if connection is not None:
            connection.close()
    left = Email.objects.filter(status="queued").count()
    print(f"Done: {sent} sent, {failed} failed" + (f", {left} still queued (later times; run again or queue more to "
                                                    "start the sender)." if left else ", the queue is empty."))
    return PARTIAL if failed else 0
