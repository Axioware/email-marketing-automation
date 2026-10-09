"""The complete pipeline for one campaign: fetch -> research -> decision makers -> verify -> write emails.

Each step runs exactly as its own command would, on the campaign's businesses, and only processes what still needs
it, so rerunning the pipeline continues where it stopped. It never sends: emails end up In review for a person to
approve. A step that finishes with some errors (e.g. one website failed) does not stop the pipeline; a step that
cannot run at all (missing LLM key, Reacher not available, Google CAPTCHA) stops it, keeping everything saved so far.
"""
from django.core.management import call_command
from django.core.management.base import CommandError

from pipeline.models import Business, DiscoveryCampaign, EmailPrompt

STEPS = {
    "fetch": "Find businesses on Google Maps",
    "research": "Research and score websites",
    "stakeholders": "Find decision makers and candidate emails",
    "verify": "Verify emails",
    "generate": "Write emails for review",
}
PARTIAL_FAILURE = "Finished with errors"  # how a step reports that only some items failed


def add_arguments(parser) -> None:
    parser.add_argument("--campaign-id", type=int,
                        help="Run for this campaign's businesses (needed to fetch new ones). Without it, every business "
                             "is processed and nothing is fetched.")
    parser.add_argument("--fetch-limit", type=int, default=20,
                        help="New businesses to fetch from Google Maps (default: 20; 0 skips fetching).")
    parser.add_argument("--skip", action="append", choices=list(STEPS), help="Skip this step (repeatable).")
    parser.add_argument("--min-score", type=int, default=50,
                        help="Qualification score needed to look for decision makers (default: 50).")
    parser.add_argument("--email-prompt-id", type=int,
                        help="Write emails with this email prompt (default: the campaign's default).")
    parser.add_argument("--headed", action="store_true", help="Show the browsers (useful for Google consent pages).")


def business_arguments(campaign_id: int | None) -> list[str]:
    if campaign_id is None:
        return []
    ids = Business.objects.filter(discovery_campaign_id=campaign_id).order_by("id").values_list("id", flat=True)
    return [item for pk in ids for item in ("--business-id", str(pk))]


def run(options: dict) -> int:
    campaign_id = options["campaign_id"]
    skip = set(options["skip"] or [])
    if options["fetch_limit"] < 0:
        raise CommandError("--fetch-limit cannot be negative")
    if not 0 <= options["min_score"] <= 100:
        raise CommandError("--min-score must be between 0 and 100")
    if campaign_id is not None and not DiscoveryCampaign.objects.filter(pk=campaign_id).exists():
        raise CommandError(f"Campaign {campaign_id} does not exist.")
    if options["email_prompt_id"] is not None and not EmailPrompt.objects.filter(pk=options["email_prompt_id"]).exists():
        raise CommandError(f"Email prompt {options['email_prompt_id']} does not exist.")
    if campaign_id is None or options["fetch_limit"] == 0:
        skip.add("fetch")
    headed = ["--headed"] if options["headed"] else []

    def arguments(step: str) -> list[str]:
        businesses = business_arguments(campaign_id)  # re-read: fetching adds businesses
        if step == "fetch":
            return ["--campaign-id", str(campaign_id), "--limit", str(options["fetch_limit"]), *headed]
        if step == "research":
            return [*businesses, *headed]
        if step == "stakeholders":
            return ["--min-score", str(options["min_score"]), *businesses, *headed]
        if step == "verify":
            return businesses
        prompt = ["--email-prompt-id", str(options["email_prompt_id"])] if options["email_prompt_id"] else []
        return [*businesses, *prompt]

    commands = {"fetch": "fetch_businesses", "research": "research_websites", "stakeholders": "find_stakeholders",
                "verify": "verify_emails", "generate": "generate_emails"}
    planned = [step for step in STEPS if step not in skip]
    scope = f"campaign {campaign_id}" if campaign_id is not None else "all businesses"
    print(f"Full pipeline for {scope}: " + " -> ".join(STEPS[step] for step in planned) + ". Nothing is sent.")
    partial = []
    for number, step in enumerate(planned, start=1):
        args = arguments(step)
        if step != "fetch" and campaign_id is not None and "--business-id" not in args:
            # Without business ids a step would process every business, not just this campaign's.
            print("\nThe campaign has no businesses yet; nothing more to do.")
            return 1 if partial else 0
        print(f"\n=== Step {number}/{len(planned)}: {STEPS[step]} ===", flush=True)
        try:
            call_command(commands[step], *args)
        except CommandError as error:
            if PARTIAL_FAILURE in str(error):
                partial.append(STEPS[step])
                print("(Some items in this step failed; continuing with the next step.)", flush=True)
            else:
                print(f"\nStopped at step {number} ({STEPS[step]}): {error}")
                print("Everything done so far is saved. Fix the problem and run the pipeline again; finished work is "
                      "skipped.")
                return 1
    print("\nPipeline finished." + (f" Steps with some failures: {', '.join(partial)}." if partial else ""))
    print("Next: review the new emails (Emails > In review), approve them, then send.")
    return 1 if partial else 0
