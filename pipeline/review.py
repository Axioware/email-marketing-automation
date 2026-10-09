"""Reviewing generated emails: edit, approve, reject, reopen, regenerate and send. Used by the admin and the API.

Every change is a conditional update on the email's current status, so two reviewers (or a reviewer and a send run)
can never move an email through an invalid transition. Editing always sends an email back to review.
"""
from datetime import datetime, timezone

from django.db.models import Count, Q

from pipeline.models import Email, EmailPrompt
from pipeline.services import generation, sending

EDITABLE = ("in_review", "approved", "rejected")
STATUS_ORDER = ("in_review", "approved", "rejected", "sent", "opened", "failed", "sending")
MAX_SUBJECT = generation.MAX_SUBJECT_CHARS
MAX_BODY = generation.MAX_BODY_CHARS


class ReviewError(Exception):
    """The action is not allowed for this email right now; the message says why."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _transition(email_id: int, allowed_from: tuple[str, ...], **values) -> bool:
    return Email.objects.filter(pk=email_id, status__in=allowed_from).update(**values) == 1


def prospect_ready(email: Email) -> bool:
    return email.prospect.is_ready


def has_placeholder(email: Email) -> bool:
    return sending.has_placeholder({"subject": email.subject, "body_text": email.body_text, "body_html": email.body_html})


def editable_body(email: Email) -> str:
    return generation.editable_body(email.body_text)


def preview_html(email: Email) -> str:
    """The email as the recipient will see it, with the footer image loaded straight from Storage so viewing a
    preview never counts as an open."""
    footer = generation.footer_preview_url(generation.default_tracking_base_url()) or ""
    return generation.render_html(editable_body(email), footer)


def status_counts() -> dict[str, int]:
    counts = dict.fromkeys(STATUS_ORDER, 0)
    counts.update(dict(Email.objects.values_list("status").annotate(n=Count("id")).values_list("status", "n")))
    return counts


def open_rate(counts: dict[str, int]) -> int | None:
    sent_total = counts.get("sent", 0) + counts.get("opened", 0)
    return round(100 * counts.get("opened", 0) / sent_total) if sent_total else None


def next_in_review(after_id: int) -> int | None:
    queue = Email.objects.filter(status="in_review").order_by("id").values_list("id", flat=True)
    return queue.filter(id__gt=after_id).first() or queue.exclude(id=after_id).first()


def save_edit(email: Email, subject: str, body: str) -> str:
    subject, body = " ".join((subject or "").split()), (body or "").replace("\r\n", "\n").strip()
    if not subject or not body:
        raise ReviewError("Subject and body cannot be empty.")
    if len(subject) > MAX_SUBJECT or len(body) > MAX_BODY:
        raise ReviewError(f"Keep the subject under {MAX_SUBJECT} and the body under {MAX_BODY} characters.")
    base_url = generation.default_tracking_base_url()
    if not base_url:
        raise ReviewError("Cannot build the tracking URL; set EMAIL_TRACKING_BASE_URL.")
    changed = _transition(
        email.pk, EDITABLE,
        subject=subject,
        body_text=generation.render_text(body),
        body_html=generation.render_html(body, generation.tracking_url(base_url, email.tracking_token)),
        edited_at=_now(),
        status="in_review", reviewed_at=None,  # an edited email always needs a fresh approval
    )
    if not changed:
        raise ReviewError("This email can no longer be edited.")
    return "Saved." + (" It went back to review." if email.status != "in_review" else "")


def approve(email: Email) -> str:
    if has_placeholder(email):
        raise ReviewError("This email still contains placeholder text; edit it first.")
    if not _transition(email.pk, ("in_review",), status="approved", reviewed_at=_now(), review_note=None):
        raise ReviewError("Only emails in review can be approved.")
    return "Approved."


def reject(email: Email, note: str = "") -> str:
    if not _transition(email.pk, ("in_review", "approved"), status="rejected", reviewed_at=_now(),
                       review_note=(note or "").strip()[:2000] or None):
        raise ReviewError("Only emails in review or approved can be rejected.")
    return "Rejected."


def reopen(email: Email) -> str:
    if not _transition(email.pk, ("rejected", "approved"), status="in_review", reviewed_at=None):
        raise ReviewError("Only rejected or approved emails can go back to review.")
    return "Back in review."


def regenerate(email: Email, email_prompt: EmailPrompt | None = None) -> str:
    """Rewrite the email with the LLM (same tracking token); it goes back to review. Uses the given email prompt,
    else the one the email was written with, else its campaign's default."""
    if email.status not in generation.REGENERATABLE_STATUSES:
        raise ReviewError("Only emails in review or rejected can be regenerated.")
    llm = generation.make_llm_client()
    base_url = generation.default_tracking_base_url()
    if llm is None or not base_url:
        raise ReviewError("Set an LLM key (GROQ_API_KEY or OPENAI_API_KEY) and the tracking URL.")
    client, model, provider = llm
    rows = generation.load_prospects({"prospect_id": [email.prospect_id], "regenerate": True})
    if not rows:
        raise ReviewError("The prospect is no longer ready for outreach (check its status).")
    row = rows[0]
    system_prompt, email_prompt_id = generation.resolve_prompt(row, email_prompt or email.email_prompt)
    try:
        draft = generation.generate_draft(client, model, generation.prospect_context(row), system_prompt)
    except Exception as error:  # noqa: BLE001 - show any model/API failure to the reviewer
        raise ReviewError(f"Regeneration failed ({type(error).__name__}): {error}"[:300]) from error
    body_html = generation.render_html(draft.body, generation.tracking_url(base_url, row["tracking_token"]))
    if not generation.save_draft(row, draft, body_html, row["tracking_token"], provider, model, _now(), email_prompt_id):
        raise ReviewError("The email changed while it was being regenerated; nothing was saved.")
    return f"Regenerated with {provider} ({model})."


