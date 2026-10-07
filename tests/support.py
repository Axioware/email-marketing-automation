"""Helpers for running pipeline commands in-process and building test data."""
import io
import os
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError

from pipeline.models import Business, BusinessContact, BusinessWebsiteProfile, DiscoveryCampaign

ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]


@dataclass
class Result:
    returncode: int
    stdout: str
    stderr: str


def run_command(name: str, *args, env: dict | None = None) -> Result:
    """Run a management command like the CLI would: (exit code, printed output, error message)."""
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with mock.patch.dict(os.environ, {k: str(v) for k, v in (env or {}).items()}), redirect_stdout(out), redirect_stderr(err):
        try:
            call_command(name, *[str(a) for a in args])
        except CommandError as error:
            code = error.returncode or 1
            err.write(f"CommandError: {error}\n")
    return Result(code, out.getvalue(), err.getvalue())


def make_campaign(**fields) -> DiscoveryCampaign:
    return DiscoveryCampaign.objects.create(**{"name": "test", **fields})


def make_business(campaign=None, name="Business", score=None, **fields) -> Business:
    business = Business.objects.create(discovery_campaign=campaign or make_campaign(), name=name, **fields)
    if score is not None:
        BusinessWebsiteProfile.objects.create(business=business, status="completed", qualification_score=score)
    return business


def make_contact(business, email=None, candidates=(), primary=True, **fields) -> BusinessContact:
    return BusinessContact.objects.create(
        business=business, name=fields.pop("name", f"Person {business.pk}"), email=email, email_source="inferred",
        is_primary=primary, candidate_emails=[{"email": c, "pattern": "x", "confidence": 0.1} for c in candidates],
        **fields,
    )


BASE = "https://ref.supabase.co/functions/v1/email/footer"


def make_email(status="in_review", body="Hi Ali,\n\nA short note.", subject="Hello there", recipient=None, campaign=None,
               **prospect_cols):
    """A generated email with its business (Module 2 profile), contact and ready prospect."""
    from pipeline.models import Email, Prospect
    from pipeline.services import generation as g

    business = make_business(campaign, "Clinic", city="Lahore")
    business.name = f"Clinic {business.pk}"
    business.save(update_fields=["name"])
    BusinessWebsiteProfile.objects.create(business=business, status="completed", qualification_score=80,
                                          qualification_reasons=["Strong reviews"],
                                          scraped_pages=[{"relevant_findings": ["Hours: 9-5"]}])
    contact = make_contact(business, name="Ali Khan", first_name="Ali")
    recipient = recipient or f"ali{business.pk}@clinic{business.pk}.org"
    cols = {"email_status": "deliverable", "outreach_status": "ready", "do_not_contact": False, **prospect_cols}
    prospect = Prospect.objects.create(business=business, contact=contact, email=recipient, **cols)
    token = g.new_tracking_token()
    return Email.objects.create(prospect=prospect, business=business, contact=contact, recipient=recipient,
                                subject=subject, body_text=g.render_text(body),
                                body_html=g.render_html(body, g.tracking_url(BASE, token)), tracking_token=token,
                                status=status)


def review_env(smtp, llm) -> dict:
    return {
        "EMAIL_TRACKING_BASE_URL": BASE, "OPENAI_API_KEY": "test", "OPENAI_BASE_URL": llm.url,
        "OPENAI_MODEL": "fake-model", "GROQ_API_KEY": "", "GROK_API_KEY": "",
        "SMTP_HOST": "127.0.0.1", "SMTP_PORT": str(smtp.port), "SMTP_SECURITY": "none",
        "SMTP_USERNAME": "user@x.org", "SMTP_PASSWORD": "secret", "SMTP_FROM_EMAIL": "user@x.org",
        "SMTP_FROM_NAME": "Sender", "SMTP_REPLY_TO": "",
    }
