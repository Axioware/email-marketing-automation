"""Handing approved emails to GoHighLevel, which sends them.

1. Contacts: every ready prospect (one address per person) becomes a GHL contact tagged `outreach-app` and its
   campaign (`sync_contacts`); a GHL workflow turns new tagged contacts into New Lead opportunities.
2. Scheduling: approving an email gives it a send time (`schedule`): the campaign's previous slot plus a fixed gap
   plus a random gap, inside the campaign's send window and daily cap; a follow-up never before "previous email
   sent + its days". The approval also writes Email Due At (a date) and, for an initial email, Initial Email Type
   to the contact, which lets a GHL workflow move the lead to Ready to Send.
3. Release: GHL date fields hold no time of day, so GHL cannot wait for the minute; this app does. When an email's
   time comes (`release_due`, run every minute by cron) its subject, body, Follow-up Count and Email Due At are
   written to the contact; the GHL sending workflow, triggered by Email Body becoming non-empty, sends it at once.
4. Sent (`record_sent`, the email-sent webhook): the email is marked sent; the contact's Email Subject, Body and Due
   At are emptied and Follow-up Count set to the next follow-up's number, or emptied too when the sequence is over
   (the workflow then moves the lead to Quota Exhausted). The next follow-up is written for review by the
   follow-up cron and released the same way once approved.
5. Stop (`stop_sequence`, the stop webhook): a reply or a manual stage move ends the prospect's sequence: pending
   emails are cancelled and the contact's fields emptied.
"""
import random
import re
from datetime import date, datetime, time as day_time, timedelta, timezone
from zoneinfo import ZoneInfo

from django.db import transaction
from django.db.models import Max
from django.utils.text import slugify

from pipeline.models import DiscoveryCampaign, Email, FollowUp, Prospect
from pipeline.services import generation, ghl

SCHEDULED_OR_LATER = ("scheduled", "sending", "sent", "opened", "replied")  # emails that hold a send slot
SENT_STATUSES = ("sent", "opened")
RELEASE_BATCH = 25  # emails released per cron call
SYNC_BATCH = 25  # contacts created per cron call
STUCK_AFTER = timedelta(minutes=30)  # handed to GHL this long ago without the email-sent webhook: shown as a problem


class OutreachError(RuntimeError):
    """The email cannot be scheduled or recorded; the message says why."""


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


def next_slot(campaign: DiscoveryCampaign, earliest: datetime, rng: random.Random | None = None) -> datetime:
    """The send time for a newly approved email of the campaign: at least `earliest`, at least the fixed plus a
    random gap after the campaign's latest slot, inside the send window, on a day under the daily cap. Call it
    inside a transaction that locked the campaign row, so two approvals never take the same slot."""
    rng = rng or random
    latest = (Email.objects.filter(business__discovery_campaign=campaign, status__in=SCHEDULED_OR_LATER)
              .aggregate(latest=Max("scheduled_at"))["latest"])
    candidate = earliest
    if latest is not None:
        gap = campaign.gap_fixed_seconds + rng.uniform(campaign.gap_random_min_seconds, campaign.gap_random_max_seconds)
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
        day_start, day_end = local_day_bounds(campaign, day)
        used = Email.objects.filter(business__discovery_campaign=campaign, status__in=SCHEDULED_OR_LATER,
                                    scheduled_at__gte=day_start, scheduled_at__lt=day_end).count()
        if used >= campaign.daily_cap:
            candidate = local_day_bounds(campaign, day + timedelta(days=1))[0]
            continue
        return candidate
    raise OutreachError("No send slot found within a year; check the campaign's daily cap and send window.")


def earliest_send(email: Email) -> datetime:
    """Now for an initial email; for a follow-up, the previous email's send time plus the step's days."""
    if email.sequence_step <= generation.SEQUENCE_STEP:
        return now()
    previous = Email.objects.filter(prospect_id=email.prospect_id, sequence_step=email.sequence_step - 1).first()
    if previous is None or previous.sent_at is None:
        raise OutreachError("The previous email of this follow-up has not been sent yet.")
    step = FollowUp.objects.filter(campaign_id=email.business.discovery_campaign_id,
                                   sequence=email.sequence_step - 1).first()
    return max(now(), previous.sent_at + timedelta(days=step.days_after_previous if step else 0))


def schedule(email: Email) -> datetime:
    """Give an approved email its send time (status scheduled). Returns the time."""
    with transaction.atomic():
        campaign = DiscoveryCampaign.objects.select_for_update().get(pk=email.business.discovery_campaign_id)
        slot = next_slot(campaign, earliest_send(email))
        if not Email.objects.filter(pk=email.pk, status="approved").update(status="scheduled", scheduled_at=slot,
                                                                            released_at=None, ghl_error=None):
            raise OutreachError("Only approved emails can be scheduled.")
    email.status, email.scheduled_at = "scheduled", slot
    return slot


# ---------------------------------------------------------------- GoHighLevel fields


