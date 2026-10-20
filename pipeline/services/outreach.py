"""Scheduling, queueing and recording the emails this machine sends (pipeline/services/sending.py sends them).

1. Approval = a send time (`schedule`): after the latest send time of any campaign plus a fixed gap plus a random
   gap, inside the campaign's send window and the daily cap (Settings); a follow-up never before "previous email sent
   + its days". The time is stored on the email (`emails.scheduled_at`); the email stays Approved.
2. Queue (`queue`, the Outbox page): a person picks approved emails and queues them; only queued emails are sent. An
   email whose time already passed gets a fresh slot first, so a late queue never sends a burst. Queueing starts the
   sender (a background run) if it is not running; it sends each queued email at its time and stops when the queue
   is empty. Nothing runs on a timer.
3. Sent (`record_sent`): the email is marked sent, the lead moves on the pipeline (Initial Email / Follow-up) and the
   next follow-up is written for review right away; approving and queueing it sends it the same way.
4. Stop (`stop_prospect`): moving a lead to Replied, Demo Booked, Demo Done, Won, Lost or Quota Exhausted (Pipeline
   page) ends its sequence: emails not sent yet are cancelled. Moving it back to Follow-up resumes it
   (`resume_prospect`).
"""
import random
from datetime import date, datetime, time as day_time, timedelta, timezone
from zoneinfo import ZoneInfo

from django.db import transaction
from django.db.models import Max

from pipeline.models import DiscoveryCampaign, Email, FollowUp, Prospect, SendingSettings
from pipeline.services import generation

HOLDS_SLOT = ("approved", "queued", "sending", "sent", "opened", "replied")  # with scheduled_at set
QUEUEABLE = ("approved", "failed")  # failed: queue it again to retry
WAITING = ("queued", "sending")  # will be (or is being) sent from this machine
SENT_STATUSES = ("sent", "opened")
STUCK_AFTER = timedelta(minutes=15)  # "sending" this long: the sender stopped in the middle of an email


class OutreachError(RuntimeError):
    """The email cannot be scheduled, queued or recorded; the message says why."""


def now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- scheduling


def local_day_bounds(campaign: DiscoveryCampaign, day: date) -> tuple[datetime, datetime]:
    """Start and end of the campaign's sending on a local day: its send window, or the whole day."""
    tz = ZoneInfo(campaign.send_timezone)
    if campaign.send_window_start is not None and campaign.send_window_end is not None:
        return (datetime.combine(day, campaign.send_window_start, tzinfo=tz),
                datetime.combine(day, campaign.send_window_end, tzinfo=tz))
    return datetime.combine(day, day_time.min, tzinfo=tz), datetime.combine(day + timedelta(days=1), day_time.min, tzinfo=tz)


def slot_holders():
    """Emails holding a send time, in every campaign: they all go out from one mailbox."""
    return Email.objects.filter(status__in=HOLDS_SLOT, scheduled_at__isnull=False)


def next_slot(campaign: DiscoveryCampaign, earliest: datetime, rng: random.Random | None = None,
              sending: SendingSettings | None = None, exclude_id: int | None = None) -> datetime:
    """The send time for an email of the campaign: at least `earliest`, at least the fixed plus a random gap after the
    latest send time of any campaign, inside the campaign's send window, on a day when fewer than the daily cap (all
    campaigns together, the day in the campaign's time zone) are planned. The gaps and the cap are the global
    Settings. Call it inside a transaction that locked the settings row, so two approvals never take the same slot."""
    rng = rng or random
    sending = sending or SendingSettings.current()
    holders = slot_holders().exclude(pk=exclude_id) if exclude_id else slot_holders()
    latest = holders.aggregate(latest=Max("scheduled_at"))["latest"]
    candidate = earliest
    if latest is not None:
        gap = sending.gap_fixed_seconds + rng.uniform(sending.gap_random_min_seconds, sending.gap_random_max_seconds)
        candidate = max(candidate, latest + timedelta(seconds=gap))
    tz = ZoneInfo(campaign.send_timezone)
    for _ in range(366):
        day = candidate.astimezone(tz).date()
        begin, finish = local_day_bounds(campaign, day)
        if candidate < begin:
            candidate = begin
        if candidate >= finish:
            candidate = local_day_bounds(campaign, day + timedelta(days=1))[0]
            continue
        day_start = datetime.combine(day, day_time.min, tzinfo=tz)
        planned = holders.filter(scheduled_at__gte=day_start, scheduled_at__lt=day_start + timedelta(days=1))
        if planned.count() >= sending.daily_cap:
            candidate = local_day_bounds(campaign, day + timedelta(days=1))[0]
            continue
        return candidate
    raise OutreachError("No send slot found within a year; check the daily cap (Settings) and the campaign's send "
                        "window.")


