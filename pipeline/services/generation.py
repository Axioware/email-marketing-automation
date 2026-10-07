"""Generate one outreach email per prospect and store it in `emails` for review, ready for open tracking.

For every prospect that is ready for outreach (deliverable email, `outreach_status = 'ready'`, not
`do_not_contact`) the LLM writes a subject and body from what earlier modules learned about the business and the
contact. Each email gets a cryptographically random tracking token, and its HTML footer image points at the
tracking edge function (`/email/footer/{token}`), which records opens once the email has been sent.

Nothing is sent here: emails are stored with status `in_review`; approve or edit them in the admin
(Pipeline > Emails) or through the API. The LLM is Groq when its key is set, otherwise OpenAI (see `llm.py`).
"""
import html
import json
import os
import re
import secrets
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit

from django.core.management.base import CommandError
from django.db import DatabaseError, IntegrityError, transaction
from django.db.models import F, FilteredRelation, Q
from openai import OpenAI
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from pipeline.models import Email, Prospect
from pipeline.services.llm import make_llm_client

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

REGENERATABLE_STATUSES = ("in_review", "rejected")
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
    try:
        host = urlsplit(database_url).hostname or ""
    except ValueError:
        return None
    match = re.fullmatch(r"db\.([a-z0-9]+)\.supabase\.co", host)
    return f"https://{match.group(1)}.supabase.co{TRACKING_PATH}" if match else None


def default_tracking_base_url() -> str | None:
    return tracking_base_url(os.environ.get("DATABASE_URL"))


def tracking_url(base_url: str, token: str) -> str:
    return f"{base_url}/{quote(token, safe='')}"


# ---------------------------------------------------------------- rendering


def _paragraphs_html(text: str) -> str:
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
    return "\n".join(f"<p>{html.escape(p).replace(chr(10), '<br>')}</p>" for p in paragraphs)


def render_text(body: str) -> str:
    """Plain-text version: the body followed by the footer text."""
    return f"{body.strip()}{FOOTER_SEPARATOR}{FOOTER_TEXT.strip()}"


FOOTER_SEPARATOR = "\n\n--\n"


def editable_body(body_text: str) -> str:
    """The part of a stored plain-text email that a person writes: everything before the footer."""
    head, sep, _ = (body_text or "").rpartition(FOOTER_SEPARATOR)
    return head if sep else (body_text or "")


def footer_preview_url(base_url: str | None) -> str | None:
    """Public Storage URL of the footer image, for previews that must not count as opens."""
    if base_url and base_url.endswith("/functions/v1/email/footer"):
        return base_url[: -len("/functions/v1/email/footer")] + "/storage/v1/object/public/email-assets/footer.png"
    return None


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


PROSPECT_COLUMNS = ("business_id", "contact_id", "qualification_score", "outreach_facts", "research_summary")
PROSPECT_FIELDS = {
    "prospect_id": F("id"),
    "recipient": F("email"),
    "business_name": F("business__name"),
    "category": F("business__category"),
    "city": F("business__city"),
    "country": F("business__country"),
    "website_url": F("business__website_url"),
    "google_rating": F("business__google_rating"),
    "google_review_count": F("business__google_review_count"),
    "contact_name": F("contact__name"),
    "first_name": F("contact__first_name"),
    "job_title": F("contact__job_title"),
    "role_type": F("contact__role_type"),
    "qualification_reasons": F("business__website_profile__qualification_reasons"),
    "scraped_pages": F("business__website_profile__scraped_pages"),
    "email_id": F("step_email__id"),
    "current_email_status": F("step_email__status"),
    "tracking_token": F("step_email__tracking_token"),
}


def load_prospects(options: dict) -> list[dict]:
    """Ready prospects (deliverable, outreach_status ready, not do-not-contact) that need a first email, with the
    facts the model may use. With `regenerate`, prospects whose email is in review or rejected are included too."""
    queryset = (
        Prospect.objects.filter(email_status="deliverable", outreach_status="ready", do_not_contact=False)
        .annotate(step_email=FilteredRelation("emails", condition=Q(emails__sequence_step=SEQUENCE_STEP)))
        .order_by(F("qualification_score").desc(nulls_last=True), "id")
    )
    if options.get("regenerate"):
        queryset = queryset.filter(Q(step_email__isnull=True) | Q(step_email__status__in=REGENERATABLE_STATUSES))
    else:
        queryset = queryset.filter(step_email__isnull=True)
    if options.get("prospect_id"):
        queryset = queryset.filter(id__in=options["prospect_id"])
    if options.get("business_id"):
        queryset = queryset.filter(business_id__in=options["business_id"])
    if options.get("limit") is not None:
        queryset = queryset[: options["limit"]]
    return list(queryset.values(*PROSPECT_COLUMNS, **PROSPECT_FIELDS))


