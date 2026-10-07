"""Generate one outreach email per prospect and store it as a draft in `emails`, ready for open tracking.

For every prospect that is ready for outreach (deliverable email, `outreach_status = 'ready'`, not
`do_not_contact`) the LLM writes a subject and body from what earlier modules learned about the business and the
contact. Each email gets a cryptographically random tracking token, and its HTML footer image points at the
tracking edge function (`/email/footer/{token}`), which records opens once the email has been sent.

Nothing is sent here: emails are stored with status `draft`. The LLM is Groq when its key is set, otherwise
OpenAI (see `llm.py`).
"""
import argparse
import html
import json
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv
from openai import OpenAI
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import MetaData, Table, create_engine, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import make_url
from sqlalchemy.exc import NoSuchTableError, SQLAlchemyError

from llm import make_llm_client

# The model receives the JSON built by `prospect_context` as the user message.
SYSTEM_PROMPT = """You write first-touch cold emails for Axioware, sent one at a time to a single named person at a local business. Each email must read like a short, thoughtful note from one person to another, never like a campaign.

ABOUT AXIOWARE (the only facts you may state about us)
- Axioware is a software and AI company based in Karachi, Pakistan (axioware.tech). It builds AI voice agents, AI chatbots, machine learning solutions, websites and mobile apps.
- Our lead product for clinics is Ava, an AI dental receptionist that answers the clinic's phone 24/7. Ava books, reschedules and cancels appointments, sends SMS confirmations and reminders, answers questions about treatments, hours and insurance, and hands emergencies or complex billing calls to the clinic's staff. Ava speaks English, Urdu and Arabic.
- Anyone can try Ava on a live demo call, with no sign-up, at axioware.tech/dental-agent.
- For businesses that are not dental clinics, offer what fits the facts: an AI voice agent that answers calls and books appointments, or an AI chatbot that answers enquiries and qualifies leads on the website or WhatsApp.

THE INPUT
You receive JSON describing one prospect:
- business: name, category, city, country, website, Google rating and review count (any field may be null).
- contact: the person you are writing to. Use first_name for the greeting. job_title and role_type are our best guess and may be wrong, so never state or imply their role ("as the owner..."); write to them simply as someone at the clinic.
- website_findings: notes taken from the business's own website (services, opening hours, how patients book, years in practice, branches, reviews shown on the site).
- qualification.reasons, outreach_facts and research_summary: more notes from our research.
- validation_feedback: if present, your previous answer was rejected; fix exactly what it says.

HOW TO WRITE IT
1. Open with one specific, accurate observation about their business taken from the input (for example their opening hours, that patients book by phone, a service they offer, their branches, or their rating). Never open with "I hope this finds you well", "My name is" or praise that could apply to anyone.
2. Connect it to one problem Ava (or the fitting service) solves: calls missed while the front desk is with patients, calls after hours or on days the clinic is closed, patients left on hold, no-shows and double bookings, or reception workload. Pick the one the facts support best, and present it as a likely possibility, not a claim about their clinic.
3. Describe Ava, or the fitting service, in one or two plain sentences focused on what changes for them.
4. End with one low-pressure call to action: invite them to hear Ava on the live demo at axioware.tech/dental-agent, or ask whether a 15-minute call next week would be useful. One call to action only.
5. Sign off with the sender's name from sender.name and "Axioware" on the next line. If sender.name is empty, sign off as "The Axioware team".

STYLE
- 60 to 120 words in the body, in short paragraphs separated by blank lines. Plain text only: no markdown, bullet points, emojis, bold text or exclamation marks.
- English, warm, professional and direct. Simple words; no jargon such as "leverage", "synergy", "revolutionize", "cutting-edge" or "game-changer".
- Subject: 3 to 7 words, specific to them, in sentence case, mentioning the business name or a detail about it. No "Re:" or "Fwd:", no questions designed to trick, no all caps, no "free", "guaranteed" or "urgent".

RULES YOU MUST NOT BREAK
- Use only facts present in the input or in the ABOUT AXIOWARE section. Never invent numbers, statistics, results, client names, testimonials, prices, discounts or deadlines, and never claim we have spoken before or that they asked to be contacted.
- Never mention how we found them, our research, scraping, scores, qualification or AI tools used to write the email.
- Do not include phone numbers, email addresses, or links other than axioware.tech/dental-agent and axioware.tech.
- Do not add a footer, address or unsubscribe line; it is added automatically after your text.
- If the input has little information, write a shorter, more general but still honest email rather than guessing.

OUTPUT
Return only the JSON object with "subject" and "body". The body starts with the greeting ("Hi <first_name>,") and ends with the sign-off."""
PROMPT_IS_PLACEHOLDER = False
SENDER_NAME_ENV = "SMTP_FROM_NAME"  # name used in the sign-off; empty -> "The Axioware team"
MAX_WEBSITE_FINDINGS = 12