def earliest_send(email: Email) -> datetime:
    """Now for an initial email; for a follow-up, the previous email's send time plus the step's days."""
    if email.sequence_step <= generation.SEQUENCE_STEP:
        return now()
    previous = Email.objects.filter(prospect_id=email.prospect_id, sequence_step=email.sequence_step - 1).first()
    if previous is None or previous.sent_at is None:
        raise OutreachError("The previous email of this follow-up has not been sent yet.")
    sequence_id = Prospect.objects.filter(pk=email.prospect_id).values_list("email_sequence_id", flat=True).first()
    step = FollowUp.objects.filter(email_sequence_id=sequence_id, sequence=email.sequence_step - 1).first()
    return max(now(), previous.sent_at + timedelta(days=step.days_after_previous if step else 0))


def schedule(email: Email, statuses: tuple[str, ...] = ("approved",)) -> datetime:
    """Give the email its send time (a fresh one if it had one). Returns the time."""
    SendingSettings.current()  # the row exists before it is locked
    with transaction.atomic():
        sending = SendingSettings.objects.select_for_update().get(pk=1)  # one at a time, all campaigns
        campaign = DiscoveryCampaign.objects.get(pk=email.business.discovery_campaign_id)
        slot = next_slot(campaign, earliest_send(email), sending=sending, exclude_id=email.pk)
        if not Email.objects.filter(pk=email.pk, status__in=statuses).update(scheduled_at=slot):
            raise OutreachError("Only approved emails can be scheduled.")
    email.scheduled_at = slot
    return slot


def local_time(email: Email) -> str:
    """The send time in the campaign's time zone, for messages."""
    if email.scheduled_at is None:
        return "no time yet"
    zone = email.business.discovery_campaign.send_timezone
    return f"{email.scheduled_at.astimezone(ZoneInfo(zone)).strftime('%Y-%m-%d %H:%M')} ({zone})"


# ---------------------------------------------------------------- the queue


def can_be_sent(email: Email) -> str | None:
    """Why this email may not go out now, or None."""
    prospect = email.prospect
    if prospect.do_not_contact:
        return "the prospect is do-not-contact"
    if prospect.sequence_stopped_at is not None:
        return f"the prospect's sequence was stopped ({prospect.sequence_stop_reason or 'stopped'})"
    if email.sequence_step > generation.SEQUENCE_STEP and not prospect.emails.filter(
            sequence_step=email.sequence_step - 1, status__in=(*SENT_STATUSES, "replied")).exists():
        return "the previous email of this follow-up has not been sent"
    return None


def queue(emails) -> dict:
    """Queue approved (or failed) emails for sending, in send-time order. An email whose time has passed gets a fresh
    slot first. Returns {"queued": n, "skipped": [(email id, reason), ...]}."""
    result = {"queued": 0, "skipped": []}
    for email in (Email.objects.filter(pk__in=[e.pk for e in emails])
                  .select_related("prospect", "business__discovery_campaign").order_by("scheduled_at", "id")):
        if email.status not in QUEUEABLE:
            result["skipped"].append((email.pk, f"it is {email.get_status_display().lower()}"))
            continue
        reason = can_be_sent(email)
        if reason:
            result["skipped"].append((email.pk, reason))
            continue
        try:
            if email.scheduled_at is None or email.scheduled_at < now():
                schedule(email, statuses=QUEUEABLE)
        except OutreachError as error:
            result["skipped"].append((email.pk, str(error)))
            continue
        if Email.objects.filter(pk=email.pk, status__in=QUEUEABLE).update(status="queued", queued_at=now(),
                                                                          send_error=None):
            result["queued"] += 1
    return result


def unqueue(emails) -> int:
    """Take queued emails out of the queue (back to Approved, keeping their send time)."""
    return Email.objects.filter(pk__in=[e.pk for e in emails], status="queued").update(status="approved", queued_at=None)


def next_queued():
    return Email.objects.filter(status="queued").order_by("scheduled_at", "id").first()


def stuck_emails():
    """Emails left "sending" by a sender that stopped mid-send: whether they went out is unknown."""
    return Email.objects.filter(status="sending", updated_at__lt=now() - STUCK_AFTER)


# ---------------------------------------------------------------- after sending