def body_for_ghl(email: Email) -> str:
    """The email's HTML without the document wrapper, for the Email Body field (paragraphs and footer kept)."""
    match = re.search(r"<body[^>]*>(.*)</body>", email.body_html or "", re.S | re.I)
    return (match.group(1) if match else email.body_html or "").strip()


def due_date(email: Email) -> str | None:
    if email.scheduled_at is None:
        return None
    return email.scheduled_at.astimezone(ZoneInfo(email.business.discovery_campaign.send_timezone)).date().isoformat()


def initial_type(email: Email) -> str | None:
    return email.email_prompt.name if email.sequence_step == generation.SEQUENCE_STEP and email.email_prompt else None


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


def ensure_contact(prospect: Prospect, client: ghl.Client) -> str:
    """The prospect's GHL contact id, creating (or matching by email) the contact when it has none."""
    if prospect.ghl_contact_id:
        return prospect.ghl_contact_id
    contact_id, _ = client.upsert_contact(contact_payload(prospect))
    Prospect.objects.filter(pk=prospect.pk).update(ghl_contact_id=contact_id)
    prospect.ghl_contact_id = contact_id
    return contact_id


def announce_schedule(email: Email, client: ghl.Client | None = None) -> None:
    """After approval: write Email Due At (and Initial Email Type for an initial email) to the contact, so a GHL
    workflow can move the lead to Ready to Send. Errors are kept on the email and retried by the next sync."""
    try:
        client = client or ghl.Client()
        contact_id = ensure_contact(email.prospect, client)
        values = {"due": due_date(email)}
        if initial_type(email):
            values["type"] = initial_type(email)
        client.set_fields(contact_id, values)
        Email.objects.filter(pk=email.pk).update(ghl_error=None)
    except ghl.GhlError as error:
        Email.objects.filter(pk=email.pk).update(ghl_error=f"Ready-to-send update failed: {error}"[:2000])


def withdraw_schedule(email: Email, client: ghl.Client | None = None) -> None:
    """A scheduled email went back to review or was rejected: empty its due date in GHL (never the count, which
    would end the sequence)."""
    if not email.prospect.ghl_contact_id or not ghl.configured():
        return
    try:
        (client or ghl.Client()).set_fields(email.prospect.ghl_contact_id, {"due": None})
    except ghl.GhlError:
        pass  # only a date shown in GHL; nothing is sent without release


def release(email: Email, client: ghl.Client) -> None:
    """Write a due email into its contact's fields (status scheduled -> sending); GHL sends it right away."""
    prospect = email.prospect
    if not prospect.is_ready and not (email.sequence_step > generation.SEQUENCE_STEP
                                      and prospect.outreach_status == "contacted" and not prospect.do_not_contact):
        Email.objects.filter(pk=email.pk, status="scheduled").update(
            status="rejected", review_note="Not sent: the prospect can no longer be emailed.", scheduled_at=None)
        return
    if prospect.sequence_stopped_at is not None:
        Email.objects.filter(pk=email.pk, status="scheduled").update(
            status="rejected", review_note="Not sent: the sequence was stopped.", scheduled_at=None)
        return
    contact_id = ensure_contact(prospect, client)
    if not Email.objects.filter(pk=email.pk, status="scheduled").update(status="sending", released_at=now()):
        return  # released by another run meanwhile
    values = {"subject": email.subject, "body": body_for_ghl(email),
              "count": email.sequence_step - generation.SEQUENCE_STEP, "due": due_date(email)}
    if initial_type(email):
        values["type"] = initial_type(email)
    try:
        client.set_fields(contact_id, values)
    except ghl.GhlError as error:  # back to scheduled, tried again on the next run
        Email.objects.filter(pk=email.pk, status="sending").update(status="scheduled", released_at=None,
                                                                   ghl_error=str(error)[:2000])
        raise
    Email.objects.filter(pk=email.pk).update(ghl_error=None)


def release_due(limit: int = RELEASE_BATCH) -> dict:
    """Release every scheduled email whose time has come. Returns counts."""
    due = list(Email.objects.filter(status="scheduled", scheduled_at__lte=now())
               .select_related("prospect__contact", "prospect__business__discovery_campaign", "business__discovery_campaign",
                               "email_prompt")
               .order_by("scheduled_at", "id")[:limit])
    result = {"released": 0, "failed": 0, "cancelled": 0}
    if not due:
        return result
    client = ghl.Client()
    for email in due:
        try:
            release(email, client)
        except ghl.GhlError:
            result["failed"] += 1
            continue
        status = Email.objects.filter(pk=email.pk).values_list("status", flat=True).first()
        result["released" if status == "sending" else "cancelled"] += 1
    return result


# ---------------------------------------------------------------- contacts


