"""Score emails: an AI check of each verified address before it may be emailed (its own step; Settings page).

1. Excluded patterns (Settings, e.g. info@*, business@*): a matching address is disqualified (Rejected) and never
   emailed. Verification already skips them (no Reacher time, no Hunter credit); this step applies the list again, so
   a pattern added later also catches addresses verified before.
2. Every other verified address (deliverable, risky or unknown, not yet scored) goes to the AI with the contact and
   the business, using the scoring prompt from Settings. It answers a score (0-100) and a short reasoning, stored on
   the prospect.
3. Gate: a deliverable address scoring at least the minimum (Settings) becomes Ready to email; a lower score, or a
   risky/unknown address, waits in Needs review with the reasoning shown.

With "Require an AI score" on (default), verification leaves deliverable addresses Pending until this step scores
them. Addresses a person approved, and ones already contacted or rejected, are left alone.
"""
import json
from datetime import datetime, timezone
from fnmatch import fnmatchcase

from django.core.management.base import CommandError
from django.db import DatabaseError
from openai import APIError

from pipeline.management.base import OutOfTime, out_of_time, time_limit_note
from pipeline.models import Prospect, SendingSettings
from pipeline.services.llm import complete, make_llm_client, rejected_output, tune

MAX_COMPLETION_TOKENS = 400
LLM_TIMEOUT = 60
RESERVE_SECONDS = 30  # with a deadline: an AI answer takes a few seconds, sometimes much more
SCORABLE_EMAIL_STATUSES = ("deliverable", "risky", "unknown")
SCORABLE_OUTREACH = ("pending", "needs_review")  # waiting for a decision; ready/contacted/rejected are left alone
ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "integer", "description": "0-100: how likely this address reaches the decision maker."},
        "reasoning": {"type": "string", "description": "One or two sentences a person can check."},
    },
    "required": ["score", "reasoning"],
    "additionalProperties": False,
}


def patterns(text: str | None = None) -> list[str]:
    """The excluded patterns from Settings (one per line, lower case)."""
    if text is None:
        text = SendingSettings.current().excluded_email_patterns
    return [line.strip().casefold() for line in (text or "").splitlines() if line.strip()]


def matching_pattern(address: str, excluded: list[str]) -> str | None:
    """The first excluded pattern the address matches (case-insensitive), or None."""
    value = (address or "").strip().casefold()
    return next((pattern for pattern in excluded if fnmatchcase(value, pattern)), None)


def exclusion_note(pattern: str) -> str:
    return f"Excluded: matches the pattern {pattern} (Settings), so it is never emailed."


def prospect_context(prospect: Prospect) -> dict:
    """What the AI sees about one address."""
    contact, business = prospect.contact, prospect.business
    profile = getattr(business, "website_profile", None)
    siblings = (Prospect.objects.filter(business=business).exclude(pk=prospect.pk).exclude(verdict="excluded")
                .values_list("email", "email_status")[:12])
    return {
        "address": prospect.email,
        "verification": {"status": prospect.email_status, "verdict": prospect.verdict,
                         "note": prospect.verification_note, "provider": prospect.email_verification_provider},
        "contact": {"name": contact.name, "job_title": contact.job_title, "role_type": contact.role_type,
                    "found_by": contact.email_source, "is_primary_contact": contact.is_primary,
                    "confidence": float(contact.confidence) if contact.confidence is not None else None,
                    "linkedin": contact.linkedin_url, "why_chosen": contact.discovery_reasoning},
        "business": {"name": business.name, "category": business.category, "city": business.city,
                     "country": business.country, "website": business.website_url,
                     "google_rating": float(business.google_rating) if business.google_rating is not None else None,
                     "google_review_count": business.google_review_count,
                     "qualification_score": profile.qualification_score if profile else None,
                     "qualification_reasons": (profile.qualification_reasons if profile else None) or []},
        "other_addresses_at_this_business": [{"address": email, "status": status} for email, status in siblings],
    }


def score_address(client, model: str, prompt: str, prospect: Prospect, deadline: float | None = None) -> dict:
    """The AI's {"score", "reasoning"} for one address."""
    response = complete(
        client, deadline, timeout=LLM_TIMEOUT, model=model,
        **tune(model, temperature=0, max_completion_tokens=MAX_COMPLETION_TOKENS),
        response_format={"type": "json_schema",
                         "json_schema": {"name": "email_score", "strict": True, "schema": ANSWER_SCHEMA}},
        messages=[{"role": "system", "content": prompt},
                  {"role": "user", "content": json.dumps(prospect_context(prospect), ensure_ascii=False,
                                                         default=str)}],
    )
    message = response.choices[0].message
    if getattr(message, "refusal", None):
        raise ValueError(f"the AI refused: {message.refusal}")
    answer = json.loads(message.content or "{}")
    score = int(answer["score"])
    return {"score": max(0, min(score, 100)), "reasoning": " ".join(str(answer["reasoning"]).split())[:2000]}


def decide(prospect: Prospect, score: int, minimum: int) -> str:
    """The outreach status a score gives."""
    return "ready" if prospect.email_status == "deliverable" and score >= minimum else "needs_review"


