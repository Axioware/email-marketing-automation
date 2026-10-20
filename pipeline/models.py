"""The outreach pipeline's data, one model per table.

Table and column names are the ones the database already uses (they were created before the move to Django), so
`manage.py migrate --fake-initial` adopts an existing database without changing it. The Supabase open-tracking edge
function writes to `emails` and `email_open_events` through the `record_email_open` database function.
"""
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models.functions import Now
from django.utils import timezone


# Extra fields the research agent fills when it scores a business (Campaign prompt > Output fields). The app's own
# fields (action, url, score, ...) drive the research and cannot be changed; these come on top.
OUTPUT_FIELD_TYPES = {
    "text": {"type": "string"},
    "integer": {"type": "integer"},
    "number": {"type": "number"},
    "yes/no": {"type": "boolean"},
    "text list": {"type": "array", "items": {"type": "string"}},
    "integer list": {"type": "array", "items": {"type": "integer"}},
}
CORE_ANSWER_FIELDS = ("action", "url", "score", "reasons", "reasoning", "reason", "summary", "relevant_findings")
MAX_OUTPUT_FIELDS = 20
DEFAULT_OUTPUT_FIELDS = [
    {"name": "team_size", "type": "integer",
     "description": "How many staff the content states (e.g. people listed on a team page, or a stated number)."},
    {"name": "languages", "type": "text list",
     "description": "Languages the clinic says it offers patients, e.g. English, Urdu."},
]


def default_output_fields() -> list[dict]:
    return [dict(field) for field in DEFAULT_OUTPUT_FIELDS]


def check_output_fields(value) -> list[dict]:
    """The output fields as stored ([{name, type, description}]), or ValidationError saying what is wrong."""
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise ValidationError("Output fields must be a list of {name, type, description}.")
    if len(value) > MAX_OUTPUT_FIELDS:
        raise ValidationError(f"At most {MAX_OUTPUT_FIELDS} output fields.")
    fields, names = [], set()
    for number, item in enumerate(value, start=1):
        if not isinstance(item, dict):
            raise ValidationError(f"Field {number}: give a name, a type and a description.")
        name = str(item.get("name") or "").strip()
        kind = str(item.get("type") or "").strip().lower()
        description = " ".join(str(item.get("description") or "").split())
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", name):
            raise ValidationError(f"Field {number}: the name '{name}' must start with a lowercase letter and use only "
                                  "lowercase letters, digits and _ (at most 40), e.g. team_size.")
        if name in CORE_ANSWER_FIELDS:
            raise ValidationError(f"Field {number}: '{name}' is one of the app's own fields "
                                  f"({', '.join(CORE_ANSWER_FIELDS)}); choose another name.")
        if name in names:
            raise ValidationError(f"Field {number}: '{name}' is listed twice.")
        if kind not in OUTPUT_FIELD_TYPES:
            raise ValidationError(f"Field {number} ({name}): the type must be one of: {', '.join(OUTPUT_FIELD_TYPES)}.")
        if not description:
            raise ValidationError(f"Field {number} ({name}): describe what to put in it (the agent reads this).")
        if len(description) > 500:
            raise ValidationError(f"Field {number} ({name}): keep the description under 500 characters.")
        names.add(name)
        fields.append({"name": name, "type": kind, "description": description})
    return fields


class CampaignPrompt(models.Model):
    """How a campaign qualifies businesses: the research agent's whole instructions (what to look for on each website
    and how to score it), used exactly as stored. Campaigns choose one; several campaigns may share it."""

    name = models.CharField(max_length=255, unique=True)
    prompt = models.TextField(help_text="The research agent's instructions, used exactly as written: what makes a "
                                        "business a good prospect for this campaign, what to look for on its website "
                                        "and what each score range (0-100) means. Businesses scoring at least the "
                                        "campaign's minimum score go on to finding decision makers. The answer "
                                        "format is set by the app, plus the output fields below.")
    answer_schema = models.JSONField(
        null=True, blank=True,
        help_text="The research answer's JSON schema as edited on the prompt page (the app's fields with your "
                  "descriptions, plus your own fields); empty: built from the app's fields and the output fields.")
    output_fields = models.JSONField(
        default=default_output_fields, blank=True,
        help_text="Extra facts the agent returns when it scores a business: [{name, type, description}], type one of "
                  "text, integer, number, yes/no, text list, integer list. Each may be null when not stated. Saved on "
                  "the business's website research (extracted).")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "campaign_prompts"
        ordering = ["name"]

    def __str__(self):
        return self.name

    def clean(self):
        try:
            self.output_fields = check_output_fields(self.output_fields)
        except ValidationError as error:
            raise ValidationError({"output_fields": error.messages}) from error


