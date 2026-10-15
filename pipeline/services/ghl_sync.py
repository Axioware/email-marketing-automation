"""Create GoHighLevel contacts for ready prospects that have none yet, and hand approved emails whose hand-over failed
or was interrupted to GoHighLevel again (see outreach.py). The full pipeline runs it as its last step; run it by
hand (or from the dashboard) after verifying emails or approving prospects outside the pipeline."""
from django.core.management.base import CommandError

from pipeline.management.base import out_of_time, time_limit_note
from pipeline.services import ghl, outreach

RESERVE_SECONDS = 15  # with a deadline: a GHL call takes a second or two


def add_arguments(parser) -> None:
    parser.add_argument("--limit", type=int, default=200, help="Most contacts and emails handled in this run (default: 200).")
    parser.add_argument("--contacts-only", action="store_true", help="Only create contacts; hand over no emails.")


def run(options: dict) -> int:
    if options["limit"] < 1:
        raise CommandError("--limit must be a positive integer")
    if not ghl.configured():
        print("GoHighLevel is not set up (GHL_API_KEY and GHL_LOCATION_ID in .env); nothing to do.")
        return 0
    try:
        deadline = options.get("deadline")
        contacts = outreach.sync_contacts(options["limit"], None if deadline is None else deadline - RESERVE_SECONDS)
    except ghl.GhlError as error:
        print(f"GoHighLevel error: {error}")
        return 1
    print(f"Contacts: {contacts['created']} created, {contacts['matched']} matched to existing GHL contacts, "
          f"{contacts['failed']} failed (see the prospect; one email address gets one sequence).")
    if contacts.get("left"):
        time_limit_note(contacts["left"], "contact(s)")
    handed = failed = 0
    if not options["contacts_only"]:
        emails = list(outreach.not_handed_over()[: options["limit"]])
        for position, email in enumerate(emails):
            if out_of_time(options, RESERVE_SECONDS):
                time_limit_note(len(emails) - position, "email(s) to hand over")
                break
            try:
                outreach.hand_over(email)
                handed += 1
            except (outreach.OutreachError, ghl.GhlError) as error:
                failed += 1
                print(f"  Email {email.pk} not handed over: {error}")
        print(f"Approved emails handed to GoHighLevel: {handed}, failed: {failed}.")
    stuck = outreach.stuck_emails().count()
    if stuck:
        print(f"Warning: {stuck} email(s) are past their send time by over a day and GoHighLevel never reported them "
              "sent; check the sending workflow.")
    return 1 if contacts["failed"] or failed else 0
