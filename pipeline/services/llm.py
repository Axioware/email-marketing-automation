"""LLM client selection shared by every pipeline step that calls a model.

Groq is used when its key is set (GROQ_API_KEY, or GROK_API_KEY as an alias), otherwise OpenAI. Groq exposes an
OpenAI-compatible API, so the OpenAI SDK is used for both.
"""
import os
import time

from openai import BadRequestError, OpenAI, RateLimitError

from pipeline.management.base import OutOfTime, call_timeout

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_GROQ_MODEL = "openai/gpt-oss-120b"  # supports the strict JSON-schema responses the pipeline requires
DEFAULT_OPENAI_MODEL = "gpt-5.4-mini"
DEFAULT_TIMEOUT = 60
MIN_TIME_AFTER_WAIT = 20  # with a deadline: wait out a rate limit only if this much time is left afterwards


def make_llm_client() -> tuple[OpenAI, str, str] | None:
    """(client, model, provider name). Groq when its key is set, otherwise OpenAI; None if neither.

    The Groq key is read from GROQ_API_KEY, or GROK_API_KEY as an alias.
    """
    groq_key = (os.environ.get("GROQ_API_KEY", "") or os.environ.get("GROK_API_KEY", "")).strip()
    if groq_key:
        model = os.environ.get("GROQ_MODEL", "").strip() or DEFAULT_GROQ_MODEL
        # More retries: the free tier answers 429 when the per-minute token budget is spent; the SDK waits and retries.
        return OpenAI(api_key=groq_key, base_url=GROQ_BASE_URL, max_retries=6), model, "groq"
    openai_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if openai_key:
        model = os.environ.get("OPENAI_MODEL", "").strip() or DEFAULT_OPENAI_MODEL
        return OpenAI(api_key=openai_key), model, "openai"
    return None


def rejected_output(error: Exception) -> str | None:
    """The model's answer when the provider refused it for not matching the JSON schema (Groq checks strict schemas
    itself and answers 400 json_validate_failed, with the answer in failed_generation); None for any other error."""
    if not isinstance(error, BadRequestError):
        return None
    body = error.body if isinstance(error.body, dict) else {}
    body = body.get("error", body) if isinstance(body.get("error", body), dict) else body
    if body.get("code") != "json_validate_failed":
        return None
    output = body.get("failed_generation")
    return output if isinstance(output, str) else ""


REASONING_PREFIXES = ("gpt-5", "gpt-6", "o1", "o3", "o4")  # OpenAI reasoning models: default temperature only
REASONING_HEADROOM = 6000  # tokens a reasoning model may spend thinking before it answers


def is_reasoning_model(model: str) -> bool:
    return model.split("/")[-1].lower().startswith(REASONING_PREFIXES)


def tune(model: str, temperature: float, max_completion_tokens: int) -> dict:
    """Sampling settings the model accepts. OpenAI's reasoning models (gpt-5, o-series) refuse any temperature but
    the default and count their hidden reasoning against max_completion_tokens, so they get no temperature, low
    reasoning effort and room to think; other models (e.g. gpt-oss on Groq) get both as asked."""
    if is_reasoning_model(model):
        return {"reasoning_effort": "low", "max_completion_tokens": max_completion_tokens + REASONING_HEADROOM}
    return {"temperature": temperature, "max_completion_tokens": max_completion_tokens}


def complete(client: OpenAI, deadline: float | None = None, timeout: float = DEFAULT_TIMEOUT, **request):
    """client.chat.completions.create(**request). With a deadline (the web app on Vercel) nothing runs past it: the
    SDK's own retries (which wait out rate limits for minutes) are off, each call may only use the time left, and a
    rate limit is waited out here only when there is time for it afterwards; otherwise OutOfTime."""
    try:
        return _create(client, deadline, timeout, request)
    except BadRequestError as error:  # a model that refuses a sampling setting: once more without it
        rejected = getattr(error, "param", None) or (getattr(error, "body", None) or {}).get("param")
        if rejected not in ("temperature", "reasoning_effort") or rejected not in request:
            raise
        return _create(client, deadline, timeout, {k: v for k, v in request.items() if k != rejected})


def _create(client: OpenAI, deadline: float | None, timeout: float, request: dict):
    if deadline is None:
        return client.chat.completions.create(timeout=timeout, **request)
    bounded = client.with_options(max_retries=0)
    while True:
        try:
            return bounded.chat.completions.create(timeout=call_timeout(deadline, timeout), **request)
        except RateLimitError as error:
            wait = retry_after(error)
            if deadline - time.monotonic() - wait < MIN_TIME_AFTER_WAIT:
                raise OutOfTime("the AI provider's rate limit (try again in a minute)") from error
            time.sleep(wait)


def retry_after(error: RateLimitError) -> float:
    """Seconds the provider asks to wait (Retry-After), 10 when it does not say."""
    try:
        return max(1.0, float(error.response.headers.get("retry-after")))
    except (AttributeError, TypeError, ValueError):
        return 10.0