class EmailPrompt(models.Model):
    """An email template: a subject and a body with {{variables}}, filled in per prospect (pipeline/services/
    templates.py). Initial-email templates can be A/B tested (a campaign picks several, split equally); follow-up
    templates are picked per follow-up step. Reusable across campaigns. (The table keeps its old name.)"""

    class Kind(models.TextChoices):
        INITIAL = "initial", "Initial email"
        FOLLOW_UP = "follow_up", "Follow-up"

    name = models.CharField(max_length=255, unique=True)
    kind = models.CharField(max_length=16, choices=Kind.choices, default=Kind.INITIAL, db_default=Kind.INITIAL,
                            help_text="Initial emails can be A/B tested on a campaign; follow-ups are picked per step.")
    subject = models.CharField(max_length=200, default="", db_default="",
                               help_text="E.g. Quick question for {{business_short_name}}. Variables: see Email "
                                         "variables; {{name|fallback}} uses the fallback when there is no value.")
    body = models.TextField(default="", db_default="",
                            help_text="Plain text with {{variables}}; blank lines separate paragraphs. The footer is "
                                      "added automatically.")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "email_prompts"
        ordering = ["kind", "name"]
        verbose_name = "email template"
        constraints = [models.CheckConstraint(condition=models.Q(kind__in=["initial", "follow_up"]),
                                              name="ck_email_prompts_kind")]

    def __str__(self):
        return self.name


LEAD_SOURCES = [("browser", "Built-in browser"), ("scraper_api", "Scraper container")]
DEFAULT_SEND_TIMEZONE = "Asia/Karachi"


class EmailSequence(models.Model):
    """A whole email flow: the initial email's template, then its follow-ups (FollowUp rows), each with its template
    and days. A campaign chooses one or more; with several, prospects are split equally between them (A/B test) and
    each prospect keeps its sequence to the end. Sequences can be shared by campaigns."""

    name = models.CharField(max_length=200, unique=True)
    initial_template = models.ForeignKey(
        EmailPrompt, on_delete=models.SET_NULL, null=True, blank=True, related_name="initial_for_sequences",
        limit_choices_to={"kind": "initial"}, verbose_name="initial-email template",
        help_text="The template of the first email.")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "email_sequences"
        ordering = ["name"]
        verbose_name = "email sequence"

    def __str__(self):
        return self.name


class FollowUp(models.Model):
    """One follow-up in an email sequence: sent `days_after_previous` days after the previous email."""

    email_sequence = models.ForeignKey(EmailSequence, on_delete=models.CASCADE, related_name="follow_ups",
                                       verbose_name="sequence")
    sequence = models.PositiveIntegerField(help_text="1 = the first follow-up (the first email is 0).")
    days_after_previous = models.PositiveIntegerField(default=3, help_text="Days after the previous email was sent.")
    email_prompt = models.ForeignKey(EmailPrompt, on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name="follow_up_steps", limit_choices_to={"kind": "follow_up"},
                                     verbose_name="template", help_text="The follow-up template for this step.")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "follow_ups"
        ordering = ["email_sequence_id", "sequence"]
        verbose_name = "follow-up"
        constraints = [
            models.UniqueConstraint(fields=["email_sequence", "sequence"], name="uq_follow_ups_email_sequence"),
            models.CheckConstraint(condition=models.Q(sequence__gte=1), name="ck_follow_ups_sequence"),
            models.CheckConstraint(condition=models.Q(days_after_previous__gte=1), name="ck_follow_ups_days"),
        ]

    def __str__(self):
        return f"Follow-up {self.sequence} ({self.days_after_previous} days later)"


