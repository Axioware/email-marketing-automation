"""Follow-ups: further emails to a prospect after the previous one was sent.

The prospect's email sequence (its A/B group) defines its follow-ups: follow-up 1, 2, ..., each with its template and the
days to wait after the previous email. A follow-up is filled in from its template (In review) as soon as the previous email is
sent ({{previous_subject}} is that email's subject), so a person has those days to review it; approving it schedules it no earlier than "previous email sent + days" (outreach.py).
Nothing is written once the prospect replied, was stopped (sequence_stopped_at) or is do-not-contact; each prospect
has its own sequence.
"""
from datetime import datetime, timezone

from django.core.management.base import CommandError
from django.db import DatabaseError

from pipeline.management.base import PARTIAL, out_of_time, time_limit_note
from pipeline.models import Email, EmailPrompt, FollowUp, Prospect
from pipeline.services import generation, templates

SENT_STATUSES = ("sent", "opened")  # a follow-up only follows an email that went out and got no reply


def now() -> datetime:
    return datetime.now(timezone.utc)


def find_due(options: dict) -> list[dict]:
    """The follow-ups to write now: the prospect's latest email was sent, its sequence has a next follow-up step, and
    that follow-up does not exist yet. Each item: prospect_id, step (emails.sequence_step to write), sequence
    (follow-up number), previous (the latest sent Email) and follow_up (the FollowUp step)."""
    prospects = (
        Prospect.objects.filter(do_not_contact=False, sequence_stopped_at__isnull=True,
                                emails__status__in=SENT_STATUSES)
        .exclude(emails__status="replied")
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
        sequence_id = prospect.email_sequence_id
        if sequence_id is None:
            continue  # no sequence: no follow-ups
        if sequence_id not in schedules:
            schedules[sequence_id] = {f.sequence: f for f in FollowUp.objects.filter(email_sequence_id=sequence_id)}
        latest = prospect.emails.order_by("-sequence_step").first()
        if latest is None or latest.status not in SENT_STATUSES or latest.sent_at is None:
            continue  # the latest email is not sent yet, failed or replied, or the next one exists already
        sequence = latest.sequence_step  # step s is follow-up s - 1, so the next follow-up is number s
        follow_up = schedules[sequence_id].get(sequence)
        if follow_up is None:
            continue  # its sequence is finished
        if follow_up.email_prompt_id is None and not options.get("any_prompt"):
            continue  # nothing can write it (the sequence ends there; see outreach.sequence_state)
        due.append({"prospect_id": prospect.pk, "step": latest.sequence_step + 1, "sequence": sequence,
                    "previous": latest, "follow_up": follow_up})
    if options.get("limit") is not None:
        due = due[: options["limit"]]
    return due


def write_followup(item: dict, chosen: EmailPrompt | None = None, dry_run: bool = False,
                   cache: dict | None = None) -> tuple[str, str]:
    """Fill in one follow-up. Returns (outcome, detail): ("saved", subject), ("dry_run", body), ("skipped", reason),
    or ("held", which variable has no value)."""
    rows = generation.prospect_rows(Prospect.objects.filter(pk=item["prospect_id"]))
    if not rows:
        return "skipped", "the prospect no longer exists"
    row = {**rows[0], "previous_subject": item["previous"].subject}
    template = generation.resolve_template(row, chosen, cache, sequence=item["sequence"])
    if template is None:
        return "skipped", generation.missing_prompt_reason(row, item["sequence"])
    try:
        draft = generation.fill_template(template, row)
    except templates.TemplateIncomplete as error:
        return "held", f"{template}: {error}"
    if dry_run:
        return "dry_run", f"{draft.subject}\n{draft.body}"
    existing = Email.objects.filter(prospect_id=item["prospect_id"], sequence_step=item["step"]).first()
    token = existing.tracking_token if existing else generation.new_tracking_token()
    body_html = generation.render_html(draft.body, generation.footer_image_url(token))
    if not generation.save_draft(row, draft, body_html, token, "template", template.name, now(), template.pk,
                                 sequence_step=item["step"]):
        return "skipped", "that follow-up was already written and is no longer in review"
    return "saved", draft.subject


def add_arguments(parser) -> None:
    parser.add_argument("--campaign-id", type=int, help="Only this campaign's prospects.")
    parser.add_argument("--business-id", type=int, action="append", help="Only this business (repeatable).")
    parser.add_argument("--prospect-id", type=int, action="append", help="Only this prospect (repeatable).")
    parser.add_argument("--limit", type=int, help="Write at most this many follow-ups.")
    parser.add_argument("--email-prompt-id", "--template-id", type=int, dest="email_prompt_id",
                        help="Use this template for every follow-up (default: the template of each step).")
    parser.add_argument("--dry-run", action="store_true", help="Show the follow-ups without saving them.")


def run(options: dict) -> int:
    if options["limit"] is not None and options["limit"] < 1:
        raise CommandError("--limit must be a positive integer")
    chosen = None
    if options.get("email_prompt_id") is not None:
        chosen = EmailPrompt.objects.filter(pk=options["email_prompt_id"]).first()
        if chosen is None:
            raise CommandError(f"Template {options['email_prompt_id']} does not exist.")
    try:
        due = find_due({**options, "any_prompt": chosen is not None})
        if not due:
            print("No follow-ups are due.")
            return 0
        if generation.OPEN_TRACKING and not generation.default_tracking_base_url():
            raise CommandError("Set EMAIL_TRACKING_BASE_URL in .env")
        print(f"Writing {len(due)} due follow-up(s) from templates.")
        saved = skipped = held = 0
        cache: dict = {}
        for position, item in enumerate(due):
            if out_of_time(options, generation.EMAIL_RESERVE_SECONDS):
                time_limit_note(len(due) - position, "follow-up(s)")
                break
            label = f"Prospect {item['prospect_id']} <{item['previous'].recipient}>, follow-up {item['sequence']}"
            outcome, detail = write_followup(item, chosen, options["dry_run"], cache)
            if outcome == "skipped":
                skipped += 1
                print(f"  {label} skipped: {detail}")
            elif outcome == "held":
                held += 1
                print(f"  {label} held back ({detail}).")
            elif outcome == "dry_run":
                saved += 1
                print(f"  {label}:\n    " + detail.replace("\n", "\n    "))
            else:
                saved += 1
                print(f"  {label}: {detail}")
        verb = "written (dry run, not saved)" if options["dry_run"] else "saved for review"
        print(f"Done: {saved} follow-up(s) {verb}, {skipped} skipped, {held} held back.")
        return PARTIAL if held or skipped else 0
    except DatabaseError as error:
        print(f"Database operation failed ({type(error).__name__}): {str(error).splitlines()[0]}")
        return 1


def regenerate(email: Email, chosen: EmailPrompt | None = None) -> str:
    """Fill a follow-up that is in review or rejected again from its template (or a chosen one). Returns the
    template's name; ValueError if it cannot."""
    previous = Email.objects.filter(prospect_id=email.prospect_id, sequence_step=email.sequence_step - 1).first()
    if previous is None or previous.sent_at is None:
        raise ValueError("The previous email of this follow-up was not sent.")
    item = {"prospect_id": email.prospect_id, "step": email.sequence_step, "sequence": email.sequence_step - 1,
            "previous": previous, "follow_up": None}
    outcome, detail = write_followup(item, chosen or email.email_prompt)
    if outcome != "saved":
        raise ValueError(f"Not filled in: {detail}")
    return (chosen or email.email_prompt).name if (chosen or email.email_prompt) else "its step's template"
