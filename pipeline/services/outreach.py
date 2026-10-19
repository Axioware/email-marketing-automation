"""Handing approved emails to GoHighLevel, which waits until their time and sends them. Nothing runs on a timer.

1. Contacts: every ready prospect (one sequence per email address) becomes a GHL contact tagged `outreach-app` and
   its campaign (`sync_contacts`: the full pipeline's last step, the ghl_sync command, approval); a GHL workflow
   turns new tagged contacts into New Lead opportunities.
2. Approval = hand-over (`hand_over`): the email gets its send time (`schedule`): the campaign's previous slot plus
   a fixed gap plus a random gap, inside the campaign's send window and daily cap; a follow-up never before
   "previous email sent + its days". The time is stored here (`emails.scheduled_at`) and written with the email
   into the contact at once: Email Subject, Email Body, Follow-up Count (0 = initial, n = follow-up n), Email Due
   At and, for an initial email, Initial Email Type. Status: scheduled. GHL waits until Email Due At and sends.
   A write that fails leaves the email approved, with the error shown; handing it over again first reads the
   contact and does not write again if GHL already has it (so a timed-out write is never sent twice).
3. Sent (`record_sent`, the email-sent webhook): the email is marked sent; the contact's Email Subject, Body and Due
   At are emptied and Follow-up Count set to the next follow-up's number, or emptied too when the sequence is over
   (the workflow then moves the lead to Quota Exhausted). The next follow-up is then written for review; approving
   it hands it over the same way.
4. Stop (`stop_sequence`, the stop webhook): a reply or a manual stage move ends the prospect's sequence: emails not
   sent yet are cancelled and the contact's fields emptied.
"""
import random
import re
import time
from datetime import date, datetime, time as day_time, timedelta, timezone
from zoneinfo import ZoneInfo

from django.db import transaction
from django.db.models import Max, Q
from django.utils.text import slugify

from pipeline.models import DiscoveryCampaign, Email, FollowUp, Prospect, SendingSettings
from pipeline.services import generation, ghl

HOLDS_SLOT = ("approved", "scheduled", "sending", "sent", "opened", "replied")  # with scheduled_at set
HANDED = ("scheduled", "sending")  # in GoHighLevel, waiting to be sent
SENT_STATUSES = ("sent", "opened")
SYNC_BATCH = 200  # contacts created per call
STUCK_AFTER = timedelta(days=1)  # due this long ago and never reported sent: shown as a problem


class OutreachError(RuntimeError):
    """The email cannot be scheduled, handed over or recorded; the message says why."""


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
              sending: SendingSettings | None = None) -> datetime:
    """The send time for a newly approved email of the campaign: at least `earliest`, at least the fixed plus a
    random gap after the latest send time of any campaign, inside the campaign's send window, on a day when fewer
    than the daily cap (all campaigns together, the day in the campaign's time zone) are planned. The gaps and the
    cap are the global Sending settings. Call it inside a transaction that locked the settings row, so two approvals
    never take the same slot."""
    rng = rng or random
    sending = sending or SendingSettings.current()
    latest = slot_holders().aggregate(latest=Max("scheduled_at"))["latest"]
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
        planned = slot_holders().filter(scheduled_at__gte=day_start, scheduled_at__lt=day_start + timedelta(days=1))
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
    step = FollowUp.objects.filter(email_sequence_id=email.prospect.email_sequence_id,
                                   sequence=email.sequence_step - 1).first()
    return max(now(), previous.sent_at + timedelta(days=step.days_after_previous if step else 0))


def schedule(email: Email) -> datetime:
    """Give an approved email its send time (kept while it is approved or handed over). Returns the time."""
    SendingSettings.current()  # the row exists before it is locked
    with transaction.atomic():
        sending = SendingSettings.objects.select_for_update().get(pk=1)  # one approval at a time, all campaigns
        campaign = DiscoveryCampaign.objects.get(pk=email.business.discovery_campaign_id)
        slot = next_slot(campaign, earliest_send(email), sending=sending)
        if not Email.objects.filter(pk=email.pk, status="approved").update(scheduled_at=slot):
            raise OutreachError("Only approved emails can be scheduled.")
    email.scheduled_at = slot
    return slot


# ---------------------------------------------------------------- GoHighLevel fields


def body_for_ghl(email: Email) -> str:
    """The email's HTML without the document wrapper, for the Email Body field (paragraphs and footer kept)."""
    match = re.search(r"<body[^>]*>(.*)</body>", email.body_html or "", re.S | re.I)
    return (match.group(1) if match else email.body_html or "").strip()