class DiscoveryCampaign(models.Model):
    """Module 1: what to search Google Maps for."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=255)
    target_country = models.CharField(max_length=100, null=True, blank=True, help_text="One country, e.g. Pakistan.")
    target_locations = models.JSONField(null=True, blank=True, default=list, help_text='List of places, e.g. ["Lahore", "Karachi"].')
    search_terms = models.JSONField(null=True, blank=True, default=list, help_text='List of searches, e.g. ["dental clinic"].')
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    campaign_prompt = models.ForeignKey(
        CampaignPrompt, on_delete=models.SET_NULL, null=True, blank=True, related_name="campaigns",
        help_text="Qualifies this campaign's businesses (website research). Without one, they are not researched.",
    )
    email_sequences = models.ManyToManyField(
        EmailSequence, blank=True, related_name="campaigns", verbose_name="email sequences",
        help_text="The email flows (initial email and follow-ups). With several, prospects are split equally between "
                  "them (A/B test) and each keeps its sequence to the end.")
    min_score = models.PositiveSmallIntegerField(
        default=50, db_default=50, help_text="Businesses scoring at least this go on to finding decision makers; lower ones wait "
                              "for a person to approve or reject them.")
    send_window_start = models.TimeField(null=True, blank=True, help_text="Send only between these times (campaign "
                                                                          "time zone); empty: any time of day.")
    send_window_end = models.TimeField(null=True, blank=True)
    send_timezone = models.CharField(max_length=64, default=DEFAULT_SEND_TIMEZONE, db_default=DEFAULT_SEND_TIMEZONE,
                                     help_text="Time zone of the send window, e.g. Asia/Karachi.")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    lead_source = models.CharField(
        max_length=20, choices=LEAD_SOURCES, default="browser", db_default="browser",
        help_text="How businesses are found on Google Maps: the built-in browser, or the Google Maps scraper container "
                  "(MAPS_SCRAPER_URL in .env).")
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "discovery_campaigns"
        ordering = ["-created_at"]
        verbose_name = "campaign"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(status__in=["pending", "running", "completed", "failed"]),
                name="ck_discovery_campaigns_status",
            ),
            models.CheckConstraint(condition=models.Q(lead_source__in=["browser", "scraper_api"]),
                                   name="ck_discovery_campaigns_lead_source"),
            models.CheckConstraint(condition=models.Q(min_score__lte=100), name="ck_discovery_campaigns_min_score"),
            models.CheckConstraint(
                condition=models.Q(send_window_start__isnull=True, send_window_end__isnull=True)
                | models.Q(send_window_start__isnull=False, send_window_end__isnull=False,
                           send_window_start__lt=models.F("send_window_end")),
                name="ck_discovery_campaigns_send_window"),
        ]

    def __str__(self):
        return self.name

    def clean(self):
        errors = {}
        if self.min_score is not None and self.min_score > 100:
            errors["min_score"] = "Scores go from 0 to 100."
        if (self.send_window_start is None) != (self.send_window_end is None):
            errors["send_window_end"] = "Set both the start and the end of the send window, or neither."
        elif self.send_window_start is not None and self.send_window_start >= self.send_window_end:
            errors["send_window_end"] = "The end must be later than the start (same day, e.g. 09:00 - 18:00)."
        try:
            ZoneInfo(self.send_timezone or "")
        except (ZoneInfoNotFoundError, ValueError):
            errors["send_timezone"] = "Unknown time zone; use a name like Asia/Karachi."
        if errors:
            raise ValidationError(errors)


# Under every email (CAN-SPAM / GDPR): the sender, a postal address and an easy way to opt out.
DEFAULT_FOOTER_TEXT = """Axioware - AI-powered software solutions
Karachi, Pakistan | axioware.tech | business@axioware.tech
If you would rather not hear from us, reply with "unsubscribe"."""


class SendingSettings(models.Model):
    """How fast emails go out, for all campaigns together (they all go from one mailbox), and the footer
    under every email. One row, edited on the admin's Settings page (or the API's /api/settings/); read it with
    SendingSettings.current()."""

    daily_cap = models.PositiveIntegerField(default=40, help_text="Most emails sent in one day, all campaigns together "
                                                                  "(the day in each campaign's time zone).")
    gap_fixed_seconds = models.PositiveIntegerField(default=120, help_text="Fixed gap between two sends, in seconds.")
    gap_random_min_seconds = models.PositiveIntegerField(default=180, help_text="Plus a random gap of at least this many "
                                                                               "seconds...")
    gap_random_max_seconds = models.PositiveIntegerField(default=300, help_text="...and at most this many seconds.")
    reply_window_days = models.PositiveIntegerField(
        default=3, db_default=3, help_text="Days to wait for a reply after a sequence's last email before the lead moves "
                                           "to Quota Exhausted on the Pipeline page.")
    footer_text = models.TextField(
        "footer", default=DEFAULT_FOOTER_TEXT, db_default=DEFAULT_FOOTER_TEXT,
        help_text="Added under every email: who you are, an address and how to opt out. Plain text; each line stays a "
                  "line. Applies to emails written or edited from now on.")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "sending_settings"
        verbose_name = "sending settings"
        verbose_name_plural = "settings"
        constraints = [
            models.CheckConstraint(condition=models.Q(id=1), name="ck_sending_settings_single_row"),
            models.CheckConstraint(condition=models.Q(daily_cap__gte=1), name="ck_sending_settings_daily_cap"),
            models.CheckConstraint(condition=models.Q(gap_random_min_seconds__lte=models.F("gap_random_max_seconds")),
                                   name="ck_sending_settings_gap"),
        ]

    def __str__(self):
        return "Sending settings"

    @classmethod
    def current(cls) -> "SendingSettings":
        return cls.objects.get_or_create(pk=1)[0]

    def save(self, *args, **kwargs):
        self.pk = 1  # there is only one
        super().save(*args, **kwargs)

    def clean(self):
        if self.gap_random_min_seconds is not None and self.gap_random_max_seconds is not None \
                and self.gap_random_min_seconds > self.gap_random_max_seconds:
            raise ValidationError({"gap_random_max_seconds": "Must be at least the minimum random gap."})
        if self.daily_cap is not None and self.daily_cap < 1:
            raise ValidationError({"daily_cap": "At least one email a day."})
        if not (self.footer_text or "").strip():
            raise ValidationError({"footer_text": "Every email needs a footer: who you are, an address and how to "
                                                  "opt out."})


class Business(models.Model):
    """Module 1: a business found on Google Maps."""

    id = models.AutoField(primary_key=True)
    discovery_campaign = models.ForeignKey(DiscoveryCampaign, on_delete=models.CASCADE, related_name="businesses")
    name = models.CharField(max_length=255, null=True, blank=True)
    category = models.CharField(max_length=255, null=True, blank=True)
    website_url = models.TextField(null=True, blank=True)
    domain = models.CharField(max_length=255, null=True, blank=True)
    phone = models.CharField(max_length=50, null=True, blank=True)
    address = models.TextField(null=True, blank=True)
    city = models.CharField(max_length=120, null=True, blank=True)
    state = models.CharField(max_length=120, null=True, blank=True)
    country = models.CharField(max_length=100, null=True, blank=True)
    postal_code = models.CharField(max_length=30, null=True, blank=True)
    latitude = models.DecimalField(max_digits=10, decimal_places=7, null=True, blank=True)
    longitude = models.DecimalField(max_digits=10, decimal_places=7, null=True, blank=True)
    google_place_id = models.CharField(max_length=255, null=True, blank=True, db_index=True)
    google_maps_url = models.TextField(null=True, blank=True)
    google_rating = models.DecimalField(max_digits=2, decimal_places=1, null=True, blank=True)
    google_review_count = models.IntegerField(null=True, blank=True)
    opening_hours = models.JSONField(null=True, blank=True)
    source = models.CharField(max_length=100, null=True, blank=True)
    source_url = models.TextField(null=True, blank=True)
    first_discovered_at = models.DateTimeField(null=True, blank=True)
    last_discovered_at = models.DateTimeField(null=True, blank=True)
    replied_at = models.DateTimeField(null=True, blank=True,
                                      help_text="Set when someone at the business replied: no more follow-ups go to it.")
    review_status = models.CharField(
        max_length=16, choices=[("approved", "Approved"), ("rejected", "Rejected")], null=True, blank=True,
        db_index=True, help_text="A person's decision for a business below the campaign's minimum score (or not "
                                 "researched): approved ones go on to finding decision makers, rejected ones never do.")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "businesses"
        ordering = ["id"]
        verbose_name_plural = "businesses"
        constraints = [  # one business per Google place, whichever campaign or lead source found it
            models.UniqueConstraint(fields=["google_place_id"], name="uq_businesses_google_place_id",
                                    condition=models.Q(google_place_id__isnull=False) & ~models.Q(google_place_id="")),
        ]

    def __str__(self):
        return self.name or f"Business {self.pk}"


class BusinessSource(models.Model):
    """Module 1: the raw listing a business was built from."""

    id = models.AutoField(primary_key=True)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="sources")
    source = models.CharField(max_length=100)
    source_business_id = models.CharField(max_length=255, null=True, blank=True)
    source_url = models.TextField(null=True, blank=True)
    raw_data = models.JSONField(null=True, blank=True)
    discovered_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "business_sources"
        ordering = ["id"]
        verbose_name = "Google Maps listing"

    def __str__(self):
        return f"{self.source} listing for {self.business}"


class BusinessWebsiteProfile(models.Model):
    """Module 2: what the research agent read on the business's website, and its qualification score."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    class DiscoveryStatus(models.TextChoices):
        COMPLETED = "completed", "Contacts found"
        NO_CONTACTS = "no_contacts", "No contacts found"

    id = models.AutoField(primary_key=True)
    business = models.OneToOneField(Business, on_delete=models.CASCADE, related_name="website_profile")
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.PENDING, db_index=True)
    pages_scraped = models.IntegerField(default=0)
    scraped_urls = models.JSONField(default=list, blank=True)
    scraped_pages = models.JSONField(default=list, blank=True)
    discovered_links = models.JSONField(default=list, blank=True)
    emails = models.JSONField(default=list, blank=True)
    qualification_score = models.IntegerField(null=True, blank=True)
    qualification_reasons = models.JSONField(default=list, blank=True)
    agent_reasoning = models.TextField(null=True, blank=True)
    team_size = models.PositiveIntegerField(null=True, blank=True,
                                            help_text="Staff the website or listing states (the team_size output "
                                                      "field); empty when not stated.")
    languages = models.JSONField(null=True, blank=True,
                                 help_text='Languages the clinic says it offers, e.g. ["English", "Urdu"]; empty when '
                                           "not stated (the languages output field).")
    extracted = models.JSONField(null=True, blank=True,
                                 help_text="The campaign prompt's output fields as the agent filled them when it "
                                           "scored, e.g. {\"team_size\": 6, \"languages\": [\"English\"]}.")
    contact_discovery_status = models.CharField(
        max_length=32, choices=DiscoveryStatus.choices, null=True, blank=True, db_index=True,
        help_text="Set by contact discovery (Module 3). Clear it to let discovery run again.",
    )
    contact_discovery_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "business_website_profiles"
        ordering = ["-qualification_score", "id"]
        verbose_name = "website research"
        verbose_name_plural = "website research"

    def __str__(self):
        return f"Website profile of {self.business}"