def write_next_followup(prospect: Prospect) -> str:
    """Fill in the prospect's next follow-up from its template, for review, right away. Returns what happened; a
    follow-up held back (a variable with no value) or a failure is left for Write follow-ups (dashboard) or the
    generate_followups command."""
    from pipeline.services import followups

    due = followups.find_due({"prospect_id": [prospect.pk]})
    if not due:
        return "no follow-up to write"
    try:
        outcome, detail = followups.write_followup(due[0])
    except Exception as error:  # noqa: BLE001 - the email was sent either way; report and let a person retry
        return f"follow-up not written ({type(error).__name__}); use Write follow-ups"
    if outcome == "saved":
        return f"follow-up {due[0]['sequence']} written for review"
    return f"follow-up {due[0]['sequence']} {'held back' if outcome == 'held' else outcome}: {detail}"


def has_next_step(prospect: Prospect, sent_step: int) -> bool:
    """Whether the prospect's sequence has a follow-up after email `sent_step` (a step without a template ends it)."""
    return (prospect.sequence_stopped_at is None and not prospect.do_not_contact
            and FollowUp.objects.filter(email_sequence_id=prospect.email_sequence_id, sequence=sent_step,
                                        email_prompt__isnull=False).exists())


def record_sent(email: Email, message_id: str | None, sent_from: str, write_followup: bool = True) -> dict:
    """The email went out: mark it sent, move the lead on the pipeline and write the next follow-up for review."""
    from pipeline.services import stages

    sent_at = now()
    Email.objects.filter(pk=email.pk).update(status="sent", sent_at=sent_at, message_id=message_id,
                                             sent_from=sent_from, send_error=None)
    Prospect.objects.filter(pk=email.prospect_id).update(outreach_status="contacted", last_contacted_at=sent_at)
    prospect = Prospect.objects.get(pk=email.prospect_id)
    first = email.sequence_step == generation.SEQUENCE_STEP
    stages.advance(prospect, stages.PipelineStage.INITIAL_EMAIL if first else stages.PipelineStage.FOLLOW_UP,
                   note="email sent" if first else f"follow-up {email.sequence_step - 1} sent")
    result = {"email": email.pk, "next": "sequence finished"}
    if has_next_step(prospect, email.sequence_step):
        result["next"] = f"follow-up {email.sequence_step}"
        if write_followup:
            result["follow_up"] = write_next_followup(prospect)
    return result


# ---------------------------------------------------------------- stopping and resuming


STOP_NOTE = "Sequence stopped ("  # the review note stop_prospect leaves on the emails it cancels


def stop_prospect(prospect: Prospect, reason: str = "stopped") -> int:
    """No more emails to this prospect: its sequence is stopped and emails not sent yet are cancelled (an email being
    sent right now still goes). Returns how many emails were cancelled."""
    reason = (reason or "stopped").strip()[:64]
    Prospect.objects.filter(pk=prospect.pk, sequence_stopped_at__isnull=True).update(
        sequence_stopped_at=now(), sequence_stop_reason=reason)
    prospect.refresh_from_db()
    return Email.objects.filter(prospect=prospect, status__in=("in_review", "approved", "queued", "failed")).update(
        status="rejected", review_note=f"{STOP_NOTE}{reason}).", scheduled_at=None, queued_at=None)


def resume_prospect(prospect: Prospect) -> dict:
    """Undo a stop made by mistake (e.g. an automatic reply taken for a reply): the email marked replied counts as
    sent again, the emails the stop cancelled go back to review (approve and queue them again), and the next
    follow-up is written for review if it does not exist yet. Does nothing for a prospect that is not stopped."""
    if prospect.sequence_stopped_at is None:
        return {"prospect": prospect.pk, "outcome": "not stopped; nothing to resume"}
    if prospect.do_not_contact:
        raise OutreachError("This prospect is marked do-not-contact; clear that first.")
    previous_reason = prospect.sequence_stop_reason
    with transaction.atomic():
        unreplied = Email.objects.filter(prospect=prospect, status="replied").update(status="sent")
        restored = Email.objects.filter(prospect=prospect, status="rejected", review_note__startswith=STOP_NOTE).update(
            status="in_review", review_note=None, reviewed_at=None, scheduled_at=None)
        Prospect.objects.filter(pk=prospect.pk).update(sequence_stopped_at=None, sequence_stop_reason=None)
    prospect.refresh_from_db()
    latest_sent = prospect.emails.filter(status__in=SENT_STATUSES).order_by("-sequence_step").first()
    result = {"prospect": prospect.pk, "outcome": "resumed", "previous_stop_reason": previous_reason,
              "unmarked_replied": unreplied, "back_in_review": restored, "next": "sequence finished"}
    if latest_sent and has_next_step(prospect, latest_sent.sequence_step):
        result["next"] = f"follow-up {latest_sent.sequence_step}"
        if not prospect.emails.filter(sequence_step=latest_sent.sequence_step + 1).exists():
            result["follow_up"] = write_next_followup(prospect)
    return result