def due_value(email: Email) -> str | None:
    """Email Due At: the send time in the campaign's time zone, ISO format."""
    if email.scheduled_at is None:
        return None
    zone = ZoneInfo(email.business.discovery_campaign.send_timezone)
    return email.scheduled_at.astimezone(zone).replace(microsecond=0).isoformat()


def initial_type(email: Email) -> str | None:
    """Initial Email Type: the prospect's sequence (its A/B group), written with the first email."""
    if email.sequence_step != generation.SEQUENCE_STEP:
        return None
    sequence = email.prospect.email_sequence
    return sequence.name if sequence else (email.email_prompt.name if email.email_prompt else None)


def email_fields(email: Email) -> dict:
    values = {"subject": email.subject, "body": body_for_ghl(email),
              "count": email.sequence_step - generation.SEQUENCE_STEP, "due": due_value(email)}
    if initial_type(email):
        values["type"] = initial_type(email)
    return values


def contact_payload(prospect: Prospect) -> dict:
    contact, business = prospect.contact, prospect.business
    first = (contact.first_name or "").strip() or None
    last = (contact.last_name or "").strip() or None
    if not first and contact.name:
        first, _, rest = contact.name.strip().partition(" ")
        last = last or rest or None
    payload = {"firstName": first, "lastName": last, "email": prospect.email, "companyName": business.name,
               "website": business.website_url, "city": business.city, "source": "Outreach app",
               "tags": [ghl.OUTREACH_TAG, f"campaign-{slugify(business.discovery_campaign.name)[:40]}"]}
    return {key: value for key, value in payload.items() if value}


def link_contact(prospect: Prospect, contact_id: str) -> None:
    """Store the GHL contact on the prospect; refuse a contact another prospect already uses (GHL matches contacts
    by email, and two sequences in one contact would overwrite each other's emails)."""
    other = Prospect.objects.filter(ghl_contact_id=contact_id).exclude(pk=prospect.pk).first()
    if other is not None:
        raise OutreachError(f"GoHighLevel contact {contact_id} already belongs to prospect {other.pk} "
                            f"({other.email}); one email address gets one sequence.")
    Prospect.objects.filter(pk=prospect.pk).update(ghl_contact_id=contact_id)
    prospect.ghl_contact_id = contact_id


def ensure_contact(prospect: Prospect, client: ghl.Client) -> str:
    """The prospect's GHL contact id, creating (or matching by email) the contact when it has none."""
    if not prospect.ghl_contact_id:
        contact_id, _ = client.upsert_contact(contact_payload(prospect))
        link_contact(prospect, contact_id)
    return prospect.ghl_contact_id


def load(email_id: int) -> Email:
    return Email.objects.select_related(
        "prospect__contact", "prospect__business__discovery_campaign", "business__discovery_campaign",
        "email_prompt").get(pk=email_id)


def hand_over(email: Email, client: ghl.Client | None = None) -> str:
    """Write an approved email into its contact (status approved -> scheduled); GHL waits until Email Due At and
    sends it. Raises OutreachError (or ghl.GhlError) and leaves it approved, with the error kept, when it cannot."""
    email = load(email.pk)
    if email.status != "approved":
        raise OutreachError(f"Only approved emails are handed over (this one is {email.get_status_display()}).")
    prospect = email.prospect
    if prospect.sequence_stopped_at is not None or prospect.do_not_contact:
        raise OutreachError("The prospect can no longer be emailed (stopped or do-not-contact).")
    waiting = prospect.emails.filter(status__in=HANDED).exclude(pk=email.pk).first()
    if waiting is not None:
        raise OutreachError(f"Email {waiting.pk} of this prospect is still waiting in GoHighLevel.")
    if email.scheduled_at is None:
        schedule(email)
    client = client or ghl.Client()
    values = email_fields(email)
    try:
        contact_id = ensure_contact(prospect, client)
        current = client.get_fields(contact_id)
        already = (current.get("subject") == values["subject"] and current.get("count") in (values["count"],
                                                                                           str(values["count"])))
        if not already:  # a timed-out write that GHL did save is never written (and sent) a second time
            client.set_fields(contact_id, values)
    except (ghl.GhlError, OutreachError) as error:
        Email.objects.filter(pk=email.pk).update(ghl_error=str(error)[:2000])
        raise
    Email.objects.filter(pk=email.pk, status="approved").update(status="scheduled", released_at=now(), ghl_error=None)
    return due_value(email)