class BusinessContact(models.Model):
    """Module 3: a decision maker at a business, with the email guesses to verify."""

    class EmailStatus(models.TextChoices):
        UNVERIFIED = "unverified", "Unverified"
        DELIVERABLE = "deliverable", "Deliverable"
        UNDELIVERABLE = "undeliverable", "Undeliverable"
        RISKY = "risky", "Risky"
        UNKNOWN = "unknown", "Unknown"

    id = models.AutoField(primary_key=True)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="contacts")
    name = models.CharField(max_length=255, null=True, blank=True)
    first_name = models.CharField(max_length=120, null=True, blank=True)
    last_name = models.CharField(max_length=120, null=True, blank=True)
    job_title = models.CharField(max_length=255, null=True, blank=True)
    role_type = models.CharField(max_length=64, null=True, blank=True)
    email = models.CharField(max_length=320, null=True, blank=True, db_index=True)
    email_source = models.CharField(
        max_length=32, null=True, blank=True,
        help_text="website, search, inferred or pattern are replaced when discovery reruns; any other value (e.g. manual) is kept.",
    )
    email_status = models.CharField(max_length=32, choices=EmailStatus.choices, null=True, blank=True, default=EmailStatus.UNVERIFIED)
    email_checked_at = models.DateTimeField(null=True, blank=True)
    email_check_details = models.JSONField(null=True, blank=True)
    candidate_emails = models.JSONField(default=list, blank=True)
    linkedin_url = models.TextField(null=True, blank=True)
    source_urls = models.JSONField(default=list, blank=True)
    confidence = models.DecimalField(max_digits=4, decimal_places=3, null=True, blank=True)
    is_primary = models.BooleanField(default=False)
    discovery_reasoning = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "business_contacts"
        ordering = ["business_id", "-is_primary", "id"]
        verbose_name = "contact"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(confidence__isnull=True) | models.Q(confidence__gte=0, confidence__lte=1),
                name="ck_business_contacts_confidence_range",
            ),
        ]

    def __str__(self):
        return self.name or f"Contact {self.pk}"