def save_draft(row: dict, draft: EmailDraft, body_html: str, token: str, provider: str, model: str,
               now: datetime) -> bool:
    """Insert the email for review, or replace an existing one that is in review or rejected (it goes back to
    review). Approved, sent and failed emails are never modified. Returns whether anything was written."""
    content = {
        "subject": draft.subject,
        "body_text": render_text(draft.body),
        "body_html": body_html,
        "generation_provider": provider,
        "generation_model": model,
        "generated_at": now,
    }
    with transaction.atomic():
        existing = (
            Email.objects.select_for_update()
            .filter(prospect_id=row["prospect_id"], sequence_step=SEQUENCE_STEP)
            .first()
        )
        if existing is None:
            try:
                with transaction.atomic():
                    Email.objects.create(
                        prospect_id=row["prospect_id"],
                        business_id=row["business_id"],
                        contact_id=row["contact_id"],
                        sequence_step=SEQUENCE_STEP,
                        recipient=row["recipient"],
                        tracking_token=token,
                        status=Email.Status.IN_REVIEW,
                        **content,
                    )
                return True
            except IntegrityError:  # created concurrently; leave that one alone
                return False
        if existing.status not in REGENERATABLE_STATUSES:
            return False
        for key, value in {**content, "status": Email.Status.IN_REVIEW, "reviewed_at": None, "review_note": None,
                           "edited_at": None}.items():
            setattr(existing, key, value)
        existing.save()
        return True


# ---------------------------------------------------------------- main


def add_arguments(parser) -> None:
    parser.add_argument("--prospect-id", type=int, action="append", help="Generate for this prospect only (repeatable).")
    parser.add_argument("--business-id", type=int, action="append", help="Generate for this business only (repeatable).")
    parser.add_argument("--limit", type=int, help="Maximum number of prospects in this run.")
    parser.add_argument("--regenerate", action="store_true",
                        help="Rewrite emails that are in review or rejected (approved and sent emails are never touched).")
    parser.add_argument("--dry-run", action="store_true", help="Generate and print without writing to the database.")


def run(options: dict) -> int:
    if options["limit"] is not None and options["limit"] < 1:
        raise CommandError("--limit must be a positive integer")
    base_url = default_tracking_base_url()
    if not base_url:
        raise CommandError("Set EMAIL_TRACKING_BASE_URL (e.g. https://<project>.supabase.co/functions/v1/email/footer) in .env")
    llm = make_llm_client()
    if llm is None:
        raise CommandError("Set GROQ_API_KEY (or GROK_API_KEY) or OPENAI_API_KEY in the environment or project .env file")
    client, model, provider = llm
    if PROMPT_IS_PLACEHOLDER:
        print("Warning: the email prompt is still a placeholder (SYSTEM_PROMPT in pipeline/services/generation.py).")
    if FOOTER_IS_PLACEHOLDER:
        print("Warning: the email footer is still a placeholder (FOOTER_TEXT in pipeline/services/generation.py).")
    print(f"Using {provider} model {model}. Tracking URL base: {base_url}")

    try:
        queue = load_prospects(options)
        if not queue:
            print("No prospects need an email (use --regenerate to rewrite emails in review).")
            return 0

        print(f"Generating emails for {len(queue)} prospect(s).")
        created = failed = 0
        for row in queue:
            token = row["tracking_token"] or new_tracking_token()  # a regenerated email keeps its token
            try:
                draft = generate_draft(client, model, prospect_context(row))
            except Exception as error:  # one bad prospect (bad output, API error) must not stop the run
                failed += 1
                print(f"  Prospect {row['prospect_id']} <{row['recipient']}> failed ({type(error).__name__}): {error}")
                continue
            body_html = render_html(draft.body, tracking_url(base_url, token))
            print(f"  Prospect {row['prospect_id']} <{row['recipient']}>: {draft.subject}")
            if options["dry_run"]:
                print("    " + draft.body.replace("\n", "\n    "))
            else:
                save_draft(row, draft, body_html, token, provider, model, datetime.now(timezone.utc))
            created += 1
        verb = "generated (dry run, not saved)" if options["dry_run"] else "saved for review"
        print(f"Done: {created} email(s) {verb}, {failed} failed.")
        return 1 if failed else 0
    except DatabaseError as error:
        print(f"Database operation failed ({type(error).__name__}): {str(error).splitlines()[0]}")
        print("Check DATABASE_URL and run `python manage.py migrate` first.")
        return 1