def withdraw(email: Email, client: ghl.Client | None = None) -> None:
    """Take a handed-over email back out of GoHighLevel before it is sent (edit, reject, back to review): its
    subject, body and due date are emptied, the Follow-up Count stays (emptying it would end the sequence). Raises
    if GHL cannot be reached, so the email is never shown as withdrawn while GHL could still send it."""
    email = load(email.pk)
    may_be_in_ghl = email.status in HANDED or (email.status == "approved" and email.ghl_error)  # a timed-out write
    if not may_be_in_ghl or not email.prospect.ghl_contact_id or not ghl.configured():
        return
    try:
        (client or ghl.Client()).set_fields(email.prospect.ghl_contact_id, {"subject": None, "body": None, "due": None})
    except ghl.GhlError as error:
        raise OutreachError(f"Could not take it back out of GoHighLevel ({error}); it may still be sent. "
                            "Try again.") from error


# ---------------------------------------------------------------- contacts


def sync_contacts(limit: int = SYNC_BATCH, deadline: float | None = None) -> dict:
    """Create GHL contacts for ready prospects that have none yet (with a deadline, only while there is time)."""
    result = {"created": 0, "matched": 0, "failed": 0}
    pending = list(generation.ready_prospects().filter(ghl_contact_id__isnull=True)
                   .select_related("contact", "business__discovery_campaign").order_by("id")[:limit])
    if not pending:
        return result
    client = ghl.Client()
    for prospect in pending:
        if deadline is not None and time.monotonic() > deadline:
            result["left"] = result.get("left", 0) + 1
            continue
        try:
            contact_id, new = client.upsert_contact(contact_payload(prospect))
            link_contact(prospect, contact_id)
        except (ghl.GhlError, OutreachError):
            result["failed"] += 1
            continue
        result["created" if new else "matched"] += 1
    return result


# ---------------------------------------------------------------- webhooks


def prospect_for_contact(contact_id: str) -> Prospect:
    prospect = Prospect.objects.filter(ghl_contact_id=contact_id).select_related("business__discovery_campaign").first()
    if prospect is None:
        raise OutreachError(f"No prospect has GoHighLevel contact {contact_id}.")
    return prospect


def sequence_state(prospect: Prospect, sent_step: int) -> dict:
    """The contact's fields after email `sent_step` went out: the next follow-up's number, or all empty (done)."""
    has_next = (prospect.sequence_stopped_at is None and not prospect.do_not_contact
                and FollowUp.objects.filter(email_sequence_id=prospect.email_sequence_id, sequence=sent_step,
                                            email_prompt__isnull=False).exists())  # a step without a template ends it
    return {"subject": None, "body": None, "due": None, "count": sent_step if has_next else None}


def write_next_followup(prospect: Prospect) -> str:
    """Fill in the prospect's next follow-up from its template, for review, right away (the webhook does this;
    nothing runs on a timer). Returns what happened; a follow-up held back (a variable with no value) or a failure is
    left for Write follow-ups (dashboard) or the generate_followups command."""
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


def record_sent(contact_id: str, followup_count=None, client: ghl.Client | None = None,
                write_followup: bool = True) -> dict:
    """The email-sent webhook: mark the email GHL just sent, set the contact's fields for what comes next, then
    write the next follow-up for review. Safe to call twice for the same email (GHL may retry a webhook)."""
    prospect = prospect_for_contact(contact_id)
    emails = prospect.emails.all()
    handed = Q(status__in=HANDED) | Q(status="approved", scheduled_at__isnull=False)  # approved: a timed-out write
    if followup_count not in (None, ""):
        try:
            step = int(float(followup_count)) + generation.SEQUENCE_STEP
        except (TypeError, ValueError) as error:
            raise OutreachError(f"Follow-up Count must be a number, got {followup_count!r}.") from error
        email = emails.filter(sequence_step=step).first()
    else:
        email = emails.filter(handed).order_by("sequence_step").first() \
            or emails.filter(status__in=SENT_STATUSES).order_by("-sequence_step").first()
    if email is None:
        raise OutreachError("This contact has no email waiting to be sent.")
    if Email.objects.filter(pk=email.pk).filter(handed).exists():
        sent_at = now()
        if Email.objects.filter(pk=email.pk).filter(handed).update(status="sent", sent_at=sent_at, ghl_error=None):
            Prospect.objects.filter(pk=prospect.pk).update(outreach_status="contacted", last_contacted_at=sent_at)
            outcome = "recorded"
        else:
            outcome = "already recorded"
    elif email.status in (*SENT_STATUSES, "replied"):
        outcome = "already recorded"
    else:
        raise OutreachError(f"Email {email.pk} was not handed to GoHighLevel (status {email.status}).")
    prospect.refresh_from_db()
    state = sequence_state(prospect, email.sequence_step)
    (client or ghl.Client()).set_fields(contact_id, state)
    result = {"email": email.pk, "outcome": outcome,
              "next": f"follow-up {state['count']}" if state["count"] is not None else "sequence finished"}
    if write_followup and state["count"] is not None:
        result["follow_up"] = write_next_followup(prospect)
    return result