def load_queue(options: dict):
    queryset = (Prospect.objects.filter(do_not_contact=False, approved_by_person=False,
                                        outreach_status__in=SCORABLE_OUTREACH,
                                        email_status__in=SCORABLE_EMAIL_STATUSES)
                .exclude(verdict="excluded")
                .select_related("contact", "business__website_profile").order_by("business_id", "id"))
    if not options["rescore"]:
        queryset = queryset.filter(email_scored_at__isnull=True)
    if options.get("business_id"):
        queryset = queryset.filter(business_id__in=options["business_id"])
    if options.get("prospect_id"):
        queryset = queryset.filter(id__in=options["prospect_id"])
    if options["limit"] is not None:
        queryset = queryset[: options["limit"]]
    return list(queryset)


def apply_exclusions(options: dict, excluded: list[str]) -> int:
    """Disqualify addresses (verified before the pattern was added) that match an excluded pattern."""
    candidates = (Prospect.objects.filter(outreach_status__in=("pending", "needs_review", "ready"),
                                          approved_by_person=False)
                  .exclude(verdict="excluded"))
    if options.get("business_id"):
        candidates = candidates.filter(business_id__in=options["business_id"])
    if options.get("prospect_id"):
        candidates = candidates.filter(id__in=options["prospect_id"])
    count = 0
    for prospect in candidates.only("id", "email"):
        pattern = matching_pattern(prospect.email, excluded)
        if not pattern:
            continue
        count += 1
        print(f"  {prospect.email}: {exclusion_note(pattern)}")
        if not options["dry_run"]:
            Prospect.objects.filter(pk=prospect.pk).update(
                outreach_status="rejected", email_score=None, email_score_reason=exclusion_note(pattern),
                email_scored_at=datetime.now(timezone.utc))
    return count


def add_arguments(parser) -> None:
    parser.add_argument("--business-id", type=int, action="append", help="Only this business (repeatable).")
    parser.add_argument("--prospect-id", type=int, action="append", help="Only this prospect (repeatable).")
    parser.add_argument("--limit", type=int, help="Score at most this many addresses.")
    parser.add_argument("--rescore", action="store_true",
                        help="Score again addresses that already have a score (e.g. after changing the prompt).")
    parser.add_argument("--dry-run", action="store_true", help="Print the scores without saving anything.")


def run(options: dict) -> int:
    if options["limit"] is not None and options["limit"] < 1:
        raise CommandError("--limit must be a positive integer")
    settings_row = SendingSettings.current()
    excluded = patterns(settings_row.excluded_email_patterns)
    try:
        print(f"Excluded patterns: {len(excluded)} (Settings).")
        excluded_count = apply_exclusions(options, excluded)
        queue = load_queue(options)
        if not queue:
            print(f"Done: {excluded_count} excluded by a pattern; no address waits for a score"
                  + (" (--rescore scores them again)." if not options["rescore"] else "."))
            return 0
        llm = make_llm_client()
        if llm is None:
            raise CommandError("Set GROQ_API_KEY (or GROK_API_KEY) or OPENAI_API_KEY to score emails.")
        client, model, provider = llm
        minimum = settings_row.email_min_score
        print(f"Scoring {len(queue)} address(es) with {provider} model {model}; ready to email at {minimum}+.")
        counts = {"ready": 0, "needs_review": 0, "failed": 0}
        for position, prospect in enumerate(queue):
            if out_of_time(options, RESERVE_SECONDS):
                time_limit_note(len(queue) - position, "address(es)")
                break
            try:
                answer = score_address(client, model, settings_row.email_scoring_prompt, prospect,
                                       options.get("deadline"))
            except OutOfTime as error:
                print(f"  Stopped: {error}. The rest are scored on the next run.")
                break
            except (APIError, ValueError, KeyError, TypeError) as error:
                counts["failed"] += 1
                detail = rejected_output(error) if isinstance(error, APIError) else None
                print(f"  {prospect.email}: not scored ({type(error).__name__}: {detail or error}); tried again next run.")
                continue
            status = decide(prospect, answer["score"], minimum)
            counts[status] += 1
            print(f"  {prospect.email} [{prospect.email_status}]: {answer['score']} -> "
                  f"{'ready to email' if status == 'ready' else 'needs review'}. {answer['reasoning']}", flush=True)
            if not options["dry_run"]:
                Prospect.objects.filter(pk=prospect.pk, outreach_status__in=SCORABLE_OUTREACH).update(
                    email_score=answer["score"], email_score_reason=answer["reasoning"],
                    email_scored_at=datetime.now(timezone.utc), outreach_status=status)
        print(f"Done: {counts['ready']} ready to email, {counts['needs_review']} need review, "
              f"{excluded_count} excluded by a pattern, {counts['failed']} not scored"
              + (" (dry run, nothing saved)." if options["dry_run"] else "."))
        return 1 if counts["failed"] else 0
    except DatabaseError as error:
        print(f"Database operation failed ({type(error).__name__}): {str(error).splitlines()[0]}")
        return 1