class PipelineStage(models.TextChoices):
    """The lead pipeline. Sending moves leads through the first stages;
    a person moves them on replies and meetings (pipeline/services/stages.py)."""

    NEW_LEAD = "new_lead", "New Lead"
    READY_TO_SEND = "ready_to_send", "Ready to Send"
    INITIAL_EMAIL = "initial_email", "Initial Email"
    FOLLOW_UP = "follow_up", "Follow-up"
    REPLIED = "replied", "Replied"
    DEMO_BOOKED = "demo_booked", "Demo Booked"
    DEMO_DONE = "demo_done", "Demo Done"
    WON = "won", "Won"
    LOST = "lost", "Lost"
    QUOTA_EXHAUSTED = "quota_exhausted", "Quota Exhausted"


class Prospect(models.Model):
    """Module 4: a checked contact email with its verification result. Only deliverable ones are ready to email."""

    class EmailStatus(models.TextChoices):
        DELIVERABLE = "deliverable", "Deliverable"
        UNDELIVERABLE = "undeliverable", "Undeliverable"
        RISKY = "risky", "Risky"
        UNKNOWN = "unknown", "Unknown"

    class Verdict(models.TextChoices):
        DELIVERABLE = "deliverable", "Deliverable"
        CATCH_ALL = "catch_all", "Catch-all domain"
        FULL_INBOX = "full_inbox", "Inbox full"
        DISPOSABLE = "disposable", "Disposable address"
        RISKY = "risky", "Risky"
        MAILBOX_NOT_FOUND = "mailbox_not_found", "Mailbox not found"
        DISABLED = "disabled", "Mailbox disabled"
        NO_MAIL_SERVER = "no_mail_server", "Domain has no mail server"
        INVALID_SYNTAX = "invalid_syntax", "Invalid address"
        SMTP_UNREACHABLE = "smtp_unreachable", "Mail server unreachable"
        BLOCKED = "blocked", "Mail server refused the check"
        TEMPORARY_FAILURE = "temporary_failure", "Temporary failure (greylisting)"
        CHECK_FAILED = "check_failed", "Check failed"
        UNKNOWN = "unknown", "Unknown"

    class OutreachStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        READY = "ready", "Ready"
        NEEDS_REVIEW = "needs_review", "Needs review"
        REJECTED = "rejected", "Rejected"
        CONTACTED = "contacted", "Contacted"

    id = models.AutoField(primary_key=True)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="prospects")
    contact = models.ForeignKey(BusinessContact, on_delete=models.CASCADE, related_name="prospects")
    email = models.CharField(max_length=320, db_index=True)
    email_status = models.CharField(max_length=32, choices=EmailStatus.choices, null=True, blank=True, db_index=True)
    verdict = models.CharField(max_length=32, choices=Verdict.choices, null=True, blank=True, db_index=True,
                               help_text="Why verification gave this status, e.g. catch_all or mailbox_not_found.")
    verification_note = models.TextField(null=True, blank=True)
    verification_details = models.JSONField(null=True, blank=True, help_text="Reacher's findings for this address.")
    email_verification_provider = models.CharField(max_length=64, null=True, blank=True)
    email_verified_at = models.DateTimeField(null=True, blank=True)
    qualification_score = models.IntegerField(null=True, blank=True)
    outreach_status = models.CharField(max_length=32, choices=OutreachStatus.choices, default=OutreachStatus.PENDING, db_index=True)
    outreach_priority = models.CharField(max_length=16, null=True, blank=True)
    email_sequence = models.ForeignKey(
        EmailSequence, on_delete=models.SET_NULL, null=True, blank=True, related_name="prospects",
        verbose_name="email sequence",
        help_text="The sequence this prospect gets (A/B group), set when its first email is written; its follow-ups "
                  "come from it.")
    approved_by_person = models.BooleanField(
        default=False, db_default=False, help_text="A person approved emailing this address although verification did not confirm it.")
    pipeline_stage = models.CharField(
        max_length=32, choices=PipelineStage.choices, null=True, blank=True, db_index=True,
        help_text="Where the lead is on the Pipeline page. Empty: not in the pipeline yet (shown as New Lead once ready).")
    stage_changed_at = models.DateTimeField(null=True, blank=True)
    sequence_stopped_at = models.DateTimeField(null=True, blank=True,
                                               help_text="No more emails to this prospect (reply or a manual stage move).")
    sequence_stop_reason = models.CharField(max_length=64, null=True, blank=True)
    outreach_facts = models.JSONField(default=list, blank=True)
    research_summary = models.TextField(null=True, blank=True)
    do_not_contact = models.BooleanField(default=False)
    last_contacted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "prospects"
        ordering = ["id"]
        constraints = [models.UniqueConstraint(fields=["contact", "email"], name="uq_prospects_contact_email")]

    def __str__(self):
        return self.email

    @property
    def is_ready(self) -> bool:
        return ((self.email_status == "deliverable" or self.approved_by_person) and self.outreach_status == "ready"
                and not self.do_not_contact)

    @property
    def stage(self) -> str:
        """The pipeline stage, New Lead until something moved it."""
        return self.pipeline_stage or PipelineStage.NEW_LEAD