def stop_prospect(prospect: Prospect, reason: str = "stopped", client: ghl.Client | None = None,
                  raise_errors: bool = False) -> int:
    """No more emails to this prospect: its sequence is stopped, emails not sent yet are cancelled and its GHL
    sending fields emptied. Returns how many emails were cancelled."""
    reason = (reason or "stopped").strip()[:64]
    Prospect.objects.filter(pk=prospect.pk, sequence_stopped_at__isnull=True).update(
        sequence_stopped_at=now(), sequence_stop_reason=reason)
    cancelled = Email.objects.filter(prospect=prospect, status__in=("in_review", "approved", *HANDED)).update(
        status="rejected", review_note=f"{STOP_NOTE}{reason}).", scheduled_at=None)
    if prospect.ghl_contact_id and ghl.configured():
        try:
            (client or ghl.Client()).set_fields(prospect.ghl_contact_id,
                                                {"subject": None, "body": None, "due": None, "count": None})
        except ghl.GhlError:
            if raise_errors:
                raise
    return cancelled


def stop_sequence(contact_id: str, reason: str = "stopped", client: ghl.Client | None = None) -> dict:
    """The stop webhook (a reply, a bounce, an unsubscribe or a manual stage move in GHL): no more emails to this
    prospect. A reply also marks the latest sent email replied."""
    prospect = prospect_for_contact(contact_id)
    reason = (reason or "stopped").strip()[:64]
    if reason.lower().startswith("repl"):
        latest = prospect.emails.filter(status__in=SENT_STATUSES).order_by("-sequence_step").first()
        if latest is not None:
            Email.objects.filter(pk=latest.pk, status__in=SENT_STATUSES).update(status="replied")
    cancelled = stop_prospect(prospect, reason, client or ghl.Client(), raise_errors=True)
    return {"prospect": prospect.pk, "reason": reason, "cancelled": cancelled}


STOP_NOTE = "Sequence stopped ("  # the review note stop_prospect leaves on the emails it cancels


def resume_sequence(contact_id: str, client: ghl.Client | None = None) -> dict:
    """The resume webhook: the stop was a mistake (e.g. an automatic reply was taken for a reply). Undoes
    stop_sequence: the email marked replied is sent again, the emails the stop cancelled go back to review (approve
    them again to hand them over), the contact's Follow-up Count is set for the next follow-up, and that follow-up is
    written for review if it does not exist yet. Does nothing for a prospect whose sequence is not stopped, so a
    workflow may call it on every move to the Follow-up stage."""
    prospect = prospect_for_contact(contact_id)
    if prospect.sequence_stopped_at is None:
        return {"prospect": prospect.pk, "outcome": "not stopped; nothing to resume"}
    if prospect.do_not_contact:
        raise OutreachError("This prospect is marked do-not-contact; clear that in the admin before resuming.")
    previous_reason = prospect.sequence_stop_reason
    with transaction.atomic():
        unreplied = Email.objects.filter(prospect=prospect, status="replied").update(status="sent")
        restored = Email.objects.filter(prospect=prospect, status="rejected", review_note__startswith=STOP_NOTE).update(
            status="in_review", review_note=None, reviewed_at=None, scheduled_at=None)
        Prospect.objects.filter(pk=prospect.pk).update(sequence_stopped_at=None, sequence_stop_reason=None)
    prospect.refresh_from_db()
    latest_sent = prospect.emails.filter(status__in=SENT_STATUSES).order_by("-sequence_step").first()
    state = (sequence_state(prospect, latest_sent.sequence_step) if latest_sent
             else {"subject": None, "body": None, "due": None, "count": None})
    (client or ghl.Client()).set_fields(contact_id, state)
    result = {"prospect": prospect.pk, "outcome": "resumed", "previous_stop_reason": previous_reason,
              "unmarked_replied": unreplied, "back_in_review": restored,
              "next": f"follow-up {state['count']}" if state["count"] is not None else "sequence finished"}
    if state["count"] is not None and not prospect.emails.filter(
            sequence_step=latest_sent.sequence_step + 1).exists():
        result["follow_up"] = write_next_followup(prospect)
    return result


# ---------------------------------------------------------------- health


def stuck_emails():
    """Emails in GoHighLevel whose send time passed over a day ago and that it never reported sent: the sending
    workflow is missing, not published, or its webhook fails."""
    return Email.objects.filter(status__in=HANDED, scheduled_at__lt=now() - STUCK_AFTER)


def not_handed_over():
    """Approved emails not in GoHighLevel yet (a failed or interrupted hand-over): hand them over again."""
    return Email.objects.filter(status="approved")
