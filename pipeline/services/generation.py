"""Generate one outreach email per prospect and store it in `emails` for review.

For every prospect that is ready for outreach (deliverable email or approved by a person, `outreach_status =
'ready'`, not `do_not_contact`) the LLM writes a subject and body from what earlier modules learned about the
business and the contact, following one of the campaign's initial-email prompts (several: split equally as an A/B
test; none: the prospect is skipped). Each email gets a random tracking token. Open tracking is switched off (OPEN_TRACKING below): the email
carries no image. When it is on, the HTML footer image points at the tracking edge function
(`/email/footer/{token}`), which records opens once the email has been sent.

Nothing is sent here: emails are stored with status `in_review`; approve or edit them in the admin
(Pipeline > Emails) or through the API. Approved emails are scheduled and handed to GoHighLevel (see outreach.py). The LLM is Groq when its key is set, otherwise OpenAI (see `llm.py`).
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
from django.db.models import Count, Exists, F, FilteredRelation, OuterRef, Q
from openai import OpenAI
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from pipeline.models import DiscoveryCampaign, Email, EmailPrompt, FollowUp, Prospect
from pipeline.services.llm import make_llm_client, rejected_output

# The model's instructions are one email prompt (email_prompts table, edited in the admin), chosen per campaign, step and
# A/B group; it receives the JSON built by `prospect_context` as the user message. There is no built-in prompt.
SENDER_NAME_ENV = "SENDER_NAME"  # name used in the sign-off; empty -> "The Axioware team"
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
SEQUENCE_STEP = 1  # emails.sequence_step of the first email; follow-up N is stored as step N + 1
MAX_ATTEMPTS = 3
MAX_COMPLETION_TOKENS = 2500  # a short email; Groq counts this budget against its per-minute token limit
MAX_SUBJECT_CHARS = 200
MAX_BODY_CHARS = 5000
FOOTER_ALT = "Axioware"
FOOTER_WIDTH = 600  # matches assets/email-footer.png (rendered at 2x)
TRACKING_PATH = "/functions/v1/email/footer"
# Open tracking is switched off: emails carry no image, so opens are not recorded. Everything it needs stays in place
# (tracking tokens, the open fields on emails, the Supabase edge function and record_email_open); set this to True
# (and EMAIL_TRACKING_BASE_URL if the database is not on Supabase) to send the tracked footer image again.
OPEN_TRACKING = False
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


def footer_image_url(token: str) -> str | None:
    """The tracked footer image for an email, or None while open tracking is off (no image is sent at all)."""
    if not OPEN_TRACKING:
        return None
    base_url = default_tracking_base_url()
    return tracking_url(base_url, token) if base_url else None


def render_html(body: str, footer_url: str | None = None) -> str:
    """Plain-text body as escaped HTML paragraphs, then the footer image (only when a URL is given) and the footer
    text."""
    image = (
        f'<a href="{FOOTER_LINK}"><img src="{html.escape(footer_url, quote=True)}" alt="{FOOTER_ALT}" '
        f'width="{FOOTER_WIDTH}" style="display:block;border:0;max-width:100%;height:auto;"></a>\n'
        if footer_url else ""
    )
    footer_text = _paragraphs_html(FOOTER_TEXT).replace("<p>", '<p style="font-size:12px;color:#666;">')
    return (
        "<!doctype html>\n<html><body>\n"
        f"{_paragraphs_html(body)}\n"
        f'<div class="footer">\n{image}{footer_text}\n</div>\n'
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


def resolve_prompt(row: dict, chosen: EmailPrompt | None = None, cache: dict | None = None,
                   sequence: int = 0) -> tuple[str, int] | None:
    """(system prompt, email prompt id) for one prospect and step (sequence 0 = the initial email, n = follow-up n).

    A chosen email prompt wins. Otherwise the initial email uses the prospect's initial-email prompt (its A/B group,
    see assign_initial_prompt) and follow-up n the prompt of the campaign's follow-up step n. None when there is
    none: that email is skipped.
    """
    if chosen is not None:
        return chosen.prompt, chosen.pk
    cache = {} if cache is None else cache
    if sequence == 0:
        prompt_id = row.get("initial_email_prompt_id")
        key = ("prompt", prompt_id)
        if key not in cache:
            cache[key] = EmailPrompt.objects.filter(pk=prompt_id).first() if prompt_id else None
        prompt = cache[key]
    else:
        key = ("follow_up", row.get("campaign_id"), sequence)
        if key not in cache:
            step = FollowUp.objects.select_related("email_prompt").filter(
                campaign_id=row.get("campaign_id"), sequence=sequence).first()
            cache[key] = step.email_prompt if step else None
        prompt = cache[key]
    return (prompt.prompt, prompt.pk) if prompt is not None else None


def missing_prompt_reason(row: dict, sequence: int = 0) -> str:
    if sequence == 0:
        return "its campaign has no initial-email prompt (choose one on the campaign)"
    if not FollowUp.objects.filter(campaign_id=row.get("campaign_id"), sequence=sequence).exists():
        return f"its campaign has no follow-up {sequence}"
    return f"follow-up {sequence} of its campaign has no email prompt (choose one on the campaign)"


def assign_initial_prompt(row: dict, save: bool = True) -> int | None:
    """The prospect's initial-email prompt (its A/B group): kept if it has one, else the campaign's initial-email
    prompt with the fewest prospects of the campaign so far (equal split). Stored on the prospect unless `save` is
    False. None when the campaign has no initial-email prompt."""
    if row.get("initial_email_prompt_id"):
        return row["initial_email_prompt_id"]
    available = sorted(DiscoveryCampaign.initial_email_prompts.through.objects
                       .filter(discoverycampaign_id=row.get("campaign_id")).values_list("emailprompt_id", flat=True))
    if not available:
        return None
    counts = dict(Prospect.objects.filter(business__discovery_campaign_id=row.get("campaign_id"),
                                          initial_email_prompt_id__in=available)
                  .values_list("initial_email_prompt_id").annotate(n=Count("id"))
                  .values_list("initial_email_prompt_id", "n"))
    prompt_id = min(available, key=lambda value: (counts.get(value, 0), value))
    if save:
        Prospect.objects.filter(pk=row["prospect_id"], initial_email_prompt__isnull=True).update(
            initial_email_prompt_id=prompt_id)
    row["initial_email_prompt_id"] = prompt_id
    return prompt_id


def generate_draft(client: OpenAI, model: str, context: dict, system_prompt: str) -> EmailDraft:
    response_format = {
        "type": "json_schema",
        "json_schema": {"name": "outreach_email", "strict": True, "schema": EmailDraft.model_json_schema()},
    }
    feedback = None
    for _ in range(MAX_ATTEMPTS):
        payload = {**context, "validation_feedback": feedback}
        try:
            response = client.chat.completions.create(
                model=model,
                temperature=0.7,
                max_completion_tokens=MAX_COMPLETION_TOKENS,
                response_format=response_format,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
            )
            content = response.choices[0].message.content or ""
        except Exception as error:
            content = rejected_output(error)  # the provider refused the answer's shape: check it here instead
            if content is None:
                raise
        try:
            draft = EmailDraft.model_validate_json(content)
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


# A prospect that may get emails: verified deliverable (or approved by a person), ready, not stopped.
READY_PROSPECT = (Q(email_status="deliverable") | Q(approved_by_person=True)) & Q(
    outreach_status="ready", do_not_contact=False, sequence_stopped_at__isnull=True)

PROSPECT_COLUMNS = ("business_id", "contact_id", "qualification_score", "outreach_facts", "research_summary",
                    "initial_email_prompt_id")
PROSPECT_FIELDS = {
    "campaign_id": F("business__discovery_campaign_id"),
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


def ready_prospects():
    """Prospects that may get emails, one address per person: several addresses of a contact can be deliverable
    (each checked guess is stored), so only the contact's first ready address (candidate order: its own email, then
    the likeliest guesses) counts, and none once another address of the contact has an email or is do-not-contact."""
    same_contact = Prospect.objects.filter(contact_id=OuterRef("contact_id")).exclude(pk=OuterRef("pk"))
    return (
        Prospect.objects.filter(READY_PROSPECT)
        .exclude(Exists(Email.objects.filter(contact_id=OuterRef("contact_id")).exclude(prospect_id=OuterRef("pk"))))
        .exclude(Exists(same_contact.filter(do_not_contact=True)))
        .exclude(Exists(same_contact.filter(READY_PROSPECT, id__lt=OuterRef("pk"))))
    )


def load_prospects(options: dict) -> list[dict]:
    """Ready prospects (see ready_prospects) that need an initial email, with the facts the model may use. With
    `regenerate`, prospects whose email is in review or rejected are included too."""
    queryset = (
        ready_prospects()
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


def prospect_rows(queryset) -> list[dict]:
    """The same facts as load_prospects, for any prospects (used for follow-ups)."""
    queryset = queryset.annotate(step_email=FilteredRelation("emails", condition=Q(emails__sequence_step=SEQUENCE_STEP)))
    return list(queryset.values(*PROSPECT_COLUMNS, **PROSPECT_FIELDS))


def save_draft(row: dict, draft: EmailDraft, body_html: str, token: str, provider: str, model: str,
               now: datetime, email_prompt_id: int | None = None, sequence_step: int = SEQUENCE_STEP) -> bool:
    """Insert the email for review, or replace an existing one that is in review or rejected (it goes back to
    review). Approved, sent and failed emails are never modified. Returns whether anything was written."""
    content = {
        "subject": draft.subject,
        "body_text": render_text(draft.body),
        "body_html": body_html,
        "generation_provider": provider,
        "generation_model": model,
        "email_prompt_id": email_prompt_id,
        "generated_at": now,
    }
    with transaction.atomic():
        existing = (
            Email.objects.select_for_update()
            .filter(prospect_id=row["prospect_id"], sequence_step=sequence_step)
            .first()
        )
        if existing is None:
            try:
                with transaction.atomic():
                    Email.objects.create(
                        prospect_id=row["prospect_id"],
                        business_id=row["business_id"],
                        contact_id=row["contact_id"],
                        sequence_step=sequence_step,
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
    parser.add_argument("--email-prompt-id", type=int,
                        help="Write every email with this email prompt (default: each campaign's default prompt).")
    parser.add_argument("--dry-run", action="store_true", help="Generate and print without writing to the database.")


def run(options: dict) -> int:
    if options["limit"] is not None and options["limit"] < 1:
        raise CommandError("--limit must be a positive integer")
    if OPEN_TRACKING and not default_tracking_base_url():
        raise CommandError("Set EMAIL_TRACKING_BASE_URL (e.g. https://<project>.supabase.co/functions/v1/email/footer) in .env")
    llm = make_llm_client()
    if llm is None:
        raise CommandError("Set GROQ_API_KEY (or GROK_API_KEY) or OPENAI_API_KEY in the environment or project .env file")
    client, model, provider = llm
    chosen = None
    if options.get("email_prompt_id") is not None:
        chosen = EmailPrompt.objects.filter(pk=options["email_prompt_id"]).first()
        if chosen is None:
            raise CommandError(f"Email prompt {options['email_prompt_id']} does not exist.")
        print(f"Using email prompt: {chosen}")
    if FOOTER_IS_PLACEHOLDER:
        print("Warning: the email footer is still a placeholder (FOOTER_TEXT in pipeline/services/generation.py).")
    print(f"Using {provider} model {model}. Open tracking is " + ("on." if OPEN_TRACKING else "off (no image in emails)."))

    try:
        queue = load_prospects(options)
        if not queue:
            print("No prospects need an email (use --regenerate to rewrite emails in review).")
            return 0

        print(f"Generating emails for {len(queue)} prospect(s).")
        created = failed = skipped = 0
        prompts: dict = {}
        for row in queue:
            token = row["tracking_token"] or new_tracking_token()  # a regenerated email keeps its token
            if chosen is None and assign_initial_prompt(row, save=not options["dry_run"]) is None:
                skipped += 1
                print(f"  Prospect {row['prospect_id']} <{row['recipient']}> skipped: {missing_prompt_reason(row)}.")
                continue
            system_prompt, email_prompt_id = resolve_prompt(row, chosen, prompts)
            try:
                draft = generate_draft(client, model, prospect_context(row), system_prompt)
            except Exception as error:  # one bad prospect (bad output, API error) must not stop the run
                failed += 1
                print(f"  Prospect {row['prospect_id']} <{row['recipient']}> failed ({type(error).__name__}): {error}")
                continue
            body_html = render_html(draft.body, footer_image_url(token))
            print(f"  Prospect {row['prospect_id']} <{row['recipient']}> (prompt #{email_prompt_id}): {draft.subject}")
            if options["dry_run"]:
                print("    " + draft.body.replace("\n", "\n    "))
            else:
                save_draft(row, draft, body_html, token, provider, model, datetime.now(timezone.utc), email_prompt_id)
            created += 1
        verb = "generated (dry run, not saved)" if options["dry_run"] else "saved for review"
        print(f"Done: {created} email(s) {verb}, {failed} failed" + (f", {skipped} skipped (no email prompt)." if skipped else "."))
        return 1 if failed or skipped else 0
    except DatabaseError as error:
        print(f"Database operation failed ({type(error).__name__}): {str(error).splitlines()[0]}")
        print("Check DATABASE_URL and run `python manage.py migrate` first.")
        return 1