class StageChange(models.Model):
    """One move of a lead between pipeline stages: by a person (who) or by the app (sending)."""

    prospect = models.ForeignKey(Prospect, on_delete=models.CASCADE, related_name="stage_changes")
    from_stage = models.CharField(max_length=32, choices=PipelineStage.choices, null=True, blank=True)
    to_stage = models.CharField(max_length=32, choices=PipelineStage.choices)
    changed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+", help_text="Empty: the app (sending, the sequence ending).")
    note = models.CharField(max_length=255, blank=True, default="")
    changed_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "stage_changes"
        ordering = ["-changed_at", "-id"]
        verbose_name = "stage change"

    def __str__(self):
        return f"{self.prospect_id}: {self.from_stage or 'new_lead'} -> {self.to_stage}"


class Email(models.Model):
    """Module 5: one generated outreach email, its review state, delivery and opens."""

    class Status(models.TextChoices):
        IN_REVIEW = "in_review", "In review"
        APPROVED = "approved", "Approved"
        QUEUED = "queued", "Queued"
        REJECTED = "rejected", "Rejected"
        SENDING = "sending", "Sending"
        SENT = "sent", "Sent"
        OPENED = "opened", "Opened"
        FAILED = "failed", "Failed"
        REPLIED = "replied", "Replied"

    id = models.AutoField(primary_key=True)
    prospect = models.ForeignKey(Prospect, on_delete=models.CASCADE, related_name="emails")
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="emails")
    contact = models.ForeignKey(BusinessContact, on_delete=models.CASCADE, related_name="emails")
    sequence_step = models.IntegerField(default=1)
    recipient = models.CharField(max_length=320)
    subject = models.TextField()
    body_text = models.TextField()
    body_html = models.TextField()
    tracking_token = models.CharField(max_length=128, unique=True)
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.IN_REVIEW, db_index=True)
    generation_provider = models.CharField(max_length=32, null=True, blank=True)
    generation_model = models.CharField(max_length=128, null=True, blank=True)
    email_prompt = models.ForeignKey(EmailPrompt, on_delete=models.SET_NULL, null=True, blank=True, related_name="emails",
                                     verbose_name="template", help_text="The template this email was filled from.")
    generated_at = models.DateTimeField(null=True, blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_note = models.TextField(null=True, blank=True)
    edited_at = models.DateTimeField(null=True, blank=True)
    scheduled_at = models.DateTimeField(null=True, blank=True, db_index=True,
                                        help_text="When it goes out: the slot given when it was approved. Queued "
                                                  "emails are sent then (Outbox).")
    queued_at = models.DateTimeField(null=True, blank=True, help_text="When it was queued for sending (Outbox).")
    sent_at = models.DateTimeField(null=True, blank=True)
    sent_from = models.CharField(max_length=320, null=True, blank=True)
    message_id = models.CharField(max_length=255, null=True, blank=True)
    send_error = models.TextField(null=True, blank=True, help_text="Why the last send failed.")
    first_opened_at = models.DateTimeField(null=True, blank=True)
    last_opened_at = models.DateTimeField(null=True, blank=True)
    open_count = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "emails"
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(fields=["prospect", "sequence_step"], name="uq_emails_prospect_step"),
            models.CheckConstraint(
                condition=models.Q(status__in=["in_review", "approved", "queued", "rejected", "sending", "sent",
                                               "opened", "failed", "replied"]),
                name="ck_emails_status",
            ),
        ]

    def __str__(self):
        return f"#{self.pk} to {self.recipient}"


