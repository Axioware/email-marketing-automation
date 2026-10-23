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


QUALIFICATION_PROMPT = "TEST CRITERIA: appointment-based clinics that book by phone score high."
EMAIL_TEXT = "TEST TEMPLATE"
_unset = object()


def make_prompts(name=None, qualification=QUALIFICATION_PROMPT):
    """A campaign prompt (qualification)."""
    from pipeline.models import CampaignPrompt

    return CampaignPrompt.objects.create(name=name or f"Prompts {CampaignPrompt.objects.count() + 1}",
                                         prompt=qualification)


def make_email_prompt(name=None, text=EMAIL_TEXT, kind="initial", subject=None, body=None):
    """An email template. `text` marks its body so tests can tell templates apart."""
    from pipeline.models import EmailPrompt

    follow_up = kind == "follow_up"
    subject = subject or ("Re: {{previous_subject}}" if follow_up else f"{text} for {{{{business_short_name}}}}")
    body = body or ("Hi {{first_name|there}},\n\n" + text + " about {{business_name}}.\n\n{{sender_name}}")
    return EmailPrompt.objects.create(name=name or f"Email template {EmailPrompt.objects.count() + 1}", kind=kind,
                                      subject=subject, body=body)


def make_sequence(initial=None, name=None, follow_ups=()):
    """An email sequence: `initial` template, then (days, template) follow-ups."""
    from pipeline.models import EmailSequence, FollowUp

    sequence = EmailSequence.objects.create(name=name or (initial.name if initial else "Sequence"),
                                            initial_template=initial)
    for number, (days, template) in enumerate(follow_ups, start=1):
        FollowUp.objects.create(email_sequence=sequence, sequence=number, days_after_previous=days,
                                email_prompt=template)
    return sequence


def make_campaign(campaign_prompt=_unset, initial_prompts=_unset, sequences=None, **fields) -> DiscoveryCampaign:
    """A campaign with its own campaign prompt and one email sequence per initial-email template (named after it;
    default: one new template). Pass campaign_prompt=None or initial_prompts=[] for none, or `sequences`."""
    from pipeline.models import EmailSequence

    if campaign_prompt is _unset:
        campaign_prompt = make_prompts()
    campaign = DiscoveryCampaign.objects.create(**{"name": "test", "campaign_prompt": campaign_prompt, **fields})
    if sequences is None:
        templates = [make_email_prompt()] if initial_prompts is _unset else initial_prompts
        sequences = [EmailSequence.objects.get_or_create(name=t.name, defaults={"initial_template": t})[0]
                     for t in templates]
    campaign.email_sequences.set(sequences)
    return campaign


def add_follow_up(campaign, sequence, days, template=None):
    """Follow-up `sequence` (number) in every email sequence of the campaign."""
    from pipeline.models import FollowUp

    for email_sequence in campaign.email_sequences.all():
        FollowUp.objects.create(email_sequence=email_sequence, sequence=sequence, days_after_previous=days,
                                email_prompt=template)


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


def review_env(llm=None) -> dict:
    """Environment for writing and reviewing emails (templates need no AI key)."""
    return {"EMAIL_TRACKING_BASE_URL": BASE, "SENDER_NAME": "Sender"}


# Extra fields the campaign form needs (none since follow-ups moved to email sequences).
CAMPAIGN_INLINE = {}


TEST_MAILBOX_KEY = "pbPjVY8u9gmRZ5PiJn7f0Jx0cHqYwvTt9cHOmGKe3Lw="  # a Fernet key for tests only


def make_mailbox(name="Main", from_email="ali@axioware.tech", from_name="Ali Khan", priority=1, password="pw",
                 **fields):
    """A mailbox (SMTP account) with an encrypted password; needs MAILBOX_ENCRYPTION_KEY=TEST_MAILBOX_KEY set."""
    from pipeline.models import Mailbox

    mailbox = Mailbox(name=name, from_email=from_email, from_name=from_name, host="smtp.test", port=587,
                      username=from_email, priority=priority, **fields)
    mailbox.set_password(password)
    mailbox.save()
    return mailbox