def sync_contacts(limit: int = SYNC_BATCH) -> dict:
    """Create GHL contacts for ready prospects that have none yet, and retry failed ready-to-send updates."""
    result = {"created": 0, "matched": 0, "failed": 0, "retried": 0}
    pending = list(generation.ready_prospects().filter(ghl_contact_id__isnull=True)
                   .select_related("contact", "business__discovery_campaign").order_by("id")[:limit])
    retry = list(Email.objects.filter(status="scheduled", ghl_error__isnull=False)
                 .select_related("prospect__contact", "prospect__business__discovery_campaign",
                                 "business__discovery_campaign", "email_prompt")[:limit])
    if not pending and not retry:
        return result
    client = ghl.Client()
    for prospect in pending:
        try:
            contact_id, new = client.upsert_contact(contact_payload(prospect))
        except ghl.GhlError:
            result["failed"] += 1
            continue
        Prospect.objects.filter(pk=prospect.pk).update(ghl_contact_id=contact_id)
        result["created" if new else "matched"] += 1
    for email in retry:
        announce_schedule(email, client)
        result["retried"] += 1
    return result


# ---------------------------------------------------------------- webhooks


def prospect_for_contact(contact_id: str) -> Prospect:
    prospects = list(Prospect.objects.filter(ghl_contact_id=contact_id).select_related("business__discovery_campaign"))
    if not prospects:
        raise OutreachError(f"No prospect has GoHighLevel contact {contact_id}.")
    if len(prospects) > 1:  # the same address under two contacts: the one with an email waiting wins
        waiting = [p for p in prospects if p.emails.filter(status="sending").exists()]
        prospects = waiting or prospects
    return prospects[0]


def sequence_state(prospect: Prospect, sent_step: int) -> dict:
    """The contact's fields after email `sent_step` went out: the next follow-up's number, or all empty (done)."""
    has_next = (prospect.sequence_stopped_at is None and not prospect.do_not_contact
                and FollowUp.objects.filter(campaign_id=prospect.business.discovery_campaign_id, sequence=sent_step,
                                            email_prompt__isnull=False).exists())  # a step without a prompt ends it
    return {"subject": None, "body": None, "due": None, "count": sent_step if has_next else None}


def record_sent(contact_id: str, followup_count=None, client: ghl.Client | None = None) -> dict:
    """The email-sent webhook: mark the email GHL just sent, then set the contact's fields for what comes next.
    Safe to call twice for the same email (GHL may retry a webhook)."""
    prospect = prospect_for_contact(contact_id)
    emails = prospect.emails.all()
    if followup_count not in (None, ""):
        try:
            step = int(float(followup_count)) + generation.SEQUENCE_STEP
        except (TypeError, ValueError) as error:
            raise OutreachError(f"Follow-up Count must be a number, got {followup_count!r}.") from error
        email = emails.filter(sequence_step=step).first()
    else:
        email = emails.filter(status="sending").order_by("sequence_step").first() \
            or emails.filter(status__in=SENT_STATUSES).order_by("-sequence_step").first()
    if email is None:
        raise OutreachError("This contact has no email waiting to be sent.")
    if email.status == "sending":
        sent_at = now()
        if Email.objects.filter(pk=email.pk, status="sending").update(status="sent", sent_at=sent_at):
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
    return {"email": email.pk, "outcome": outcome,
            "next": f"follow-up {state['count']}" if state["count"] is not None else "sequence finished"}


def stop_prospect(prospect: Prospect, reason: str = "stopped", client: ghl.Client | None = None,
                  raise_errors: bool = False) -> int:
    """No more emails to this prospect: its sequence is stopped, emails not sent yet are cancelled and its GHL
    sending fields emptied. Returns how many emails were cancelled."""
    reason = (reason or "stopped").strip()[:64]
    Prospect.objects.filter(pk=prospect.pk, sequence_stopped_at__isnull=True).update(
        sequence_stopped_at=now(), sequence_stop_reason=reason)
    cancelled = Email.objects.filter(prospect=prospect, status__in=("in_review", "approved", "scheduled")).update(
        status="rejected", review_note=f"Sequence stopped ({reason}).", scheduled_at=None)
    if prospect.ghl_contact_id and ghl.configured():
        try:
            (client or ghl.Client()).set_fields(prospect.ghl_contact_id,
                                                {"subject": None, "body": None, "due": None, "count": None})
        except ghl.GhlError:
            if raise_errors:
                raise
    return cancelled


def stop_sequence(contact_id: str, reason: str = "stopped", client: ghl.Client | None = None) -> dict:
    """The stop webhook (a reply or a manual stage move in GHL): no more emails to this prospect. A reply also
    marks the latest sent email replied."""
    prospect = prospect_for_contact(contact_id)
    reason = (reason or "stopped").strip()[:64]
    if reason.lower().startswith("repl"):
        latest = prospect.emails.filter(status__in=SENT_STATUSES).order_by("-sequence_step").first()
        if latest is not None:
            Email.objects.filter(pk=latest.pk, status__in=SENT_STATUSES).update(status="replied")
    cancelled = stop_prospect(prospect, reason, client or ghl.Client(), raise_errors=True)
    return {"prospect": prospect.pk, "reason": reason, "cancelled": cancelled}


# ---------------------------------------------------------------- health


def stuck_emails():
    """Emails handed to GHL long ago that it never reported as sent: the sending workflow is missing or failing."""
    return Email.objects.filter(status="sending", released_at__lt=now() - STUCK_AFTER)
