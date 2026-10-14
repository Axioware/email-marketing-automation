"""Reviewing generated emails: edit, approve, reject, reopen, regenerate. Used by the admin and the API.

Every change is a conditional update on the email's current status, so two reviewers can never move an email through
an invalid transition. Editing always sends an email back to review. This app does not send emails: approved ones
wait for whichever sending method is chosen.
"""
from datetime import datetime, timezone

from django.db.models import Count, Q

from pipeline.models import Business, Email, EmailPrompt
from pipeline.services import generation

EDITABLE = ("in_review", "approved", "rejected")
STATUS_ORDER = ("in_review", "approved", "rejected", "sent", "opened", "replied", "failed", "sending")
MAX_SUBJECT = generation.MAX_SUBJECT_CHARS
MAX_BODY = generation.MAX_BODY_CHARS


class ReviewError(Exception):
    """The action is not allowed for this email right now; the message says why."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _transition(email_id: int, allowed_from: tuple[str, ...], **values) -> bool:
    return Email.objects.filter(pk=email_id, status__in=allowed_from).update(**values) == 1


def prospect_ready(email: Email) -> bool:
    """Whether this email may still be sent to its prospect (a follow-up also goes to an already contacted one)."""
    prospect = email.prospect
    statuses = ("ready",) if email.sequence_step == generation.SEQUENCE_STEP else ("ready", "contacted")
    return (prospect.email_status == "deliverable" and prospect.outreach_status in statuses
            and not prospect.do_not_contact and email.business.replied_at is None)


def mark_replied(email: Email) -> str:
    """Record a reply to a sent email: the email becomes Replied and its business gets no more follow-ups."""
    if email.status == "replied":
        return "Already marked as replied."
    if not _transition(email.pk, ("sent", "opened"), status="replied"):
        raise ReviewError("Only sent or opened emails can be marked replied.")
    Business.objects.filter(pk=email.business_id, replied_at__isnull=True).update(replied_at=_now())
    return "Marked replied. No more follow-ups go to this business."


def mark_business_replied(business: Business, prospect_id: int | None = None) -> str:
    """Mark the latest sent email of a business (or of one of its prospects) as replied; with no sent email, just
    stop follow-ups for the business."""
    emails = Email.objects.filter(business=business, status__in=("sent", "opened"))
    if prospect_id is not None:
        emails = emails.filter(prospect_id=prospect_id)
    latest = emails.order_by("-sent_at", "-id").first()
    if latest is not None:
        return mark_replied(latest)
    Business.objects.filter(pk=business.pk, replied_at__isnull=True).update(replied_at=_now())
    return "Marked replied. No more follow-ups go to this business."


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
    if email.sequence_step > generation.SEQUENCE_STEP:  # a follow-up: rewritten from the thread so far
        from pipeline.services import followups

        try:
            provider, model = followups.regenerate(email, email_prompt)
        except ValueError as error:
            raise ReviewError(str(error)) from error
        except Exception as error:  # noqa: BLE001 - show any model/API failure to the reviewer
            raise ReviewError(f"Regeneration failed ({type(error).__name__}): {error}"[:300]) from error
        return f"Regenerated with {provider} ({model})."
    llm = generation.make_llm_client()
    if llm is None:
        raise ReviewError("Set an LLM key (GROQ_API_KEY or OPENAI_API_KEY).")
    client, model, provider = llm
    rows = generation.load_prospects({"prospect_id": [email.prospect_id], "regenerate": True})
    if not rows:
        raise ReviewError("The prospect is no longer ready for outreach (check its status).")
    row = rows[0]
    generation.assign_variation(row)
    resolved = generation.resolve_prompt(row, email_prompt or email.email_prompt)
    if resolved is None:
        raise ReviewError(f"Cannot regenerate: {generation.missing_prompt_reason(row)}.")
    system_prompt, email_prompt_id = resolved
    try:
        draft = generation.generate_draft(client, model, generation.prospect_context(row), system_prompt)
    except Exception as error:  # noqa: BLE001 - show any model/API failure to the reviewer
        raise ReviewError(f"Regeneration failed ({type(error).__name__}): {error}"[:300]) from error
    body_html = generation.render_html(draft.body, generation.footer_image_url(row["tracking_token"]))
    if not generation.save_draft(row, draft, body_html, row["tracking_token"], provider, model, _now(), email_prompt_id):
        raise ReviewError("The email changed while it was being regenerated; nothing was saved.")
    return f"Regenerated with {provider} ({model})."


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