# Added under every email, in text and HTML. Outreach law (e.g. CAN-SPAM, GDPR/PECR) generally requires identifying
# the sender, a postal address and an easy way to opt out. The footer image (assets/email-footer.png) repeats this
# visually but is not enough on its own: many clients block images.
FOOTER_TEXT = """Axioware - AI-powered software solutions
Karachi, Pakistan | axioware.tech | business@axioware.tech
If you would rather not hear from us, reply with "unsubscribe"."""
FOOTER_IS_PLACEHOLDER = False
FOOTER_LINK = "https://axioware.tech"

SEQUENCE_STEP = 1  # first email of the sequence; follow-ups would use later steps
MAX_ATTEMPTS = 3
MAX_SUBJECT_CHARS = 200
MAX_BODY_CHARS = 5000
FOOTER_ALT = "Axioware"
FOOTER_WIDTH = 600  # matches assets/email-footer.png (rendered at 2x)
TRACKING_PATH = "/functions/v1/email/footer"
TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32,128}$")  # must match the edge function's check


class EmailDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    subject: str = Field(..., min_length=1)
    body: str = Field(..., min_length=1)


class GenerationError(RuntimeError):
    pass


# ---------------------------------------------------------------- tracking


def new_tracking_token() -> str:
    """Unguessable, URL-safe and independent of the recipient (32 random bytes -> 43 characters)."""
    return secrets.token_urlsafe(32)


def tracking_base_url(database_url: str | None) -> str | None:
    """EMAIL_TRACKING_BASE_URL, or the edge function URL derived from a Supabase database host."""
    explicit = os.environ.get("EMAIL_TRACKING_BASE_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")
    if not database_url:
        return None
    host = make_url(database_url).host or ""
    match = re.fullmatch(r"db\.([a-z0-9]+)\.supabase\.co", host)
    return f"https://{match.group(1)}.supabase.co{TRACKING_PATH}" if match else None


def tracking_url(base_url: str, token: str) -> str:
    return f"{base_url}/{quote(token, safe='')}"


# ---------------------------------------------------------------- rendering


def _paragraphs_html(text: str) -> str:
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
    return "\n".join(f"<p>{html.escape(p).replace(chr(10), '<br>')}</p>" for p in paragraphs)


def render_text(body: str) -> str:
    """Plain-text version: the body followed by the footer text."""
    return f"{body.strip()}\n\n--\n{FOOTER_TEXT.strip()}"


def render_html(body: str, footer_url: str) -> str:
    """Plain-text body as escaped HTML paragraphs, then the tracked footer image and the footer text."""
    image = (
        f'<a href="{FOOTER_LINK}"><img src="{html.escape(footer_url, quote=True)}" alt="{FOOTER_ALT}" '
        f'width="{FOOTER_WIDTH}" style="display:block;border:0;max-width:100%;height:auto;"></a>'
    )
    footer_text = _paragraphs_html(FOOTER_TEXT).replace("<p>", '<p style="font-size:12px;color:#666;">')
    return (
        "<!doctype html>\n<html><body>\n"
        f"{_paragraphs_html(body)}\n"
        f'<div class="footer">\n{image}\n{footer_text}\n</div>\n'
        "</body></html>"
    )


# ---------------------------------------------------------------- generation


_CONTACT_DETAIL = re.compile(r"@|\+?\d[\d\s().-]{7,}\d")
# Notes the research agent made about its own browsing ("the contact page was blank", "About Us is likely the best
# next page"), as opposed to facts about the business.
_RESEARCH_NOTE = re.compile(
    r"current page|this page|blank|unvisited|same-site|five-page|page limit|cleaned|scrap|likely|next page"
    r"|remaining|usable|no visible|no links|did not provide|evidence|qualif|navigation",
    re.IGNORECASE,
)


def website_findings(scraped_pages, limit: int = MAX_WEBSITE_FINDINGS) -> list[str]:
    """Facts Module 2 noted on the business's own website, without contact details or notes about the research
    itself (e.g. "the contact page was blank"), de-duplicated."""
    findings, seen = [], []
    for page in scraped_pages or []:
        for item in (page or {}).get("relevant_findings") or []:
            text = " ".join(str(item).split())
            if not text or _CONTACT_DETAIL.search(text) or _RESEARCH_NOTE.search(text):
                continue
            words = set(re.findall(r"[a-z0-9]+", text.casefold()))
            # The same fact worded twice ("Address: X" / "Clinic address: X, Pakistan") shares most of its words.
            if not words or any(len(words & other) >= 0.8 * min(len(words), len(other)) for other in seen):
                continue
            seen.append(words)
            findings.append(text[:300])
    return findings[:limit]


