"""Usage tracking: every AI request (its tokens) and every Hunter request (its credits) becomes a UsageRecord, with the
action, business, prospect and pipeline run it was for (the admin's Usage page; GET /api/usage/).

Callers say what they are doing with `track(action=..., business_id=..., prospect_id=...)` around their calls; the
run id is set once per pipeline run (jobs.execute_run). llm.complete and the Hunter client record the calls themselves,
so nothing is missed, and a failure to record never stops the work.
"""
import contextvars
import logging
import math
from contextlib import contextmanager
from decimal import Decimal

logger = logging.getLogger(__name__)
_context: contextvars.ContextVar[dict] = contextvars.ContextVar("usage_context", default={})
HUNTER_VERIFICATION_CREDITS = Decimal("0.5")


@contextmanager
def track(**values):
    """Attribute the calls made inside this block: action, business_id, prospect_id, run_id."""
    token = _context.set({**_context.get(), **{key: value for key, value in values.items() if value is not None}})
    try:
        yield
    finally:
        _context.reset(token)


def record(provider: str, model: str, **fields) -> None:
    from pipeline.models import UsageRecord

    context = _context.get()
    try:
        UsageRecord.objects.create(provider=provider, model=(model or "")[:100], action=context.get("action", "other"),
                                   business_id=context.get("business_id"), prospect_id=context.get("prospect_id"),
                                   run_id=context.get("run_id"), **fields)
    except Exception:  # noqa: BLE001 - tracking must never stop the work it tracks
        logger.warning("Could not record usage for %s %s", provider, model, exc_info=True)


def record_ai(client, model: str, response) -> None:
    """Record the tokens of one AI response (nothing when the response has no usage, e.g. in tests)."""
    tokens = getattr(response, "usage", None)
    if tokens is None:
        return
    details = getattr(tokens, "completion_tokens_details", None)
    provider = "groq" if "groq" in str(getattr(client, "base_url", "")) else "openai"
    record(provider, model, input_tokens=getattr(tokens, "prompt_tokens", 0) or 0,
           output_tokens=getattr(tokens, "completion_tokens", 0) or 0,
           reasoning_tokens=getattr(details, "reasoning_tokens", 0) or 0)


def record_hunter_search(target: str, emails_returned: int) -> None:
    record("hunter", "domain-search", hunter_credits=Decimal(math.ceil(emails_returned / 10)),
           detail=f"{target} ({emails_returned} email(s))"[:255])


def record_hunter_verification(address: str, finished: bool = True) -> None:
    record("hunter", "email-verifier", hunter_credits=HUNTER_VERIFICATION_CREDITS if finished else Decimal(0),
           detail=(address if finished else f"{address} (not finished)")[:255])
