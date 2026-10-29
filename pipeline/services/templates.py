"""Email templates: a subject and a body with {{variables}}, filled in per prospect. No AI writes any email.

A variable is written {{first_name}}; {{first_name|there}} uses "there" when the prospect has no value for it. An
email whose template uses a variable with no value and no fallback is held back (the run says which variable), so no
email ever goes out with a gap in it. The variables are the prospect's business, contact and campaign, the sender, the
previous email's subject (follow-ups), and every output field of the campaign prompts (what research extracted, e.g.
team_size, languages). The admin's Email variables page lists them with example values.
"""
import os
import re

TEMPLATE_VARIABLE = re.compile(r"\{\{\s*([a-z][a-z0-9_]*)\s*(?:\|([^{}]*))?\}\}")
SENDER_NAME_ENV = "SENDER_NAME"
DEFAULT_SENDER = "The Axioware team"
SENDER_COMPANY = "Axioware"


class TemplateIncomplete(ValueError):
    """The template needs a variable this prospect has no value for (and it has no fallback)."""

    def __init__(self, missing: list[str]):
        self.missing = missing
        super().__init__("no value for " + ", ".join(f"{{{{{name}}}}}" for name in missing)
                         + " (give it a fallback, e.g. {{" + missing[0] + "|...}})")


def _first_name(row: dict) -> str | None:
    if row.get("first_name"):
        first_name = str(row["first_name"]).strip()
        return first_name if "@" not in first_name else None
    words = [word for word in (row.get("contact_name") or "").replace(".", ". ").split()
             if word.rstrip(".").casefold() not in {"dr", "mr", "mrs", "ms", "prof", "engr"}]
    first_name = words[0] if words else None
    return first_name if first_name and "@" not in first_name else None


def _greeting(row: dict) -> str:
    first_name = _first_name(row)
    return f"Hi {first_name}," if first_name else "Hi"


def _job_title(row: dict) -> str | None:
    title = row.get("job_title")
    return re.sub(r"\s*\([^)]*\)", "", title).strip() or None if title else None  # drop our own notes in brackets


def _short_name(row: dict) -> str | None:
    from pipeline.services.stakeholders import search_name

    return search_name(row["business_name"]) if row.get("business_name") else None


def _rating(row: dict) -> str | None:
    rating = row.get("google_rating")
    return f"{float(rating):g}" if rating is not None else None


# name -> (what it is, how to get it from a prospect row)
VARIABLES = {
    "greeting": ("A greeting using the contact's first name when known, or just Hi", _greeting),
    "first_name": ("The contact's first name (without Dr./Mr.)", _first_name),
    "last_name": ("The contact's last name", lambda row: row.get("last_name")),
    "full_name": ("The contact's full name as found", lambda row: row.get("contact_name")),
    "job_title": ("The contact's job title, e.g. Owner", _job_title),
    "business_name": ("The business's name as on Google Maps", lambda row: row.get("business_name")),
    "business_short_name": ("The business's name without the branch Google Maps adds "
                            "(\"Alvi Dental Hospital — SMCHS\" -> \"Alvi Dental Hospital\")", _short_name),
    "category": ("The business's Google Maps category, e.g. Dental clinic", lambda row: row.get("category")),
    "city": ("The business's city", lambda row: row.get("city")),
    "country": ("The business's country", lambda row: row.get("country")),
    "website": ("The business's website", lambda row: row.get("website_url")),
    "google_rating": ("The business's Google rating, e.g. 4.8", _rating),
    "google_review_count": ("How many Google reviews the business has",
                            lambda row: str(row["google_review_count"]) if row.get("google_review_count") is not None
                            else None),
    "campaign_name": ("The campaign's name", lambda row: row.get("campaign_name")),
    "sender_name": ("Your name for the sign-off (SENDER_NAME; \"The Axioware team\" when not set)",
                    lambda row: os.environ.get(SENDER_NAME_ENV, "").strip() or DEFAULT_SENDER),
    "sender_company": ("Your company", lambda row: SENDER_COMPANY),
    "previous_subject": ("Follow-ups only: the subject of the email sent before it",
                         lambda row: row.get("previous_subject")),
}


def research_fields() -> dict[str, str]:
    """Every output field of the campaign prompts (what research extracts): name -> description."""
    from pipeline.models import CampaignPrompt

    fields = {}
    for output_fields in CampaignPrompt.objects.values_list("output_fields", flat=True):
        for field in output_fields or []:
            fields.setdefault(field["name"], f"From research: {field['description']}")
    return fields


def known_variables() -> dict[str, str]:
    """name -> description for every variable a template may use."""
    return {**{name: description for name, (description, _) in VARIABLES.items()},
            **{name: description for name, description in research_fields().items() if name not in VARIABLES}}


def _shown(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, list):
        return ", ".join(str(item) for item in value if item not in (None, "")) or None
    return " ".join(str(value).split()) or None


def values_for(row: dict) -> dict[str, str | None]:
    """The prospect's value for each variable (None when it has none)."""
    values = {name: _shown(getter(row)) for name, (_, getter) in VARIABLES.items()}
    for name, value in (row.get("extracted") or {}).items():
        if name not in VARIABLES:
            values[name] = _shown(value)
    return values


def fill(text: str, values: dict[str, str | None]) -> tuple[str, list[str]]:
    """The text with its variables filled in, and the variables that had no value and no fallback."""
    missing = []

    def replace(match):
        name, fallback = match.group(1), match.group(2)
        value = values.get(name)
        if value:
            return value
        if fallback is not None:
            return fallback.strip()
        if name not in missing:
            missing.append(name)
        return ""

    return TEMPLATE_VARIABLE.sub(replace, text or ""), missing


def render(subject: str, body: str, row: dict) -> tuple[str, str]:
    """(subject, body) of a template filled in for one prospect; TemplateIncomplete when a variable is missing."""
    values = values_for(row)
    filled_subject, missing_subject = fill(subject, values)
    filled_body, missing_body = fill(body, values)
    missing = list(dict.fromkeys(missing_subject + missing_body))
    if missing:
        raise TemplateIncomplete(missing)
    return " ".join(filled_subject.split()), filled_body.strip()


def problems(text: str, known: dict[str, str] | None = None, follow_up: bool = True) -> list[str]:
    """What is wrong with a template's subject or body: unknown variables, or braces that are not a variable."""
    known = known if known is not None else known_variables()
    found = []
    for match in TEMPLATE_VARIABLE.finditer(text or ""):
        name = match.group(1)
        if name not in known:
            found.append(f"{{{{{name}}}}} is not a variable (see the Email variables page for the list)")
        elif name == "previous_subject" and not follow_up:
            found.append("{{previous_subject}} only exists in follow-ups")
    leftover = TEMPLATE_VARIABLE.sub("", text or "")
    broken = re.search(r"\{\{[^}]*\}?\}?", leftover)
    if broken:
        found.append(f"\"{broken.group(0)[:40]}\" is not a valid variable: write {{{{name}}}} or {{{{name|fallback}}}} "
                     "with a lowercase name")
    return list(dict.fromkeys(found))


def used_variables(*texts: str) -> list[str]:
    return list(dict.fromkeys(match.group(1) for text in texts for match in TEMPLATE_VARIABLE.finditer(text or "")))