def clean_job_title(title: str | None) -> str | None:
    """Drop our own annotations such as "(practice named after them)" from inferred titles."""
    return re.sub(r"\s*\([^)]*\)", "", title).strip() or None if title else None


def prospect_context(row: dict) -> dict:
    """The facts the model may use. Only data earlier modules stored; no email addresses or phone numbers."""
    return {
        "sender": {"name": os.environ.get(SENDER_NAME_ENV, "").strip(), "company": "Axioware"},
        "business": {
            "name": row["business_name"],
            "category": row["category"],
            "city": row["city"],
            "country": row["country"],
            "website": row["website_url"],
            "google_rating": float(row["google_rating"]) if row["google_rating"] is not None else None,
            "google_review_count": row["google_review_count"],
        },
        "contact": {
            "name": row["contact_name"],
            "first_name": row["first_name"],
            "job_title": clean_job_title(row["job_title"]),
            "role_type": row["role_type"],
        },
        "qualification": {
            "score": row["qualification_score"],
            "reasons": row["qualification_reasons"] or [],
        },
        "website_findings": website_findings(row.get("scraped_pages")),
        "outreach_facts": row["outreach_facts"] or [],
        "research_summary": row["research_summary"],
    }


def generate_draft(client: OpenAI, model: str, context: dict) -> EmailDraft:
    response_format = {
        "type": "json_schema",
        "json_schema": {"name": "outreach_email", "strict": True, "schema": EmailDraft.model_json_schema()},
    }
    feedback = None
    for _ in range(MAX_ATTEMPTS):
        payload = {**context, "validation_feedback": feedback}
        response = client.chat.completions.create(
            model=model,
            temperature=0.7,
            response_format=response_format,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
        )
        try:
            draft = EmailDraft.model_validate_json(response.choices[0].message.content or "")
        except ValidationError:
            feedback = "Return a JSON object with a non-empty subject and body."
            continue
        subject = " ".join(draft.subject.split())
        body = draft.body.strip()
        if not subject or not body:
            feedback = "Subject and body must not be empty."
            continue
        if len(subject) > MAX_SUBJECT_CHARS or len(body) > MAX_BODY_CHARS:
            feedback = f"Keep the subject under {MAX_SUBJECT_CHARS} and the body under {MAX_BODY_CHARS} characters."
            continue
        return EmailDraft(subject=subject, body=body)
    raise GenerationError(feedback or "The model did not return a valid email.")


# ---------------------------------------------------------------- database


def load_prospects(connection, tables: dict[str, Table], args) -> list[dict]:
    prospects, contacts = tables["prospects"], tables["business_contacts"]
    businesses, profiles, emails = tables["businesses"], tables["business_website_profiles"], tables["emails"]
    statement = (
        select(
            prospects.c.id.label("prospect_id"),
            prospects.c.business_id,
            prospects.c.contact_id,
            prospects.c.email.label("recipient"),
            prospects.c.qualification_score,
            prospects.c.outreach_facts,
            prospects.c.research_summary,
            businesses.c.name.label("business_name"),
            businesses.c.category,
            businesses.c.city,
            businesses.c.country,
            businesses.c.website_url,
            businesses.c.google_rating,
            businesses.c.google_review_count,
            contacts.c.name.label("contact_name"),
            contacts.c.first_name,
            contacts.c.job_title,
            contacts.c.role_type,
            profiles.c.qualification_reasons,
            profiles.c.scraped_pages,
            emails.c.id.label("email_id"),
            emails.c.status.label("email_status"),
            emails.c.tracking_token,
        )
        .join(businesses, businesses.c.id == prospects.c.business_id)
        .join(contacts, contacts.c.id == prospects.c.contact_id)
        .outerjoin(profiles, profiles.c.business_id == prospects.c.business_id)
        .outerjoin(emails, (emails.c.prospect_id == prospects.c.id) & (emails.c.sequence_step == SEQUENCE_STEP))
        .where(
            prospects.c.email_status == "deliverable",
            prospects.c.outreach_status == "ready",
            prospects.c.do_not_contact.is_(False),
        )
        .order_by(prospects.c.qualification_score.desc().nulls_last(), prospects.c.id)
    )
    if args.regenerate:
        statement = statement.where((emails.c.id.is_(None)) | (emails.c.status == "draft"))
    else:
        statement = statement.where(emails.c.id.is_(None))
    if args.prospect_id is not None:
        statement = statement.where(prospects.c.id == args.prospect_id)
    if args.business_id is not None:
        statement = statement.where(prospects.c.business_id == args.business_id)
    if args.limit is not None:
        statement = statement.limit(args.limit)
    return [dict(row._mapping) for row in connection.execute(statement)]


