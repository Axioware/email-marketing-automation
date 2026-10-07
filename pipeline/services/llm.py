"""LLM client selection shared by every pipeline step that calls a model.

Groq is used when its key is set (GROQ_API_KEY, or GROK_API_KEY as an alias), otherwise OpenAI. Groq exposes an
OpenAI-compatible API, so the OpenAI SDK is used for both.
"""
import os

from openai import OpenAI

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_GROQ_MODEL = "openai/gpt-oss-120b"  # supports the strict JSON-schema responses the pipeline requires
DEFAULT_OPENAI_MODEL = "gpt-5.4-mini"


def make_llm_client() -> tuple[OpenAI, str, str] | None:
    """(client, model, provider name). Groq when its key is set, otherwise OpenAI; None if neither.

    The Groq key is read from GROQ_API_KEY, or GROK_API_KEY as an alias.
    """
    groq_key = (os.environ.get("GROQ_API_KEY", "") or os.environ.get("GROK_API_KEY", "")).strip()
    if groq_key:
        model = os.environ.get("GROQ_MODEL", "").strip() or DEFAULT_GROQ_MODEL
        return OpenAI(api_key=groq_key, base_url=GROQ_BASE_URL), model, "groq"
    openai_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if openai_key:
        model = os.environ.get("OPENAI_MODEL", "").strip() or DEFAULT_OPENAI_MODEL
        return OpenAI(api_key=openai_key), model, "openai"
    return None
