"""Send approved emails through SMTP (Hostinger by default) and mark them sent, so open tracking starts counting.

By default this only previews what would be sent. Pass `--send` to actually send. Emails are approved in the review
dashboard (`python -m dashboard`); anything still in review or rejected is never sent.

Safety:
- Only approved emails whose prospect is still ready (deliverable, `outreach_status = 'ready'`, not
  `do_not_contact`) are sent.
- Each email is claimed (`approved` -> `sending`) before it is sent, so two runs can never send the same email.
- Emails that still contain placeholder text, or a run while the prompt/footer constants are placeholders, are
  refused unless `--allow-placeholders` is given.
- A rejected recipient marks that email `failed`; a connection/login/temporary error puts the email back to
  `approved` and stops the run. An email left in `sending` (the process died mid-send) is never retried
  automatically, because it may already have been delivered.
"""
import argparse
import os
import smtplib
import socket
import ssl
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import MetaData, Table, create_engine, func, select, update
from sqlalchemy.exc import NoSuchTableError, SQLAlchemyError

import generate_emails

DEFAULT_SMTP_HOST = "smtp.hostinger.com"
DEFAULT_SMTP_PORT = 465
DEFAULT_DELAY = 20.0  # seconds between sends; keeps well under Hostinger's sending limits
PLACEHOLDER_MARKER = "PLACEHOLDER"


class SendStop(RuntimeError):
    """A problem with the connection or account; the run stops and the email goes back to approved."""


def smtp_config(args) -> dict:
    port = int(os.environ.get("SMTP_PORT", "").strip() or DEFAULT_SMTP_PORT)
    security = (os.environ.get("SMTP_SECURITY", "").strip() or ("ssl" if port == 465 else "starttls")).lower()
    username = os.environ.get("SMTP_USERNAME", "").strip()
    return {
        "host": os.environ.get("SMTP_HOST", "").strip() or DEFAULT_SMTP_HOST,
        "port": port,
        "security": security,
        "username": username,
        "password": os.environ.get("SMTP_PASSWORD", ""),
        "from_email": os.environ.get("SMTP_FROM_EMAIL", "").strip() or username,
        "from_name": os.environ.get("SMTP_FROM_NAME", "").strip(),
        "reply_to": os.environ.get("SMTP_REPLY_TO", "").strip(),
        "timeout": args.timeout,
    }


def config_problems(config: dict) -> list[str]:
    problems = []
    if config["security"] not in ("ssl", "starttls", "none"):
        problems.append("SMTP_SECURITY must be ssl, starttls or none")
    for key, name in (("username", "SMTP_USERNAME"), ("password", "SMTP_PASSWORD"), ("from_email", "SMTP_FROM_EMAIL")):
        if not config[key]:
            problems.append(f"{name} is not set")
    if config["from_email"] and "@" not in config["from_email"]:
        problems.append("SMTP_FROM_EMAIL is not an email address")
    if PLACEHOLDER_MARKER in config["from_name"]:
        problems.append("SMTP_FROM_NAME is still a placeholder")
    return problems


def build_message(email: dict, config: dict) -> EmailMessage:
    message = EmailMessage()
    sender = config["from_email"]
    message["From"] = formataddr((config["from_name"], sender)) if config["from_name"] else sender
    message["To"] = email["recipient"]
    message["Subject"] = email["subject"]
    message["Date"] = formatdate(localtime=False, usegmt=True)
    message["Message-ID"] = make_msgid(domain=sender.rpartition("@")[2] or None)
    if config["reply_to"]:
        message["Reply-To"] = config["reply_to"]
    # Lets mail clients show an unsubscribe button; it opens a reply asking to unsubscribe.
    message["List-Unsubscribe"] = f"<mailto:{config['reply_to'] or sender}?subject=unsubscribe>"
    message.set_content(email["body_text"])
    message.add_alternative(email["body_html"], subtype="html")
    return message


def has_placeholder(email: dict) -> bool:
    return any(PLACEHOLDER_MARKER in (email.get(field) or "") for field in ("subject", "body_text", "body_html"))


