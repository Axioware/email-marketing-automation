"""Write one outreach email per prospect from its campaign's templates and store it in `emails` for review.

For every prospect that is ready for outreach (deliverable email or approved by a person, `outreach_status =
'ready'`, not `do_not_contact`) the initial-email template of its email sequence is filled in with the prospect's details (pipeline/services/templates.py). The prospect gets one of its campaign's sequences (several: split equally as an
A/B test; none: the prospect is skipped) and keeps it, so its follow-ups come from the same sequence. No AI writes any
email. A template that needs a value the prospect does not have (and gives no fallback) holds that email
back, with the reason. Each email gets a random tracking token. Open tracking is switched off (OPEN_TRACKING below):
the email carries no image.

Nothing is sent here: emails are stored with status `in_review`; approve or edit them in the admin (Pipeline >
Emails) or through the API. Approved emails get a send time; queued on the Outbox page, they are sent from this
machine then (see outreach.py, sending.py).
"""
import html
import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit

from django.core.management.base import CommandError
from django.db import DatabaseError, IntegrityError, transaction
from django.db.models import Count, Exists, F, FilteredRelation, OuterRef, Q
from django.db.models.functions import Lower

from pipeline.management.base import out_of_time, time_limit_note
from pipeline.models import (DEFAULT_FOOTER_TEXT, DiscoveryCampaign, Email, EmailPrompt, EmailSequence, FollowUp,
                             Prospect, SendingSettings)
from pipeline.services import templates

MAX_WEBSITE_FINDINGS = 12

# Added under every email, in text and HTML. Outreach law (e.g. CAN-SPAM, GDPR/PECR) generally requires identifying
# the sender, a postal address and an easy way to opt out. The footer image (assets/email-footer.png) repeats this
# visually but is not enough on its own: many clients block images.
FOOTER_TEXT = DEFAULT_FOOTER_TEXT  # the default; the one in use is edited on the Settings page (footer_text())
FOOTER_LINK = "https://axioware.tech"

REGENERATABLE_STATUSES = ("in_review", "rejected")
SEQUENCE_STEP = 1  # emails.sequence_step of the first email; follow-up N is stored as step N + 1
EMAIL_RESERVE_SECONDS = 5  # with a deadline (the web app): filling a template takes a moment
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


@dataclass
class Draft:
    """A filled-in template."""

    subject: str
    body: str


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


