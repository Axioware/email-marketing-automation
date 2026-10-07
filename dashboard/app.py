"""Local review dashboard for generated outreach emails.

Run with `python -m dashboard` and open http://127.0.0.1:8001. Review each email, edit it, approve or reject it,
regenerate it with the LLM, and send approved ones (with confirmation) through the SMTP settings in `.env`.

It only listens on 127.0.0.1. Because any web page open in the same browser could still send requests to
localhost, every form carries a per-process secret token and requests whose Host header is not this machine are
refused (protection against cross-site requests and DNS rebinding).
"""
import os
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

from dotenv import load_dotenv
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import MetaData, Table, create_engine, func, or_, select, update

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import generate_emails  # noqa: E402
import send_emails  # noqa: E402

DEFAULT_PORT = 8001
EDITABLE = ("in_review", "approved", "rejected")
STATUS_ORDER = ("in_review", "approved", "rejected", "sent", "opened", "failed", "sending")
STATUS_LABELS = {
    "in_review": "In review", "approved": "Approved", "rejected": "Rejected", "sending": "Sending",
    "sent": "Sent", "opened": "Opened", "failed": "Failed",
}
PAGE_SIZE = 50
MAX_SUBJECT = generate_emails.MAX_SUBJECT_CHARS
MAX_BODY = generate_emails.MAX_BODY_CHARS