class Mailer:
    """One SMTP connection, opened on first use and reopened if the server drops it."""

    def __init__(self, config: dict):
        self.config = config
        self.server: smtplib.SMTP | None = None

    def _connect(self) -> smtplib.SMTP:
        c = self.config
        if c["security"] == "ssl":
            server = smtplib.SMTP_SSL(c["host"], c["port"], timeout=c["timeout"], context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(c["host"], c["port"], timeout=c["timeout"])
            if c["security"] == "starttls":
                server.starttls(context=ssl.create_default_context())
        server.login(c["username"], c["password"])
        return server

    def send(self, message: EmailMessage) -> None:
        for attempt in range(2):
            try:
                if self.server is None:
                    self.server = self._connect()
                self.server.send_message(message)
                return
            except smtplib.SMTPServerDisconnected:
                self.server = None
                if attempt:
                    raise

    def close(self) -> None:
        if self.server is not None:
            try:
                self.server.quit()
            except (smtplib.SMTPException, OSError):
                pass
            self.server = None


def classify_send_error(error: Exception) -> tuple[str, str]:
    """('failed', reason) when this recipient was rejected; ('stop', reason) when the problem is ours or temporary."""
    if isinstance(error, smtplib.SMTPRecipientsRefused):
        code, reply = next(iter(error.recipients.values()))
        reply = reply.decode("utf-8", "replace") if isinstance(reply, bytes) else str(reply)
        return ("failed" if code >= 500 else "stop"), f"recipient refused: {code} {reply}"[:1000]
    if isinstance(error, smtplib.SMTPAuthenticationError):
        return "stop", "SMTP login failed: check SMTP_USERNAME/SMTP_PASSWORD"
    if isinstance(error, smtplib.SMTPResponseException):
        reply = error.smtp_error.decode("utf-8", "replace") if isinstance(error.smtp_error, bytes) else str(error.smtp_error)
        return ("failed" if error.smtp_code >= 500 and isinstance(error, smtplib.SMTPDataError) else "stop"), \
            f"SMTP error {error.smtp_code}: {reply}"[:1000]
    if isinstance(error, (OSError, smtplib.SMTPException, socket.timeout, ssl.SSLError)):
        return "stop", f"{type(error).__name__}: {error}"[:1000]
    return "stop", f"{type(error).__name__}: {error}"[:1000]


def send_one(engine, emails: Table, prospects: Table, email: dict, config: dict, mailer: "Mailer") -> tuple[str, str]:
    """Claim, send and record one approved email. Returns (outcome, detail):

    ("sent", message_id) | ("failed", reason: this recipient/content was rejected) |
    ("stop", reason: login, connection or temporary problem; the email is back in approved) |
    ("skipped", reason: it was no longer approved, e.g. another run or the dashboard took it).
    """
    with engine.begin() as connection:
        claimed = connection.execute(
            update(emails)
            .where(emails.c.id == email["id"], emails.c.status == "approved")
            .values(status="sending", sent_from=config["from_email"], send_error=None)
            .returning(emails.c.id)
        ).first()
    if not claimed:
        return "skipped", "no longer approved (another run took it, or it was changed)"
    message = build_message(email, config)
    try:
        mailer.send(message)
    except Exception as error:  # noqa: BLE001 - every outcome must be written back
        outcome, reason = classify_send_error(error)
        with engine.begin() as connection:
            connection.execute(
                update(emails).where(emails.c.id == email["id"])
                .values(status="failed" if outcome == "failed" else "approved", send_error=reason)
            )
        return outcome, reason
    now = datetime.now(timezone.utc)
    with engine.begin() as connection:
        connection.execute(
            update(emails).where(emails.c.id == email["id"])
            .values(status="sent", sent_at=now, message_id=message["Message-ID"])
        )
        connection.execute(
            update(prospects).where(prospects.c.id == email["prospect_id"])
            .values(last_contacted_at=now, outreach_status="contacted")
        )
    return "sent", message["Message-ID"]


def load_approved(connection, emails: Table, prospects: Table, args) -> list[dict]:
    statement = (
        select(
            emails.c.id, emails.c.prospect_id, emails.c.business_id, emails.c.recipient, emails.c.subject,
            emails.c.body_text, emails.c.body_html, emails.c.tracking_token,
        )
        .join(prospects, prospects.c.id == emails.c.prospect_id)
        .where(
            emails.c.status == "approved",
            prospects.c.email_status == "deliverable",
            prospects.c.outreach_status == "ready",
            prospects.c.do_not_contact.is_(False),
            func.lower(prospects.c.email) == func.lower(emails.c.recipient),
        )
        .order_by(emails.c.id)
    )
    if args.email_id is not None:
        statement = statement.where(emails.c.id == args.email_id)
    if args.business_id is not None:
        statement = statement.where(emails.c.business_id == args.business_id)
    if args.limit is not None:
        statement = statement.limit(args.limit)
    return [dict(row._mapping) for row in connection.execute(statement)]


def main() -> int:
    parser = argparse.ArgumentParser(description="Send approved emails via SMTP. Previews only unless --send is given.")
    parser.add_argument("--send", action="store_true", help="Actually send. Without it, nothing is sent or changed.")
    parser.add_argument("--email-id", type=int, help="Send one email only.")
    parser.add_argument("--business-id", type=int, help="Send one business's emails only.")
    parser.add_argument("--limit", type=int, help="Maximum number of emails in this run.")
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY, help="Seconds between sends (default: 20).")
    parser.add_argument("--timeout", type=float, default=30.0, help="SMTP timeout in seconds (default: 30).")
    parser.add_argument("--allow-placeholders", action="store_true", help="Send even if placeholder text remains.")
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be a positive integer")
    if args.delay < 0 or args.timeout <= 0:
        parser.error("--delay cannot be negative and --timeout must be > 0")

    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        parser.error("DATABASE_URL is not set in the environment or project .env file")
    config = smtp_config(args)
    if args.send:
        problems = config_problems(config)
        if problems:
            parser.error("; ".join(problems))
        if not args.allow_placeholders and (generate_emails.PROMPT_IS_PLACEHOLDER or generate_emails.FOOTER_IS_PLACEHOLDER):
            parser.error("The email prompt or footer in generate_emails.py is still a placeholder. Fill them in "
                         "(and set PROMPT_IS_PLACEHOLDER/FOOTER_IS_PLACEHOLDER = False) or pass --allow-placeholders.")

    engine = create_engine(database_url, pool_pre_ping=True)
    metadata = MetaData()
    mailer = Mailer(config)
    try:
        try:
            emails = Table("emails", metadata, autoload_with=engine)
            prospects = Table("prospects", metadata, autoload_with=engine)
        except NoSuchTableError as error:
            print(f"Table {error} does not exist. Run `alembic upgrade head` first.")
            return 1
        with engine.connect() as connection:
            stuck = connection.execute(select(emails.c.id).where(emails.c.status == "sending")).scalars().all()
            queue = load_approved(connection, emails, prospects, args)
        if stuck:
            print(f"Warning: email(s) {', '.join(map(str, stuck))} are stuck in 'sending' (a previous run stopped "
                  "mid-send). They may have been delivered; check the mailbox's Sent folder, then set their status by hand.")
        if not queue:
            print("No approved emails ready to send.")
            return 0

        mode = "Sending" if args.send else "Preview (nothing is sent; pass --send to send)"
        sender = formataddr((config["from_name"], config["from_email"])) if config["from_name"] else config["from_email"]
        print(f"{mode}: {len(queue)} email(s) from {sender or '(SMTP_FROM_EMAIL not set)'} via {config['host']}:{config['port']}.")
        sent = failed = skipped = 0
        stopped = False
        for index, email in enumerate(queue):
            if has_placeholder(email) and not args.allow_placeholders:
                skipped += 1
                print(f"  Email {email['id']} <{email['recipient']}> skipped: contains placeholder text (regenerate it).")
                continue
            if not args.send:
                print(f"  Email {email['id']} -> {email['recipient']}: {email['subject']}")
                continue
            if sent or failed:
                time.sleep(args.delay)
            outcome, detail = send_one(engine, emails, prospects, email, config, mailer)
            if outcome == "skipped":
                skipped += 1
                print(f"  Email {email['id']} skipped: {detail}")
            elif outcome == "failed":
                failed += 1
                print(f"  Email {email['id']} <{email['recipient']}> FAILED: {detail}")
            elif outcome == "stop":
                print(f"  Email {email['id']} <{email['recipient']}> not sent: {detail}. Stopping; it is back in approved.")
                stopped = True
                break
            else:
                sent += 1
                print(f"  Email {email['id']} sent to {email['recipient']}: {email['subject']}")

        if args.send:
            print(f"Done: {sent} sent, {failed} failed, {skipped} skipped" + (" (stopped early)." if stopped else "."))
        else:
            print(f"Preview done: {len(queue) - skipped} would be sent, {skipped} skipped.")
        return 1 if stopped else 0
    except SQLAlchemyError as error:
        print(f"Database operation failed ({type(error).__name__}): {str(error).splitlines()[0]}")
        print("Check DATABASE_URL and run `alembic upgrade head` first.")
        return 1
    finally:
        mailer.close()
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
