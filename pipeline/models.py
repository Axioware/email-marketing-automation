"""The outreach pipeline's data, one model per table.

Table and column names are the ones the database already uses (they were created before the move to Django), so
`manage.py migrate --fake-initial` adopts an existing database without changing it. The Supabase open-tracking edge
function writes to `emails` and `email_open_events` through the `record_email_open` database function.
"""
from django.conf import settings
from django.db import models
from django.db.models.functions import Now
from django.utils import timezone


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
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
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
        ]

    def __str__(self):
        return self.name


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
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "businesses"
        ordering = ["id"]
        verbose_name_plural = "businesses"

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


class Prospect(models.Model):
    """Module 4: a contact email that verification proved deliverable."""

    class EmailStatus(models.TextChoices):
        DELIVERABLE = "deliverable", "Deliverable"
        UNDELIVERABLE = "undeliverable", "Undeliverable"
        RISKY = "risky", "Risky"
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
    email_verification_provider = models.CharField(max_length=64, null=True, blank=True)
    email_verified_at = models.DateTimeField(null=True, blank=True)
    qualification_score = models.IntegerField(null=True, blank=True)
    outreach_status = models.CharField(max_length=32, choices=OutreachStatus.choices, default=OutreachStatus.PENDING, db_index=True)
    outreach_priority = models.CharField(max_length=16, null=True, blank=True)
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
        return self.email_status == "deliverable" and self.outreach_status == "ready" and not self.do_not_contact


class Email(models.Model):
    """Module 5: one generated outreach email, its review state, delivery and opens."""

    class Status(models.TextChoices):
        IN_REVIEW = "in_review", "In review"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        SENDING = "sending", "Sending"
        SENT = "sent", "Sent"
        OPENED = "opened", "Opened"
        FAILED = "failed", "Failed"

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
    generated_at = models.DateTimeField(null=True, blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_note = models.TextField(null=True, blank=True)
    edited_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    sent_from = models.CharField(max_length=320, null=True, blank=True)
    message_id = models.CharField(max_length=255, null=True, blank=True)
    send_error = models.TextField(null=True, blank=True)
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
                condition=models.Q(status__in=["in_review", "approved", "rejected", "sending", "sent", "opened", "failed"]),
                name="ck_emails_status",
            ),
        ]

    def __str__(self):
        return f"#{self.pk} to {self.recipient}"


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
    """One run of a pipeline command, started from the admin or the API and executed in a background process."""

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