def send(email: Email, confirm: str) -> str:
    """Send one approved email now. `confirm` must be the recipient's address, typed by the person sending."""
    if email.status != "approved":
        raise ReviewError("Only approved emails can be sent.")
    if (confirm or "").strip().casefold() != email.recipient.casefold():
        raise ReviewError("Type the recipient's address exactly to confirm sending.")
    if has_placeholder(email):
        raise ReviewError("This email still contains placeholder text.")
    if not prospect_ready(email):
        raise ReviewError("The prospect is no longer ready for outreach; not sent.")
    if email.recipient.casefold() != email.prospect.email.casefold():
        raise ReviewError("The recipient no longer matches the prospect's verified address; not sent.")
    config = sending.smtp_config()
    problems = sending.config_problems(config)
    if problems:
        raise ReviewError("SMTP is not configured: " + "; ".join(problems))
    message = {field: getattr(email, field) for field in sending.EMAIL_FIELDS}
    mailer = sending.Mailer(config)
    try:
        outcome, detail = sending.send_one(message, config, mailer)
    finally:
        mailer.close()
    if outcome == "sent":
        return f"Sent to {email.recipient}."
    if outcome == "failed":
        raise ReviewError(f"The server rejected it; marked failed: {detail}")
    if outcome == "stop":
        raise ReviewError(f"Not sent, still approved: {detail}")
    raise ReviewError(f"Not sent: {detail}")


def bulk(action: str, ids: list[int]) -> tuple[int, int]:
    """Approve or reject many emails. Returns (done, skipped); skipped ones had the wrong status or placeholder text."""
    if action not in ("approve", "reject"):
        raise ReviewError("Unknown action; use approve or reject.")
    done = skipped = 0
    for email in Email.objects.filter(pk__in=ids).select_related("prospect"):
        try:
            approve(email) if action == "approve" else reject(email)
            done += 1
        except ReviewError:
            skipped += 1
    skipped += len(set(ids)) - done - skipped  # ids that do not exist
    return done, skipped


def search(queryset, text: str):
    text = (text or "").strip()
    if not text:
        return queryset
    return queryset.filter(Q(business__name__icontains=text) | Q(recipient__icontains=text) | Q(subject__icontains=text))