def create_app(database_url: str | None = None, port: int = DEFAULT_PORT) -> FastAPI:
    load_dotenv(ROOT / ".env")
    database_url = database_url or os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is not set in the environment or project .env file")

    app = FastAPI(title="Email review", docs_url=None, redoc_url=None, openapi_url=None)
    templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
    engine = create_engine(database_url, pool_pre_ping=True)
    csrf_token = secrets.token_urlsafe(32)
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}", "127.0.0.1", "localhost", "testserver"}
    state = SimpleNamespace(tables=None)

    def tables() -> dict[str, Table]:
        if state.tables is None:
            metadata = MetaData()
            state.tables = {
                name: Table(name, metadata, autoload_with=engine)
                for name in ("emails", "prospects", "businesses", "business_contacts", "business_website_profiles",
                             "email_open_events")
            }
        return state.tables

    def base_url() -> str | None:
        return generate_emails.tracking_base_url(database_url)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.headers.get("host", "") not in allowed_hosts:
            return PlainTextResponse("Forbidden host", status_code=403)
        response = await call_next(request)
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    def check_csrf(token: str) -> None:
        if not secrets.compare_digest(token or "", csrf_token):
            raise HTTPException(status_code=403, detail="Invalid form token; reload the page and try again.")

    def redirect(path: str, **params) -> RedirectResponse:
        params = {k: v for k, v in params.items() if v not in (None, "")}
        return RedirectResponse(f"{path}?{urlencode(params)}" if params else path, status_code=303)

    def render(request: Request, name: str, **context) -> HTMLResponse:
        return templates.TemplateResponse(
            request, name,
            {"csrf": csrf_token, "labels": STATUS_LABELS, "msg": request.query_params.get("msg"),
             "error": request.query_params.get("error"), **context},
        )

    # ------------------------------------------------------------------ data

    def status_counts() -> dict[str, int]:
        emails = tables()["emails"]
        with engine.connect() as c:
            rows = c.execute(select(emails.c.status, func.count()).group_by(emails.c.status)).all()
        counts = {status: 0 for status in STATUS_ORDER}
        counts.update({status: n for status, n in rows})
        return counts

    def load_email(email_id: int) -> dict:
        t = tables()
        e, p, b, ct, prof = t["emails"], t["prospects"], t["businesses"], t["business_contacts"], t["business_website_profiles"]
        statement = (
            select(
                e, p.c.email_status.label("prospect_email_status"), p.c.outreach_status, p.c.do_not_contact,
                p.c.outreach_facts, p.c.research_summary, p.c.qualification_score,
                b.c.name.label("business_name"), b.c.website_url, b.c.city, b.c.country, b.c.google_rating,
                ct.c.name.label("contact_name"), ct.c.first_name, ct.c.job_title,
                prof.c.qualification_reasons, prof.c.scraped_pages,
            )
            .join(p, p.c.id == e.c.prospect_id)
            .join(b, b.c.id == e.c.business_id)
            .join(ct, ct.c.id == e.c.contact_id)
            .outerjoin(prof, prof.c.business_id == e.c.business_id)
            .where(e.c.id == email_id)
        )
        with engine.connect() as c:
            row = c.execute(statement).first()
        if row is None:
            raise HTTPException(status_code=404, detail="Email not found")
        return dict(row._mapping)

    def next_in_review(after_id: int) -> int | None:
        emails = tables()["emails"]
        with engine.connect() as c:
            following = c.execute(
                select(emails.c.id).where(emails.c.status == "in_review", emails.c.id > after_id).order_by(emails.c.id).limit(1)
            ).scalar()
            if following is None:
                following = c.execute(
                    select(emails.c.id).where(emails.c.status == "in_review", emails.c.id != after_id).order_by(emails.c.id).limit(1)
                ).scalar()
        return following

    def set_status(email_id: int, allowed_from: tuple[str, ...], **values) -> bool:
        emails = tables()["emails"]
        with engine.begin() as c:
            changed = c.execute(
                update(emails).where(emails.c.id == email_id, emails.c.status.in_(allowed_from)).values(**values)
                .returning(emails.c.id)
            ).first()
        return changed is not None

    # ------------------------------------------------------------------ pages

    @app.get("/", response_class=HTMLResponse)
    def home():
        return redirect("/emails", status="in_review")

    @app.get("/emails", response_class=HTMLResponse)
    def list_emails(request: Request, status: str = "in_review", q: str = "", page: int = 1):
        t = tables()
        e, b = t["emails"], t["businesses"]
        statement = (
            select(e.c.id, e.c.recipient, e.c.subject, e.c.status, e.c.generated_at, e.c.sent_at, e.c.open_count,
                   e.c.edited_at, b.c.name.label("business_name"))
            .join(b, b.c.id == e.c.business_id)
            .order_by(e.c.id)
        )
        if status != "all":
            statement = statement.where(e.c.status == status)
        if q.strip():
            like = f"%{q.strip()}%"
            statement = statement.where(or_(b.c.name.ilike(like), e.c.recipient.ilike(like), e.c.subject.ilike(like)))
        page = max(page, 1)
        with engine.connect() as c:
            rows = [dict(r._mapping) for r in c.execute(statement.limit(PAGE_SIZE + 1).offset((page - 1) * PAGE_SIZE))]
        counts = status_counts()
        sent_total = counts["sent"] + counts["opened"]
        return render(
            request, "list.html", emails=rows[:PAGE_SIZE], has_more=len(rows) > PAGE_SIZE, page=page, status=status,
            q=q, counts=counts, order=STATUS_ORDER, total=sum(counts.values()), sent_total=sent_total,
            open_rate=round(100 * counts["opened"] / sent_total) if sent_total else None,
        )

    @app.get("/emails/{email_id}", response_class=HTMLResponse)
    def show_email(request: Request, email_id: int):
        email = load_email(email_id)
        body = generate_emails.editable_body(email["body_text"])
        preview = generate_emails.render_html(body, generate_emails.footer_preview_url(base_url()) or "")
        events = []
        if email["open_count"]:
            ev = tables()["email_open_events"]
            with engine.connect() as c:
                events = [dict(r._mapping) for r in c.execute(
                    select(ev.c.opened_at, ev.c.user_agent).where(ev.c.email_id == email_id).order_by(ev.c.opened_at.desc()).limit(20))]
        smtp = send_emails.smtp_config(SimpleNamespace(timeout=30))
        return render(
            request, "email.html", email=email, body=body, preview=preview, events=events,
            findings=generate_emails.website_findings(email["scraped_pages"]),
            editable=email["status"] in EDITABLE, has_placeholder=send_emails.has_placeholder(email),
            smtp_problems=send_emails.config_problems(smtp), sender=smtp["from_email"],
            prospect_ready=(email["prospect_email_status"] == "deliverable" and email["outreach_status"] == "ready"
                            and not email["do_not_contact"]),
        )

    # ------------------------------------------------------------------ actions

    @app.post("/emails/{email_id}/save")
    def save(email_id: int, subject: str = Form(...), body: str = Form(...), csrf: str = Form("")):
        check_csrf(csrf)
        subject, body = " ".join(subject.split()), body.replace("\r\n", "\n").strip()
        if not subject or not body:
            return redirect(f"/emails/{email_id}", error="Subject and body cannot be empty.")
        if len(subject) > MAX_SUBJECT or len(body) > MAX_BODY:
            return redirect(f"/emails/{email_id}", error=f"Keep the subject under {MAX_SUBJECT} and the body under {MAX_BODY} characters.")
        email = load_email(email_id)
        url = base_url()
        if not url:
            return redirect(f"/emails/{email_id}", error="Cannot build the tracking URL; set EMAIL_TRACKING_BASE_URL.")
        changed = set_status(
            email_id, EDITABLE,
            subject=subject,
            body_text=generate_emails.render_text(body),
            body_html=generate_emails.render_html(body, generate_emails.tracking_url(url, email["tracking_token"])),
            edited_at=datetime.now(timezone.utc),
            status="in_review", reviewed_at=None,  # an edited email always needs a fresh approval
        )
        if not changed:
            return redirect(f"/emails/{email_id}", error="This email can no longer be edited.")
        note = " It went back to review." if email["status"] != "in_review" else ""
        return redirect(f"/emails/{email_id}", msg="Saved." + note)

    @app.post("/emails/{email_id}/approve")
    def approve(email_id: int, csrf: str = Form(""), stay: str = Form("")):
        check_csrf(csrf)
        email = load_email(email_id)
        if send_emails.has_placeholder(email):
            return redirect(f"/emails/{email_id}", error="This email still contains placeholder text; edit it first.")
        if not set_status(email_id, ("in_review",), status="approved", reviewed_at=datetime.now(timezone.utc), review_note=None):
            return redirect(f"/emails/{email_id}", error="Only emails in review can be approved.")
        following = None if stay else next_in_review(email_id)
        if following:
            return redirect(f"/emails/{following}", msg=f"Email {email_id} approved. Next in review:")
        return redirect(f"/emails/{email_id}", msg="Approved.")

    @app.post("/emails/{email_id}/reject")
    def reject(email_id: int, note: str = Form(""), csrf: str = Form("")):
        check_csrf(csrf)
        if not set_status(email_id, ("in_review", "approved"), status="rejected",
                          reviewed_at=datetime.now(timezone.utc), review_note=note.strip()[:2000] or None):
            return redirect(f"/emails/{email_id}", error="Only emails in review or approved can be rejected.")
        following = next_in_review(email_id)
        if following:
            return redirect(f"/emails/{following}", msg=f"Email {email_id} rejected. Next in review:")
        return redirect(f"/emails/{email_id}", msg="Rejected.")

    @app.post("/emails/{email_id}/reopen")
    def reopen(email_id: int, csrf: str = Form("")):
        check_csrf(csrf)
        if not set_status(email_id, ("rejected", "approved"), status="in_review", reviewed_at=None):
            return redirect(f"/emails/{email_id}", error="Only rejected or approved emails can go back to review.")
        return redirect(f"/emails/{email_id}", msg="Back in review.")

    @app.post("/emails/{email_id}/regenerate")
    def regenerate(email_id: int, csrf: str = Form("")):
        check_csrf(csrf)
        email = load_email(email_id)
        if email["status"] not in generate_emails.REGENERATABLE_STATUSES:
            return redirect(f"/emails/{email_id}", error="Only emails in review or rejected can be regenerated.")
        llm = generate_emails.make_llm_client()
        url = base_url()
        if llm is None or not url:
            return redirect(f"/emails/{email_id}", error="Set an LLM key (GROQ_API_KEY or OPENAI_API_KEY) and the tracking URL.")
        client, model, provider = llm
        args = SimpleNamespace(prospect_id=email["prospect_id"], business_id=None, limit=None, regenerate=True)
        with engine.connect() as c:
            rows = generate_emails.load_prospects(c, tables(), args)
        if not rows:
            return redirect(f"/emails/{email_id}", error="The prospect is no longer ready for outreach (check its status).")
        row = rows[0]
        try:
            draft = generate_emails.generate_draft(client, model, generate_emails.prospect_context(row))
        except Exception as error:  # noqa: BLE001 - show any model/API failure to the reviewer
            return redirect(f"/emails/{email_id}", error=f"Regeneration failed ({type(error).__name__}): {error}"[:300])
        body_html = generate_emails.render_html(draft.body, generate_emails.tracking_url(url, row["tracking_token"]))
        with engine.begin() as c:
            generate_emails.save_draft(c, tables()["emails"], row, draft, body_html, row["tracking_token"], provider,
                                       model, datetime.now(timezone.utc))
        return redirect(f"/emails/{email_id}", msg=f"Regenerated with {provider} ({model}).")

    @app.post("/emails/{email_id}/send")
    def send(email_id: int, confirm: str = Form(""), csrf: str = Form("")):
        check_csrf(csrf)
        email = load_email(email_id)
        if email["status"] != "approved":
            return redirect(f"/emails/{email_id}", error="Only approved emails can be sent.")
        if confirm.strip().casefold() != email["recipient"].casefold():
            return redirect(f"/emails/{email_id}", error="Type the recipient's address exactly to confirm sending.")
        if send_emails.has_placeholder(email):
            return redirect(f"/emails/{email_id}", error="This email still contains placeholder text.")
        if not (email["prospect_email_status"] == "deliverable" and email["outreach_status"] == "ready"
                and not email["do_not_contact"]):
            return redirect(f"/emails/{email_id}", error="The prospect is no longer ready for outreach; not sent.")
        config = send_emails.smtp_config(SimpleNamespace(timeout=30))
        problems = send_emails.config_problems(config)
        if problems:
            return redirect(f"/emails/{email_id}", error="SMTP is not configured: " + "; ".join(problems))
        t = tables()
        mailer = send_emails.Mailer(config)
        try:
            outcome, detail = send_emails.send_one(engine, t["emails"], t["prospects"], email, config, mailer)
        finally:
            mailer.close()
        if outcome == "sent":
            return redirect(f"/emails/{email_id}", msg=f"Sent to {email['recipient']}.")
        if outcome == "failed":
            return redirect(f"/emails/{email_id}", error=f"The server rejected it; marked failed: {detail}")
        if outcome == "stop":
            return redirect(f"/emails/{email_id}", error=f"Not sent, still approved: {detail}")
        return redirect(f"/emails/{email_id}", error=f"Not sent: {detail}")

    @app.post("/emails/bulk")
    def bulk(action: str = Form(...), ids: list[int] = Form(default=[]), csrf: str = Form(""),
             status: str = Form("in_review")):
        check_csrf(csrf)
        if not ids:
            return redirect("/emails", status=status, error="Select at least one email.")
        now = datetime.now(timezone.utc)
        done = skipped = 0
        for email_id in ids:
            if action == "approve":
                email = load_email(email_id)
                ok = not send_emails.has_placeholder(email) and set_status(
                    email_id, ("in_review",), status="approved", reviewed_at=now, review_note=None)
            elif action == "reject":
                ok = set_status(email_id, ("in_review", "approved"), status="rejected", reviewed_at=now)
            else:
                return redirect("/emails", status=status, error="Unknown action.")
            done += ok
            skipped += not ok
        verb = "approved" if action == "approve" else "rejected"
        extra = f", {skipped} skipped (wrong status or placeholder text)" if skipped else ""
        return redirect("/emails", status=status, msg=f"{done} email(s) {verb}{extra}.")

    return app
