"""Reviewing emails filled from templates: edit, approve, reject, reopen, fill in again. Used by the admin and the API.

Every change is a conditional update on the email's current status, so two reviewers can never move an email through
an invalid transition. Editing always sends an email back to review. Approving gives the email its send time; queueing it
on the Outbox page sends it then (pipeline/services/outreach.py, sending.py).
"""
from datetime import datetime, timezone

from django.db.models import Count, Q

from pipeline.models import Business, Email, EmailPrompt
from pipeline.services import generation, outreach, stages, templates

EDITABLE = ("in_review", "approved", "queued", "rejected", "failed")
NOT_SENT = ("in_review", "approved", "queued", "failed")  # may still be rejected
STATUS_ORDER = ("in_review", "approved", "queued", "sending", "failed", "rejected", "sent", "opened", "replied")
LEGACY_STATUSES = ("opened",)  # open tracking; off
MAX_SUBJECT = generation.MAX_SUBJECT_CHARS
MAX_BODY = generation.MAX_BODY_CHARS


class ReviewError(Exception):
    """The action is not allowed for this email right now; the message says why."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _transition(email_id: int, allowed_from: tuple[str, ...], **values) -> bool:
    return Email.objects.filter(pk=email_id, status__in=allowed_from).update(**values) == 1


def prospect_ready(email: Email) -> bool:
    """Whether this email may still go to its prospect (a follow-up also goes to an already contacted one)."""
    prospect = email.prospect
    statuses = ("ready",) if email.sequence_step == generation.SEQUENCE_STEP else ("ready", "contacted")
    return ((prospect.email_status == "deliverable" or prospect.approved_by_person)
            and prospect.outreach_status in statuses and not prospect.do_not_contact
            and prospect.sequence_stopped_at is None)


def mark_replied(email: Email, user=None) -> str:
    """Record a reply to a sent email: the email becomes Replied, the lead moves to Replied on the pipeline and its
    prospect gets no more emails (each prospect has its own sequence; others at the same business are not affected)."""
    if email.status == "replied":
        return "Already marked as replied."
    if not _transition(email.pk, ("sent", "opened"), status="replied"):
        raise ReviewError("Only sent or opened emails can be marked replied.")
    stages.move(email.prospect, stages.PipelineStage.REPLIED, user, note="marked replied")
    return "Marked replied. No more emails go to this prospect."


def mark_business_replied(business: Business, prospect_id: int | None = None) -> str:
    """Mark the latest sent email of a business (or of one of its prospects) as replied."""
    emails = Email.objects.filter(business=business, status__in=("sent", "opened"))
    if prospect_id is not None:
        emails = emails.filter(prospect_id=prospect_id)
    latest = emails.order_by("-sent_at", "-id").first()
    if latest is None:
        raise ReviewError("No sent email to mark as replied.")
    return mark_replied(latest)


PLACEHOLDER_MARKER = "PLACEHOLDER"


def has_placeholder(email: Email) -> bool:
    """Text left for a person to fill in; such an email cannot be approved."""
    return any(PLACEHOLDER_MARKER in (getattr(email, field) or "") for field in ("subject", "body_text", "body_html"))


def editable_body(email: Email) -> str:
    return generation.editable_body(email.body_text)


def preview_html(email: Email) -> str:
    """The email as the recipient will see it, with the footer image loaded straight from Storage so viewing a
    preview never counts as an open."""
    footer = generation.footer_preview_url(generation.default_tracking_base_url()) if generation.OPEN_TRACKING else None
    return generation.render_html(editable_body(email), footer)


def status_counts() -> dict[str, int]:
    counts = dict.fromkeys(STATUS_ORDER, 0)
    counts.update(dict(Email.objects.values_list("status").annotate(n=Count("id")).values_list("status", "n")))
    return counts


def open_rate(counts: dict[str, int]) -> int | None:
    """Percentage of sent emails that were opened; None while open tracking is off or nothing was sent."""
    if not generation.OPEN_TRACKING:
        return None
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
    if generation.OPEN_TRACKING and not generation.default_tracking_base_url():
        raise ReviewError("Cannot build the tracking URL; set EMAIL_TRACKING_BASE_URL.")
    changed = _transition(
        email.pk, EDITABLE,
        subject=subject,
        body_text=generation.render_text(body),
        body_html=generation.render_html(body, generation.footer_image_url(email.tracking_token)),
        edited_at=_now(),
        status="in_review", reviewed_at=None, scheduled_at=None, queued_at=None,  # edited: approve it again
    )
    if not changed:
        raise ReviewError("This email can no longer be edited.")
    return "Saved." + (" It went back to review." if email.status != "in_review" else "")


def approve(email: Email) -> str:
    """Approve: the email gets its send time. Queue it on the Outbox page to send it then."""
    if has_placeholder(email):
        raise ReviewError("This email still contains placeholder text; edit it first.")
    if not prospect_ready(email):
        raise ReviewError("The prospect can no longer be emailed (do-not-contact, stopped or not verified).")
    if not _transition(email.pk, ("in_review",), status="approved", reviewed_at=_now(), review_note=None):
        raise ReviewError("Only emails in review can be approved.")
    try:
        outreach.schedule(email)
    except outreach.OutreachError as error:
        _transition(email.pk, ("approved",), status="in_review", reviewed_at=None)
        raise ReviewError(f"Not approved: {error}") from error
    if email.sequence_step == generation.SEQUENCE_STEP:
        stages.advance(email.prospect, stages.PipelineStage.READY_TO_SEND, note="first email approved")
    return f"Approved for {outreach.local_time(email)}. Queue it in the Outbox to send it then."


def queue(emails) -> str:
    """Queue emails for sending and make sure the sender runs."""
    from pipeline import jobs

    result = outreach.queue(emails)
    message = f"{result['queued']} email(s) queued."
    if result["already"]:
        message += f" {result['already']} were already queued."
    if result["skipped"]:
        message += " Skipped: " + "; ".join(f"#{pk} ({why})" for pk, why in result["skipped"][:10])
        message += "." if len(result["skipped"]) <= 10 else f" and {len(result['skipped']) - 10} more."
    if result["queued"] or Email.objects.filter(status="queued").exists():
        from pipeline.services import sending

        if not sending.configured():
            return message + f" Sending is not set up (add {sending.missing_settings()}), so they wait in the queue."
        run, started = jobs.ensure_sender()
        message += f" The sender {'started' if started else 'is running'} (run #{run.pk}); each email goes at its time."
    return message


def unqueue(emails) -> str:
    return f"{outreach.unqueue(emails)} email(s) taken out of the queue (still approved, same times)."


def reject(email: Email, note: str = "") -> str:
    if not _transition(email.pk, NOT_SENT, status="rejected", reviewed_at=_now(),
                       review_note=(note or "").strip()[:2000] or None, scheduled_at=None, queued_at=None):
        raise ReviewError("Only emails not sent yet can be rejected.")
    return "Rejected."


def reopen(email: Email) -> str:
    if not _transition(email.pk, ("rejected", "approved", "queued", "failed"), status="in_review", reviewed_at=None,
                       scheduled_at=None, queued_at=None):
        raise ReviewError("Only rejected, approved, queued or failed emails can go back to review.")
    return "Back in review."


def regenerate(email: Email, email_prompt: EmailPrompt | None = None) -> str:
    """Fill the email again from its template (same tracking token); it goes back to review. Uses the given template,
    else the one the email was filled from, else its campaign's."""
    if email.status not in generation.REGENERATABLE_STATUSES:
        raise ReviewError("Only emails in review or rejected can be filled in again.")
    if email.sequence_step > generation.SEQUENCE_STEP:  # a follow-up
        from pipeline.services import followups

        try:
            name = followups.regenerate(email, email_prompt)
        except ValueError as error:
            raise ReviewError(str(error)) from error
        return f"Filled in again from {name}."
    rows = generation.load_prospects({"prospect_id": [email.prospect_id], "regenerate": True})
    if not rows:
        raise ReviewError("The prospect is no longer ready for outreach (check its status).")
    row = rows[0]
    if email_prompt is None and email.email_prompt is None and generation.assign_sequence(row) is None:
        raise ReviewError(f"Cannot fill it in: {generation.missing_prompt_reason(row)}.")
    template = generation.resolve_template(row, email_prompt or email.email_prompt)
    if template is None:
        raise ReviewError(f"Cannot fill it in: {generation.missing_prompt_reason(row)}.")
    try:
        draft = generation.fill_template(template, row)
    except templates.TemplateIncomplete as error:
        raise ReviewError(f"Not filled in ({template}): {error}.") from error
    body_html = generation.render_html(draft.body, generation.footer_image_url(row["tracking_token"]))
    if not generation.save_draft(row, draft, body_html, row["tracking_token"], "template", template.name, _now(),
                                 template.pk):
        raise ReviewError("The email changed while it was being filled in; nothing was saved.")
    return f"Filled in again from {template}."


def bulk(action: str, ids: list[int]) -> tuple[int, int]:
    """Approve or reject many emails. Returns (done, skipped); skipped ones had the wrong status or placeholder text.
    (Queueing many emails: queue().)"""
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
