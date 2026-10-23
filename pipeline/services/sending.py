"""Send queued emails from your mailboxes over SMTP, each at its send time.

Queueing emails on the Outbox page starts this as a background run (it can also be run by hand: `python manage.py
send_emails`). It takes the queued email with the earliest send time, waits until that time, sends it from the
mailbox it was given (outreach.schedule picks the first mailbox with room, then the next), records it
(outreach.record_sent: Sent, the lead moves on the pipeline, the next follow-up is written for review) and goes on.
When nothing is queued any more it stops; nothing runs on a timer. Emails queued while it waits are picked up.

Mailboxes are kept in the database (Settings > Mailboxes: server, port, security, login, from address and name); their
passwords are encrypted with MAILBOX_ENCRYPTION_KEY from .env. A follow-up is sent as a reply to the email before it
(In-Reply-To), so it lands in the same thread. Every email carries a List-Unsubscribe header (a reply to the sender).
A mailbox that refuses its login is noted on the mailbox; its emails stay queued and the others keep going.
"""
import smtplib
import time
from datetime import datetime, timezone
from email.utils import formataddr, make_msgid

from django.core.mail import EmailMultiAlternatives, get_connection
from django.core.management.base import CommandError

from pipeline.management.base import PARTIAL
from pipeline.models import Email, Mailbox
from pipeline.services import outreach, secrets_box

POLL_SECONDS = 30  # while waiting for the next send time: look again this often (new emails may be queued earlier)
MAX_WAIT_NOTE_EVERY = 600  # say again that it is waiting at most this often


def configured() -> bool:
    return secrets_box.configured() and Mailbox.objects.filter(is_active=True).exists()


def missing_settings() -> str:
    """What is missing before anything can be sent, in a few words."""
    problems = []
    if not Mailbox.objects.filter(is_active=True).exists():
        problems.append("an active mailbox (Settings > Mailboxes)")
    if not secrets_box.configured():
        problems.append(f"{secrets_box.ENV_NAME} in .env")
    return " and ".join(problems)


def from_address(mailbox: Mailbox) -> str:
    return formataddr((mailbox.from_name, mailbox.from_email)) if mailbox.from_name else mailbox.from_email


def connect(mailbox: Mailbox, timeout: float = 60):
    """An SMTP connection for the mailbox (not opened yet)."""
    return get_connection(fail_silently=False, host=mailbox.host, port=mailbox.port, username=mailbox.username,
                          password=mailbox.get_password(), use_tls=mailbox.security == "starttls",
                          use_ssl=mailbox.security == "ssl", timeout=timeout)


def test_login(mailbox: Mailbox) -> str | None:
    """Connect and log in without sending anything. None when it works, else what went wrong."""
    connection = connect(mailbox, timeout=20)
    try:
        connection.open()
        connection.close()
        return None
    except (smtplib.SMTPException, OSError, secrets_box.SecretsError) as error:
        return f"{type(error).__name__}: {error}"


def build_message(email: Email, mailbox: Mailbox, connection=None) -> EmailMultiAlternatives:
    """The email as it goes out: text and HTML (both with the footer), threaded under the previous email."""
    domain = mailbox.from_email.rpartition("@")[2] or None
    headers = {
        "Message-ID": make_msgid(domain=domain),
        "List-Unsubscribe": f"<mailto:{mailbox.from_email}?subject=unsubscribe>",
    }
    previous = (Email.objects.filter(prospect_id=email.prospect_id, sequence_step=email.sequence_step - 1)
                .values_list("message_id", flat=True).first())
    if previous:
        headers["In-Reply-To"] = previous
        headers["References"] = previous
    message = EmailMultiAlternatives(subject=email.subject, body=email.body_text, from_email=from_address(mailbox),
                                     to=[email.recipient], headers=headers, connection=connection)
    message.attach_alternative(email.body_html, "text/html")
    return message