def save_draft(connection, emails: Table, row: dict, draft: EmailDraft, body_html: str, token: str,
               provider: str, model: str, now: datetime) -> None:
    """Insert the draft, or replace the content of an existing draft. A sent email is never modified."""
    content = {
        "subject": draft.subject,
        "body_text": render_text(draft.body),
        "body_html": body_html,
        "generation_provider": provider,
        "generation_model": model,
        "generated_at": now,
    }
    statement = pg_insert(emails).values(
        prospect_id=row["prospect_id"],
        business_id=row["business_id"],
        contact_id=row["contact_id"],
        sequence_step=SEQUENCE_STEP,
        recipient=row["recipient"],
        tracking_token=token,
        status="draft",
        **content,
    )
    connection.execute(
        statement.on_conflict_do_update(
            constraint="uq_emails_prospect_step",
            set_={key: statement.excluded[key] for key in content},
            where=emails.c.status == "draft",
        )
    )


# ---------------------------------------------------------------- main


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a draft outreach email per ready prospect. Sends nothing.")
    parser.add_argument("--prospect-id", type=int, help="Generate for one prospect only.")
    parser.add_argument("--business-id", type=int, help="Generate for one business only.")
    parser.add_argument("--limit", type=int, help="Maximum number of prospects in this run.")
    parser.add_argument("--regenerate", action="store_true", help="Rewrite existing drafts (sent emails are never touched).")
    parser.add_argument("--dry-run", action="store_true", help="Generate and print without writing to the database.")
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be a positive integer")

    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        parser.error("DATABASE_URL is not set in the environment or project .env file")
    base_url = tracking_base_url(database_url)
    if not base_url:
        parser.error("Set EMAIL_TRACKING_BASE_URL (e.g. https://<project>.supabase.co/functions/v1/email/footer) in .env")
    llm = make_llm_client()
    if llm is None:
        parser.error("Set GROQ_API_KEY (or GROK_API_KEY) or OPENAI_API_KEY in the environment or project .env file")
    client, model, provider = llm
    if PROMPT_IS_PLACEHOLDER:
        print("Warning: the email prompt is still a placeholder (SYSTEM_PROMPT in generate_emails.py).")
    if FOOTER_IS_PLACEHOLDER:
        print("Warning: the email footer is still a placeholder (FOOTER_TEXT in generate_emails.py).")
    print(f"Using {provider} model {model}. Tracking URL base: {base_url}")

    engine = create_engine(database_url, pool_pre_ping=True)
    metadata = MetaData()
    try:
        try:
            tables = {
                name: Table(name, metadata, autoload_with=engine)
                for name in ("prospects", "business_contacts", "businesses", "business_website_profiles", "emails")
            }
        except NoSuchTableError as error:
            print(f"Table {error} does not exist. Run `alembic upgrade head` first.")
            return 1
        with engine.connect() as connection:
            queue = load_prospects(connection, tables, args)
        if not queue:
            print("No prospects need an email (use --regenerate to rewrite drafts).")
            return 0

        print(f"Generating emails for {len(queue)} prospect(s).")
        created = failed = 0
        for row in queue:
            token = row["tracking_token"] or new_tracking_token()  # a regenerated draft keeps its token
            try:
                draft = generate_draft(client, model, prospect_context(row))
            except Exception as error:  # one bad prospect (bad output, API error) must not stop the run
                failed += 1
                print(f"  Prospect {row['prospect_id']} <{row['recipient']}> failed ({type(error).__name__}): {error}")
                continue
            body_html = render_html(draft.body, tracking_url(base_url, token))
            print(f"  Prospect {row['prospect_id']} <{row['recipient']}>: {draft.subject}")
            if args.dry_run:
                print("    " + draft.body.replace("\n", "\n    "))
            else:
                with engine.begin() as connection:
                    save_draft(connection, tables["emails"], row, draft, body_html, token, provider, model,
                               datetime.now(timezone.utc))
            created += 1
        verb = "generated (dry run, not saved)" if args.dry_run else "saved as drafts"
        print(f"Done: {created} email(s) {verb}, {failed} failed.")
        return 1 if failed else 0
    except SQLAlchemyError as error:
        print(f"Database operation failed ({type(error).__name__}): {str(error).splitlines()[0]}")
        print("Check DATABASE_URL and run `alembic upgrade head` first.")
        return 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