def footer_text() -> str:
    """The footer under every email, from the Settings page (the default when it is empty)."""
    text = (SendingSettings.current().footer_text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    return text or DEFAULT_FOOTER_TEXT


def render_text(body: str) -> str:
    """Plain-text version: the body followed by the footer text."""
    return f"{body.strip()}{FOOTER_SEPARATOR}{footer_text()}"


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
    footer = _paragraphs_html(footer_text()).replace("<p>", '<p style="font-size:12px;color:#666;">')
    return (
        "<!doctype html>\n<html><body>\n"
        f"{_paragraphs_html(body)}\n"
        f'<div class="footer">\n{image}{footer}\n</div>\n'
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


def resolve_template(row: dict, chosen: EmailPrompt | None = None, cache: dict | None = None,
                     sequence: int = 0) -> EmailPrompt | None:
    """The template for one prospect and step (sequence 0 = the initial email, n = follow-up n).

    A chosen template wins. Otherwise both come from the prospect's email sequence (its A/B group, see
    assign_sequence): its initial-email template, or the template of its follow-up step n. None when there is none:
    that email is skipped.
    """
    if chosen is not None:
        return chosen
    cache = {} if cache is None else cache
    sequence_id = row.get("email_sequence_id")
    key = ("step", sequence_id, sequence)
    if key not in cache:
        if not sequence_id:
            cache[key] = None
        elif sequence == 0:
            found = EmailSequence.objects.select_related("initial_template").filter(pk=sequence_id).first()
            cache[key] = found.initial_template if found else None
        else:
            step = FollowUp.objects.select_related("email_prompt").filter(
                email_sequence_id=sequence_id, sequence=sequence).first()
            cache[key] = step.email_prompt if step else None
    return cache[key]


def missing_prompt_reason(row: dict, sequence: int = 0) -> str:
    found = EmailSequence.objects.filter(pk=row.get("email_sequence_id")).first() \
        if row.get("email_sequence_id") else None
    if found is None:
        return "its campaign has no email sequence (choose one on the campaign)"
    if sequence == 0:
        return f"its sequence {found} has no initial-email template"
    if not found.follow_ups.filter(sequence=sequence).exists():
        return f"its sequence {found} has no follow-up {sequence}"
    return f"follow-up {sequence} of its sequence {found} has no template"


def assign_sequence(row: dict, save: bool = True) -> int | None:
    """The prospect's email sequence (its A/B group): kept if it has one, else the campaign's sequence with the fewest
    prospects of the campaign so far (equal split). Stored on the prospect unless `save` is False. None when the
    campaign has no sequence."""
    if row.get("email_sequence_id"):
        return row["email_sequence_id"]
    available = sorted(DiscoveryCampaign.email_sequences.through.objects
                       .filter(discoverycampaign_id=row.get("campaign_id")).values_list("emailsequence_id", flat=True))
    if not available:
        return None
    counts = dict(Prospect.objects.filter(business__discovery_campaign_id=row.get("campaign_id"),
                                          email_sequence_id__in=available)
                  .values_list("email_sequence_id").annotate(n=Count("id"))
                  .values_list("email_sequence_id", "n"))
    sequence_id = min(available, key=lambda value: (counts.get(value, 0), value))
    if save:
        Prospect.objects.filter(pk=row["prospect_id"], email_sequence__isnull=True).update(email_sequence_id=sequence_id)
    row["email_sequence_id"] = sequence_id
    return sequence_id


def fill_template(template: EmailPrompt, row: dict) -> Draft:
    """The template filled in for one prospect; templates.TemplateIncomplete when a variable has no value."""
    subject, body = templates.render(template.subject, template.body, row)
    if not subject or not body:
        raise templates.TemplateIncomplete(["subject" if not subject else "body"])
    return Draft(subject[:MAX_SUBJECT_CHARS], body[:MAX_BODY_CHARS])


# A prospect that may get emails: verified deliverable (or approved by a person), ready, not stopped.
READY_PROSPECT = (Q(email_status="deliverable") | Q(approved_by_person=True)) & Q(
    outreach_status="ready", do_not_contact=False, sequence_stopped_at__isnull=True)

PROSPECT_COLUMNS = ("business_id", "contact_id", "qualification_score", "outreach_facts", "research_summary",
                    "email_sequence_id")
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
    "last_name": F("contact__last_name"),
    "campaign_name": F("business__discovery_campaign__name"),
    "job_title": F("contact__job_title"),
    "role_type": F("contact__role_type"),
    "qualification_reasons": F("business__website_profile__qualification_reasons"),
    "scraped_pages": F("business__website_profile__scraped_pages"),
    "extracted": F("business__website_profile__extracted"),
    "email_id": F("step_email__id"),
    "current_email_status": F("step_email__status"),
    "tracking_token": F("step_email__tracking_token"),
}


def ready_prospects():
    """Prospects that may get emails: one address per person and one sequence per address.

    Several addresses of a contact can be deliverable (each checked guess is stored), so only the contact's first
    ready address (candidate order: its own email, then the likeliest guesses) counts, and none once another address
    of the contact has an email or is do-not-contact. And one address can belong to several prospects (an info@ of
    two branches, a person found at two businesses): it gets one sequence, from the prospect that has emails or a
    a pipeline stage, else the oldest; none if any of them is do-not-contact."""
    same_contact = Prospect.objects.filter(contact_id=OuterRef("contact_id")).exclude(pk=OuterRef("pk"))
    same_address = (Prospect.objects.annotate(address=Lower("email")).filter(address=OuterRef("address"))
                    .exclude(pk=OuterRef("pk")))
    return (
        Prospect.objects.filter(READY_PROSPECT).annotate(address=Lower("email"))
        .exclude(Exists(Email.objects.filter(contact_id=OuterRef("contact_id")).exclude(prospect_id=OuterRef("pk"))))
        .exclude(Exists(same_contact.filter(do_not_contact=True)))
        .exclude(Exists(same_contact.filter(READY_PROSPECT, id__lt=OuterRef("pk"))))
        .exclude(Exists(same_address.filter(do_not_contact=True)))
        .exclude(Exists(same_address.filter(Q(pipeline_stage__isnull=False) | Q(emails__isnull=False))))
        .exclude(Exists(same_address.filter(READY_PROSPECT, id__lt=OuterRef("pk"))))
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


def save_draft(row: dict, draft: Draft, body_html: str, token: str, provider: str, model: str,
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
    parser.add_argument("--prospect-id", type=int, action="append", help="Only this prospect (repeatable).")
    parser.add_argument("--business-id", type=int, action="append", help="Only this business (repeatable).")
    parser.add_argument("--limit", type=int, help="Maximum number of prospects in this run.")
    parser.add_argument("--regenerate", action="store_true",
                        help="Fill again the emails that are in review or rejected (approved and sent emails are never "
                             "touched).")
    parser.add_argument("--email-prompt-id", "--template-id", type=int, dest="email_prompt_id",
                        help="Use this template for every email (default: the initial-email template of each prospect's sequence).")
    parser.add_argument("--dry-run", action="store_true", help="Fill and print without writing to the database.")


def run(options: dict) -> int:
    if options["limit"] is not None and options["limit"] < 1:
        raise CommandError("--limit must be a positive integer")
    if OPEN_TRACKING and not default_tracking_base_url():
        raise CommandError("Set EMAIL_TRACKING_BASE_URL (e.g. https://<project>.supabase.co/functions/v1/email/footer) in .env")
    chosen = None
    if options.get("email_prompt_id") is not None:
        chosen = EmailPrompt.objects.filter(pk=options["email_prompt_id"]).first()
        if chosen is None:
            raise CommandError(f"Template {options['email_prompt_id']} does not exist.")
        print(f"Using template: {chosen}")

    try:
        queue = load_prospects(options)
        if not queue:
            print("No prospects need an email (use --regenerate to fill again emails in review).")
            return 0

        print(f"Writing emails from templates for {len(queue)} prospect(s).")
        created = held = skipped = 0
        cache: dict = {}
        for position, row in enumerate(queue):
            if out_of_time(options, EMAIL_RESERVE_SECONDS):
                time_limit_note(len(queue) - position, "email(s)")
                break
            label = f"Prospect {row['prospect_id']} <{row['recipient']}>"
            if chosen is None and assign_sequence(row, save=not options["dry_run"]) is None:
                skipped += 1
                print(f"  {label} skipped: {missing_prompt_reason(row)}.")
                continue
            template = resolve_template(row, chosen, cache)
            if template is None:
                skipped += 1
                print(f"  {label} skipped: {missing_prompt_reason(row)}.")
                continue
            try:
                draft = fill_template(template, row)
            except templates.TemplateIncomplete as error:
                held += 1
                print(f"  {label} held back ({template}): {error}.")
                continue
            token = row["tracking_token"] or new_tracking_token()  # an email filled again keeps its token
            print(f"  {label} ({template}): {draft.subject}")
            if options["dry_run"]:
                print("    " + draft.body.replace("\n", "\n    "))
            else:
                save_draft(row, draft, render_html(draft.body, footer_image_url(token)), token, "template",
                           template.name, datetime.now(timezone.utc), template.pk)
            created += 1
        verb = "filled (dry run, not saved)" if options["dry_run"] else "saved for review"
        print(f"Done: {created} email(s) {verb}" + (f", {held} held back (a variable with no value)" if held else "")
              + (f", {skipped} skipped (no template)" if skipped else "") + ".")
        return 1 if held or skipped else 0
    except DatabaseError as error:
        print(f"Database operation failed ({type(error).__name__}): {str(error).splitlines()[0]}")
        print("Check DATABASE_URL and run `python manage.py migrate` first.")
        return 1