def send_one(email: Email, mailbox: Mailbox, connection) -> str:
    """Send one email. Returns its Message-ID."""
    message = build_message(email, mailbox, connection)
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
        raise CommandError(f"Sending is not set up: add {missing_settings()}.")
    interrupted = recover_interrupted()
    if interrupted:
        print(f"{interrupted} email(s) were left half-sent by an earlier sender: marked failed (check your Sent folder).")
    sent = failed = 0
    connections: dict[int, object] = {}
    refused: dict[int, str] = {}  # mailbox id -> why it cannot send in this run
    waiting_for, last_note = None, 0.0

    def close_all():
        for connection in connections.values():
            connection.close()
        connections.clear()

    try:
        while True:
            email = outreach.next_queued(skip_mailboxes=refused)
            if email is None:
                break
            wait = (email.scheduled_at - outreach.now()).total_seconds() if email.scheduled_at else 0
            if wait > 0:
                if options.get("once"):
                    break
                close_all()  # do not keep idle SMTP connections open for minutes
                if waiting_for != email.pk or time.monotonic() - last_note > MAX_WAIT_NOTE_EVERY:
                    print(f"Waiting until {outreach.local_time(email)} for email {email.pk} to {email.recipient}.",
                          flush=True)
                    waiting_for, last_note = email.pk, time.monotonic()
                time.sleep(min(wait, POLL_SECONDS))
                continue
            if not Email.objects.filter(pk=email.pk, status="queued").update(status="sending"):
                continue  # unqueued or edited meanwhile
            email = Email.objects.select_related("prospect", "business__discovery_campaign", "mailbox").get(pk=email.pk)
            reason = outreach.can_be_sent(email)
            mailbox = email.mailbox
            if not reason and (mailbox is None or not mailbox.is_active):
                reason = "its mailbox was removed or turned off; queue it again to give it another"
            if reason:
                Email.objects.filter(pk=email.pk).update(status="failed", send_error=f"Not sent: {reason}.")
                failed += 1
                print(f"Email {email.pk} to {email.recipient}: not sent, {reason}.", flush=True)
                continue
            try:
                if mailbox.pk not in connections:
                    connections[mailbox.pk] = connect(mailbox)
                    connections[mailbox.pk].open()
                message_id = send_one(email, mailbox, connections[mailbox.pk])
            except (smtplib.SMTPAuthenticationError, secrets_box.SecretsError) as error:
                Email.objects.filter(pk=email.pk).update(status="queued")  # never left the machine: still queued
                problem = f"{type(error).__name__}: {error}"[:2000]
                Mailbox.objects.filter(pk=mailbox.pk).update(last_error=f"Login refused: {problem}")
                refused[mailbox.pk] = problem
                connections.pop(mailbox.pk, None)
                print(f"Mailbox {mailbox}: the server refused the login ({problem}). Its emails stay queued; the other "
                      "mailboxes go on.", flush=True)
                continue
            except (smtplib.SMTPException, OSError) as error:
                Email.objects.filter(pk=email.pk).update(status="failed", send_error=f"{type(error).__name__}: {error}"[:2000])
                failed += 1
                print(f"Email {email.pk} to {email.recipient} from {mailbox.name}: failed ({type(error).__name__}: "
                      f"{error}).", flush=True)
                connection = connections.pop(mailbox.pk, None)
                if connection is not None:
                    connection.close()
                continue
            Mailbox.objects.filter(pk=mailbox.pk).update(last_used_at=datetime.now(timezone.utc), last_error=None)
            result = outreach.record_sent(email, message_id, mailbox.from_email)
            sent += 1
            print(f"Sent email {email.pk} to {email.recipient} from {mailbox.name}: {email.subject} (next: "
                  f"{result['next']}" + (f"; {result['follow_up']}" if result.get("follow_up") else "") + ").", flush=True)
    finally:
        close_all()
    left = Email.objects.filter(status="queued").count()
    print(f"Done: {sent} sent, {failed} failed" + (f", {left} still queued" if left else ", the queue is empty") + ".")
    if refused:
        names = ", ".join(str(box) for box in Mailbox.objects.filter(pk__in=refused))
        raise CommandError(f"These mailboxes refused the login, so their emails are still queued: {names}. Fix the "
                           "password under Settings > Mailboxes, then click Start the sender.")
    return PARTIAL if failed else 0
