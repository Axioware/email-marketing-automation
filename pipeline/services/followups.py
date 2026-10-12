"""Follow-ups: further emails to a prospect, a campaign-defined number of days after the previous one.

A campaign's `follow_ups` rows define the schedule (follow-up 1, 2, ... and the days after the previous email). A
follow-up is due when the campaign has the next step, the prospect's latest email was sent (or opened) long enough
ago, that follow-up does not exist yet, nobody at the business has replied and the prospect is not do-not-contact.

Each due follow-up is written with the prompt for its step and the prospect's A/B variation (see
generation.resolve_prompt: a prompt can be meant for prospects who opened, or did not open, the previous email), with
the previous emails as context, and saved In review like every email. Nothing is sent here.
"""
from datetime import datetime, timedelta, timezone

from django.core.management.base import CommandError
from django.db import DatabaseError

from pipeline.models import Email, EmailPrompt, FollowUp, Prospect
from pipeline.services import generation

SENT_STATUSES = ("sent", "opened")  # a follow-up only follows an email that went out and got no reply


def now() -> datetime:
    return datetime.now(timezone.utc)


def find_due(options: dict, at: datetime | None = None) -> list[dict]:
    """The follow-ups due now. Each item: prospect_id, step (emails.sequence_step to write), sequence (follow-up
    number), previous (the latest sent Email) and follow_up (the FollowUp step)."""
    at = at or now()
    prospects = (
        Prospect.objects.filter(do_not_contact=False, business__replied_at__isnull=True,
                                emails__status__in=SENT_STATUSES)
        .select_related("business")
        .order_by("id")
        .distinct()
    )
    if options.get("prospect_id"):
        prospects = prospects.filter(id__in=options["prospect_id"])
    if options.get("business_id"):
        prospects = prospects.filter(business_id__in=options["business_id"])
    if options.get("campaign_id") is not None:
        prospects = prospects.filter(business__discovery_campaign_id=options["campaign_id"])
    schedules: dict[int, dict[int, FollowUp]] = {}
    due = []
    for prospect in prospects:
        campaign_id = prospect.business.discovery_campaign_id
        if campaign_id not in schedules:
            schedules[campaign_id] = {f.sequence: f for f in FollowUp.objects.filter(campaign_id=campaign_id)}
        latest = prospect.emails.order_by("-sequence_step").first()
        if latest is None or latest.status not in SENT_STATUSES or latest.sent_at is None:
            continue  # the latest email is still in review/approved/failed/replied, or the next one exists already
        sequence = latest.sequence_step  # step s is follow-up s - 1, so the next follow-up is number s
        follow_up = schedules[campaign_id].get(sequence)
        if follow_up is None:
            continue  # the campaign's sequence is finished for this prospect
        if latest.sent_at + timedelta(days=follow_up.days_after_previous) > at:
            continue
        due.append({"prospect_id": prospect.pk, "step": latest.sequence_step + 1, "sequence": sequence,
                    "previous": latest, "follow_up": follow_up})
    if options.get("limit") is not None:
        due = due[: options["limit"]]
    return due


def thread_context(prospect_id: int, before_step: int) -> list[dict]:
    """The emails already sent to this prospect, oldest first, as the model sees them."""
    emails = Email.objects.filter(prospect_id=prospect_id, sequence_step__lt=before_step, sent_at__isnull=False)
    return [
        {"step": email.sequence_step, "subject": email.subject, "body": generation.editable_body(email.body_text),
         "sent_on": email.sent_at.date().isoformat(),
         **({"opened": email.open_count > 0} if generation.OPEN_TRACKING else {})}  # unknown while tracking is off
        for email in emails.order_by("sequence_step")
    ]


def write_followup(item: dict, llm: tuple, chosen: EmailPrompt | None = None, dry_run: bool = False,
                   cache: dict | None = None) -> tuple[str, str]:
    """Write one follow-up. Returns (outcome, detail): ("saved", subject), ("dry_run", body), ("skipped", reason)."""
    client, model, provider = llm
    rows = generation.prospect_rows(Prospect.objects.filter(pk=item["prospect_id"]))
    if not rows:
        return "skipped", "the prospect no longer exists"
    row = rows[0]
    generation.assign_variation(row, save=not dry_run)
    # Without open tracking nobody's opens are known, so "opened"/"not opened" prompts are not chosen.
    previous_opened = item["previous"].open_count > 0 if generation.OPEN_TRACKING else None
    resolved = generation.resolve_prompt(row, chosen, cache, sequence=item["sequence"], previous_opened=previous_opened)
    if resolved is None:
        return "skipped", generation.missing_prompt_reason(row, item["sequence"])
    system_prompt, email_prompt_id = resolved
    context = {
        **generation.prospect_context(row),
        "follow_up": {"number": item["sequence"], "previous_email_opened": previous_opened},
        "previous_emails": thread_context(item["prospect_id"], item["step"]),
    }
    draft = generation.generate_draft(client, model, context, system_prompt)
    if dry_run:
        return "dry_run", f"{draft.subject}\n{draft.body}"
    existing = Email.objects.filter(prospect_id=item["prospect_id"], sequence_step=item["step"]).first()
    token = existing.tracking_token if existing else generation.new_tracking_token()
    body_html = generation.render_html(draft.body, generation.footer_image_url(token))
    if not generation.save_draft(row, draft, body_html, token, provider, model, now(), email_prompt_id,
                                 sequence_step=item["step"]):
        return "skipped", "that follow-up was already written and is no longer in review"
    return "saved", draft.subject