class OutboxEmail(Email):
    """Emails waiting to be sent (approved, queued, failed), for the Outbox page."""

    class Meta:
        proxy = True
        verbose_name = "outbox email"
        verbose_name_plural = "outbox"


class EmailOpenEvent(models.Model):
    """One recorded open, written by the tracking edge function (rate limited per email in the database)."""

    id = models.BigAutoField(primary_key=True)
    email = models.ForeignKey(Email, on_delete=models.CASCADE, related_name="open_events")
    opened_at = models.DateTimeField(db_default=Now())
    user_agent = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "email_open_events"
        ordering = ["-opened_at"]
        verbose_name = "email open"
        # The (email_id, opened_at) index used by the rate limit is created in migration 0002.

    def __str__(self):
        return f"Open of email #{self.email_id}"


class PipelineRun(models.Model):
    """One run of a pipeline command, started from the admin or the API and executed in a background process on this
    machine (pipeline/jobs.py)."""

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    FINISHED = (Status.SUCCEEDED, Status.FAILED, Status.CANCELLED)

    command = models.CharField(max_length=64)
    arguments = models.JSONField(default=list, blank=True, help_text="Command-line arguments, e.g. [\"--limit\", \"5\"].")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.QUEUED, db_index=True)
    output = models.TextField(blank=True, default="")
    exit_code = models.IntegerField(null=True, blank=True)
    pid = models.IntegerField(null=True, blank=True)
    cancel_requested = models.BooleanField(default=False, db_default=False, help_text="Stop asked for.")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "pipeline_runs"
        ordering = ["-created_at"]

    def __str__(self):
        return f"Run #{self.pk}: {self.command}"

    @property
    def is_finished(self) -> bool:
        return self.status in self.FINISHED

    @property
    def command_line(self) -> str:
        import shlex

        return " ".join(["python manage.py", self.command, *(shlex.quote(str(a)) for a in self.arguments)])
