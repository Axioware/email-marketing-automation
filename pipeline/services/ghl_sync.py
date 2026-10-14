"""Keep GoHighLevel in step with the app: create contacts for ready prospects, retry failed updates, and release
emails whose send time has come (see outreach.py). Run it every minute (cron) when this app runs on a server; on
Vercel the cron endpoints in pipeline/api/views.py call the same functions."""
from django.core.management.base import CommandError

from pipeline.services import ghl, outreach


def add_arguments(parser) -> None:
    parser.add_argument("--limit", type=int, default=200, help="Most contacts and emails handled in this run (default: 200).")
    parser.add_argument("--contacts-only", action="store_true", help="Only create contacts; release no emails.")


def run(options: dict) -> int:
    if options["limit"] < 1:
        raise CommandError("--limit must be a positive integer")
    if not ghl.configured():
        print("GoHighLevel is not set up (GHL_API_KEY and GHL_LOCATION_ID in .env); nothing to do.")
        return 0
    try:
        contacts = outreach.sync_contacts(options["limit"])
        print(f"Contacts: {contacts['created']} created, {contacts['matched']} matched to existing GHL contacts, "
              f"{contacts['failed']} failed; {contacts['retried']} ready-to-send update(s) retried.")
        released = {"released": 0, "failed": 0, "cancelled": 0}
        if not options["contacts_only"]:
            released = outreach.release_due(options["limit"])
            print(f"Emails: {released['released']} handed to GoHighLevel for sending, {released['cancelled']} "
                  f"cancelled (prospect no longer emailable), {released['failed']} failed (retried next run).")
    except ghl.GhlError as error:
        print(f"GoHighLevel error: {error}")
        return 1
    stuck = outreach.stuck_emails().count()
    if stuck:
        print(f"Warning: {stuck} email(s) were handed to GoHighLevel over 30 minutes ago and never reported sent; "
              "check the sending workflow.")
    return 1 if contacts["failed"] or released["failed"] else 0