def add_arguments(parser) -> None:
    parser.add_argument("--campaign-id", type=int, help="Only this campaign's prospects.")
    parser.add_argument("--business-id", type=int, action="append", help="Only this business (repeatable).")
    parser.add_argument("--prospect-id", type=int, action="append", help="Only this prospect (repeatable).")
    parser.add_argument("--limit", type=int, help="Write at most this many follow-ups.")
    parser.add_argument("--email-prompt-id", type=int,
                        help="Write every follow-up with this email prompt (default: chosen per step and variation).")
    parser.add_argument("--dry-run", action="store_true", help="Show the follow-ups without saving them.")


def run(options: dict) -> int:
    if options["limit"] is not None and options["limit"] < 1:
        raise CommandError("--limit must be a positive integer")
    chosen = None
    if options.get("email_prompt_id") is not None:
        chosen = EmailPrompt.objects.select_related("campaign_prompt").filter(pk=options["email_prompt_id"]).first()
        if chosen is None:
            raise CommandError(f"Email prompt {options['email_prompt_id']} does not exist.")
    try:
        due = find_due(options)
        if not due:
            print("No follow-ups are due.")
            return 0
        if generation.OPEN_TRACKING and not generation.default_tracking_base_url():
            raise CommandError("Set EMAIL_TRACKING_BASE_URL in .env")
        llm = generation.make_llm_client()
        if llm is None:
            raise CommandError("Set GROQ_API_KEY (or GROK_API_KEY) or OPENAI_API_KEY in the environment or .env file")
        print(f"Using {llm[2]} model {llm[1]}. Writing {len(due)} due follow-up(s).")
        saved = skipped = failed = 0
        cache: dict = {}
        for item in due:
            label = f"Prospect {item['prospect_id']} <{item['previous'].recipient}>, follow-up {item['sequence']}"
            try:
                outcome, detail = write_followup(item, llm, chosen, options["dry_run"], cache)
            except Exception as error:  # one bad follow-up must not stop the others
                failed += 1
                print(f"  {label} failed ({type(error).__name__}): {error}")
                continue
            if outcome == "skipped":
                skipped += 1
                print(f"  {label} skipped: {detail}")
            elif outcome == "dry_run":
                saved += 1
                print(f"  {label}:\n    " + detail.replace("\n", "\n    "))
            else:
                saved += 1
                print(f"  {label}: {detail}")
        verb = "written (dry run, not saved)" if options["dry_run"] else "saved for review"
        print(f"Done: {saved} follow-up(s) {verb}, {skipped} skipped, {failed} failed.")
        return 1 if failed or skipped else 0
    except DatabaseError as error:
        print(f"Database operation failed ({type(error).__name__}): {str(error).splitlines()[0]}")
        return 1


def regenerate(email: Email, chosen: EmailPrompt | None = None) -> tuple[str, str]:
    """Rewrite a follow-up that is in review or rejected (used by the review page). Raises ValueError if it cannot."""
    previous = Email.objects.filter(prospect_id=email.prospect_id, sequence_step=email.sequence_step - 1).first()
    if previous is None or previous.sent_at is None:
        raise ValueError("The previous email of this follow-up was not sent.")
    item = {"prospect_id": email.prospect_id, "step": email.sequence_step, "sequence": email.sequence_step - 1,
            "previous": previous, "follow_up": None}
    llm = generation.make_llm_client()
    if llm is None:
        raise ValueError("Set an LLM key (GROQ_API_KEY or OPENAI_API_KEY).")
    outcome, detail = write_followup(item, llm, chosen or email.email_prompt)
    if outcome != "saved":
        raise ValueError(f"Not regenerated: {detail}")
    return llm[2], llm[1]
