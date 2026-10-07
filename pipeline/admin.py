"""Django admin for the whole pipeline.

Every pipeline step can be started from here: list actions and buttons open the "Run a pipeline step" form, pre-filled
with the command and arguments (e.g. the selected businesses), where the run is confirmed and then followed live.
Generated emails are reviewed on their own page: edit, preview, approve, reject, regenerate and send.
"""
import json
import shlex
from urllib.parse import urlencode, urlsplit

from django import forms
from django.contrib import admin, messages
from django.db import transaction
from django.db.models import Count, F
from django.http import HttpResponseNotAllowed, HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.urls import path, reverse
from django.utils.html import format_html, format_html_join
from django.utils.text import Truncator

from pipeline import jobs, review
from pipeline.models import (
    Business,
    BusinessContact,
    BusinessSource,
    BusinessWebsiteProfile,
    DiscoveryCampaign,
    Email,
    EmailOpenEvent,
    PipelineRun,
    Prospect,
)
from pipeline.services import generation, sending


# ---------------------------------------------------------------- helpers


def admin_url(obj) -> str:
    return reverse(f"admin:{obj._meta.app_label}_{obj._meta.model_name}_change", args=[obj.pk])


def admin_link(obj, label=None):
    if obj is None:
        return "-"
    return format_html('<a href="{}">{}</a>', admin_url(obj), label if label is not None else str(obj))


def changelist_link(model, label, **filters):
    url = reverse(f"admin:pipeline_{model._meta.model_name}_changelist") + "?" + urlencode(filters)
    return format_html('<a href="{}">{}</a>', url, label)


def safe_url(url) -> str | None:
    """The URL if it is http(s), else None: scraped data must never become a javascript: or data: link."""
    try:
        return url if isinstance(url, str) and urlsplit(url.strip()).scheme.lower() in ("http", "https") else None
    except ValueError:
        return None


def external_link(url, label=None):
    """A new-tab link for http(s) URLs; anything else is shown as plain text."""
    if not url:
        return "-"
    if safe_url(url) is None:
        return format_html("{}", label or url)
    return format_html('<a href="{}" target="_blank" rel="noopener noreferrer">{}</a>', url, label or url)


def pretty_json(value):
    if value in (None, [], {}):
        return "-"
    return format_html('<pre class="pipeline-pre">{}</pre>', json.dumps(value, indent=2, ensure_ascii=False, default=str))


def badge(value: str, label: str | None = None):
    return format_html('<span class="pipeline-badge pipeline-{}">{}</span>', value or "none", label or value or "-")


def run_form_url(command: str, arguments: list) -> str:
    query = urlencode({"command": command, "arguments": shlex.join(str(a) for a in arguments)})
    return f"{reverse('admin:pipeline_pipelinerun_add')}?{query}"


def ids_arguments(flag: str, ids) -> list[str]:
    return [item for pk in sorted(set(ids)) for item in (flag, str(pk))]


def run_step(command: str, arguments: list):
    """Admin action helper: open the run form pre-filled, so the run is reviewed and confirmed before it starts."""
    return HttpResponseRedirect(run_form_url(command, arguments))


class PipelineAdmin(admin.ModelAdmin):
    list_per_page = 50


class ReadOnlyInline(admin.TabularInline):
    extra = 0
    can_delete = False
    show_change_link = True

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


# ---------------------------------------------------------------- Module 1: campaigns and businesses


class DiscoveryCampaignForm(forms.ModelForm):
    target_locations = forms.CharField(required=False, help_text="Comma-separated, e.g. Lahore, Karachi.")
    search_terms = forms.CharField(required=False, help_text="Comma-separated, e.g. dental clinic, dentist.")

    class Meta:
        model = DiscoveryCampaign
        fields = ["name", "target_country", "target_locations", "search_terms", "status"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in ("target_locations", "search_terms"):
            value = getattr(self.instance, field, None)
            if isinstance(value, list):
                self.initial[field] = ", ".join(str(item) for item in value)

    def _split(self, field):
        return [item.strip() for item in (self.cleaned_data.get(field) or "").split(",") if item.strip()]

    def clean_target_locations(self):
        return self._split("target_locations")

    def clean_search_terms(self):
        return self._split("search_terms")

    def clean_target_country(self):
        country = (self.cleaned_data.get("target_country") or "").strip()
        if "," in country:
            raise forms.ValidationError("Enter one country only.")
        return country or None


@admin.register(DiscoveryCampaign)
class DiscoveryCampaignAdmin(PipelineAdmin):
    form = DiscoveryCampaignForm
    list_display = ["name", "target_country", "locations", "terms", "status_badge", "business_count", "created_at"]
    list_filter = ["status", "target_country"]
    search_fields = ["name"]
    readonly_fields = ["created_at", "updated_at", "started_at", "completed_at", "businesses_link"]
    fieldsets = [
        (None, {"fields": ["name", "target_country", "target_locations", "search_terms"]}),
        ("Progress", {"fields": ["status", "businesses_link", "started_at", "completed_at", "created_at", "updated_at"]}),
    ]
    actions = ["fetch_businesses"]
    change_form_template = "admin/pipeline/discoverycampaign/change_form.html"

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(business_total=Count("businesses"))

    @admin.display(description="Locations")
    def locations(self, obj):
        return ", ".join(map(str, obj.target_locations or [])) or "-"

    @admin.display(description="Search terms")
    def terms(self, obj):
        return ", ".join(map(str, obj.search_terms or [])) or "-"

    @admin.display(description="Status", ordering="status")
    def status_badge(self, obj):
        return badge(obj.status, obj.get_status_display())

    @admin.display(description="Businesses", ordering="business_total")
    def business_count(self, obj):
        return changelist_link(Business, obj.business_total, discovery_campaign__id__exact=obj.pk)

    @admin.display(description="Businesses")
    def businesses_link(self, obj):
        if not obj.pk:
            return "-"
        return changelist_link(Business, f"{obj.businesses.count()} businesses", discovery_campaign__id__exact=obj.pk)

    @admin.action(description="Fetch businesses from Google Maps (Module 1)")
    def fetch_businesses(self, request, queryset):
        if queryset.count() != 1:
            self.message_user(request, "Select exactly one campaign.", messages.WARNING)
            return None
        return run_step("fetch_businesses", ["--campaign-id", str(queryset.first().pk), "--limit", "20"])

    def render_change_form(self, request, context, *args, **kwargs):
        obj = context.get("original")
        if obj is not None:
            context["fetch_url"] = run_form_url("fetch_businesses", ["--campaign-id", str(obj.pk), "--limit", "20"])
        return super().render_change_form(request, context, *args, **kwargs)


class BusinessSourceInline(ReadOnlyInline):
    model = BusinessSource
    fields = ["source", "source_business_id", "source_url", "discovered_at"]
    readonly_fields = fields
    classes = ["collapse"]


class BusinessContactInline(ReadOnlyInline):
    model = BusinessContact
    fields = ["name", "job_title", "email", "email_source", "email_status", "is_primary", "confidence"]
    readonly_fields = fields


class ProspectInline(ReadOnlyInline):
    model = Prospect
    fk_name = "business"
    fields = ["email", "contact", "email_status", "outreach_status", "do_not_contact", "last_contacted_at"]
    readonly_fields = fields


class EmailInline(ReadOnlyInline):
    model = Email
    fk_name = "business"
    fields = ["recipient", "subject", "status", "sent_at", "open_count"]
    readonly_fields = fields


class HasWebsiteFilter(admin.SimpleListFilter):
    title = "website"
    parameter_name = "has_website"

    def lookups(self, request, model_admin):
        return [("yes", "Has a website"), ("no", "No website")]

    def queryset(self, request, queryset):
        if self.value() == "yes":
            return queryset.filter(website_url__isnull=False).exclude(website_url="")
        if self.value() == "no":
            return queryset.filter(website_url__isnull=True) | queryset.filter(website_url="")
        return queryset


@admin.register(Business)
class BusinessAdmin(PipelineAdmin):
    list_display = ["name", "category", "city", "google_rating", "google_review_count", "website",
                    "research_status", "score", "discovery", "contact_count", "prospect_count"]
    list_filter = ["discovery_campaign", HasWebsiteFilter, "website_profile__status",
                   "website_profile__contact_discovery_status", "country", "city"]
    search_fields = ["name", "domain", "phone", "address", "website_url"]
    readonly_fields = ["created_at", "updated_at", "first_discovered_at", "last_discovered_at", "profile_link",
                       "opening_hours_display"]
    fieldsets = [
        (None, {"fields": ["discovery_campaign", "name", "category", "website_url", "domain", "phone", "profile_link"]}),
        ("Location", {"fields": ["address", "city", "state", "country", "postal_code", "latitude", "longitude"]}),
        ("Google Maps", {"fields": ["google_place_id", "google_maps_url", "google_rating", "google_review_count",
                                    "opening_hours_display", "source", "source_url"], "classes": ["collapse"]}),
        ("Timestamps", {"fields": ["first_discovered_at", "last_discovered_at", "created_at", "updated_at"],
                        "classes": ["collapse"]}),
    ]
    inlines = [BusinessContactInline, ProspectInline, EmailInline, BusinessSourceInline]
    actions = ["research_websites", "find_stakeholders", "verify_emails", "generate_emails"]
    list_select_related = ["website_profile"]

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(
            contact_total=Count("contacts", distinct=True), prospect_total=Count("prospects", distinct=True),
            profile_score=F("website_profile__qualification_score"),
        )

    @admin.display(description="Website")
    def website(self, obj):
        if not obj.website_url:
            return "-"
        return external_link(obj.website_url, Truncator(obj.domain or obj.website_url).chars(30))

    def _profile(self, obj):
        try:
            return obj.website_profile
        except BusinessWebsiteProfile.DoesNotExist:
            return None

    @admin.display(description="Research", ordering="website_profile__status")
    def research_status(self, obj):
        profile = self._profile(obj)
        return badge(profile.status, profile.get_status_display()) if profile else "-"

    @admin.display(description="Score", ordering="profile_score")
    def score(self, obj):
        return obj.profile_score if obj.profile_score is not None else "-"

    @admin.display(description="Contacts search", ordering="website_profile__contact_discovery_status")
    def discovery(self, obj):
        profile = self._profile(obj)
        return profile.get_contact_discovery_status_display() if profile and profile.contact_discovery_status else "-"

    @admin.display(description="Contacts", ordering="contact_total")
    def contact_count(self, obj):
        return changelist_link(BusinessContact, obj.contact_total, business__id__exact=obj.pk)

    @admin.display(description="Prospects", ordering="prospect_total")
    def prospect_count(self, obj):
        return changelist_link(Prospect, obj.prospect_total, business__id__exact=obj.pk)

    @admin.display(description="Website research")
    def profile_link(self, obj):
        profile = self._profile(obj) if obj.pk else None
        if profile is None:
            return "Not researched yet"
        score = f", score {profile.qualification_score}" if profile.qualification_score is not None else ""
        return admin_link(profile, f"{profile.get_status_display()}{score}")

    @admin.display(description="Opening hours")
    def opening_hours_display(self, obj):
        return pretty_json(obj.opening_hours)

    @admin.action(description="Research websites (Module 2)")
    def research_websites(self, request, queryset):
        return run_step("research_websites", ["--retry-failed", *ids_arguments("--business-id", queryset.values_list("pk", flat=True))])

    @admin.action(description="Find decision makers (Module 3)")
    def find_stakeholders(self, request, queryset):
        return run_step("find_stakeholders", ["--redo", *ids_arguments("--business-id", queryset.values_list("pk", flat=True))])

    @admin.action(description="Verify contact emails (Module 4)")
    def verify_emails(self, request, queryset):
        return run_step("verify_emails", ids_arguments("--business-id", queryset.values_list("pk", flat=True)))

    @admin.action(description="Generate outreach emails (Module 5)")
    def generate_emails(self, request, queryset):
        return run_step("generate_emails", ids_arguments("--business-id", queryset.values_list("pk", flat=True)))


@admin.register(BusinessSource)
class BusinessSourceAdmin(PipelineAdmin):
    list_display = ["business", "source", "source_business_id", "discovered_at"]
    list_filter = ["source"]
    search_fields = ["business__name", "source_business_id"]
    readonly_fields = ["business", "source", "source_business_id", "source_url", "discovered_at", "raw_data_display"]
    exclude = ["raw_data"]
    list_select_related = ["business"]

    @admin.display(description="Raw data")
    def raw_data_display(self, obj):
        return pretty_json(obj.raw_data)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


# ---------------------------------------------------------------- Module 2: website research


@admin.register(BusinessWebsiteProfile)
class BusinessWebsiteProfileAdmin(PipelineAdmin):
    list_display = ["business", "status_badge", "qualification_score", "pages_scraped", "contact_discovery_status",
                    "updated_at"]
    list_filter = ["status", "contact_discovery_status"]
    search_fields = ["business__name", "business__domain"]
    list_select_related = ["business"]
    readonly_fields = ["business", "pages_scraped", "created_at", "updated_at", "contact_discovery_at",
                       "reasons_display", "findings_display", "emails_display", "urls_display", "links_display"]
    fieldsets = [
        (None, {"fields": ["business", "status", "qualification_score", "reasons_display", "agent_reasoning"]}),
        ("What the agent read", {"fields": ["pages_scraped", "urls_display", "findings_display", "emails_display",
                                            "links_display"]}),
        ("Contact discovery (Module 3)", {"fields": ["contact_discovery_status", "contact_discovery_at"]}),
        ("Timestamps", {"fields": ["created_at", "updated_at"], "classes": ["collapse"]}),
    ]
    actions = ["research_again", "rediscover_contacts"]

    def has_add_permission(self, request):
        return False

    @admin.display(description="Status", ordering="status")
    def status_badge(self, obj):
        return badge(obj.status, obj.get_status_display())

    @admin.display(description="Qualification reasons")
    def reasons_display(self, obj):
        return format_html("<ul>{}</ul>", format_html_join("", "<li>{}</li>", ((r,) for r in obj.qualification_reasons or []))) \
            if obj.qualification_reasons else "-"

    @admin.display(description="Findings per page")
    def findings_display(self, obj):
        rows = []
        for page in obj.scraped_pages or []:
            findings = format_html_join("", "<li>{}</li>", ((f,) for f in page.get("relevant_findings") or []))
            rows.append(format_html("<p><b>{}</b><br>{}</p><ul>{}</ul>", page.get("url", ""), page.get("summary", ""), findings))
        return format_html_join("", "{}", ((row,) for row in rows)) if rows else "-"

    @admin.display(description="Emails found")
    def emails_display(self, obj):
        return ", ".join(obj.emails or []) or "-"

    @admin.display(description="Pages read")
    def urls_display(self, obj):
        return format_html_join("", "<div>{}</div>", ((external_link(url),) for url in obj.scraped_urls or [])) \
            if obj.scraped_urls else "-"

    @admin.display(description="Links discovered")
    def links_display(self, obj):
        return pretty_json(obj.discovered_links)

    @admin.action(description="Research again (Module 2)")
    def research_again(self, request, queryset):
        return run_step("research_websites", ["--retry-failed", *ids_arguments(
            "--business-id", queryset.values_list("business_id", flat=True))])

    @admin.action(description="Find decision makers again (Module 3)")
    def rediscover_contacts(self, request, queryset):
        return run_step("find_stakeholders", ["--redo", "--min-score", "0", *ids_arguments(
            "--business-id", queryset.values_list("business_id", flat=True))])


# ---------------------------------------------------------------- Module 3: contacts


@admin.register(BusinessContact)
class BusinessContactAdmin(PipelineAdmin):
    list_display = ["name", "business", "job_title", "email", "email_source", "status_badge", "is_primary",
                    "confidence", "candidate_count"]
    list_filter = ["email_status", "is_primary", "role_type", "email_source"]
    search_fields = ["name", "email", "business__name", "job_title"]
    list_select_related = ["business"]
    autocomplete_fields = ["business"]
    readonly_fields = ["created_at", "updated_at", "email_checked_at", "candidates_display", "check_details_display",
                       "source_urls_display"]
    fieldsets = [
        (None, {"fields": ["business", "name", "first_name", "last_name", "job_title", "role_type", "is_primary",
                           "linkedin_url"]}),
        ("Email", {"fields": ["email", "email_source", "email_status", "email_checked_at", "candidates_display",
                              "check_details_display"]}),
        ("How they were found", {"fields": ["confidence", "discovery_reasoning", "source_urls_display"]}),
        ("Raw data", {"fields": ["candidate_emails", "source_urls"], "classes": ["collapse"]}),
        ("Timestamps", {"fields": ["created_at", "updated_at"], "classes": ["collapse"]}),
    ]
    actions = ["verify_emails"]

    def save_model(self, request, obj, form, change):
        if not change and not obj.email_source:
            obj.email_source = "manual"  # kept when contact discovery runs again
        super().save_model(request, obj, form, change)

    @admin.display(description="Email status", ordering="email_status")
    def status_badge(self, obj):
        return badge(obj.email_status, obj.get_email_status_display() if obj.email_status else None)

    @admin.display(description="Candidates")
    def candidate_count(self, obj):
        return len(obj.candidate_emails or [])

    @admin.display(description="Candidate emails")
    def candidates_display(self, obj):
        rows = [(item.get("email", ""), item.get("pattern", ""), item.get("confidence", ""), item.get("check") or "not checked")
                for item in obj.candidate_emails or [] if isinstance(item, dict)]
        if not rows:
            return "-"
        body = format_html_join("", "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>", rows)
        return format_html('<table class="pipeline-table"><tr><th>Email</th><th>Pattern</th><th>Confidence</th>'
                           "<th>Reacher</th></tr>{}</table>", body)

    @admin.display(description="Check details")
    def check_details_display(self, obj):
        return pretty_json(obj.email_check_details)

    @admin.display(description="Sources")
    def source_urls_display(self, obj):
        return format_html_join("", "<div>{}</div>", ((external_link(url),) for url in obj.source_urls or [])) \
            if obj.source_urls else "-"

    @admin.action(description="Verify these contacts' emails (Module 4)")
    def verify_emails(self, request, queryset):
        return run_step("verify_emails", ["--recheck", *ids_arguments("--contact-id", queryset.values_list("pk", flat=True))])


# ---------------------------------------------------------------- Module 4: prospects


@admin.register(Prospect)
class ProspectAdmin(PipelineAdmin):
    list_display = ["email", "business", "contact", "status_badge", "outreach_badge", "do_not_contact",
                    "qualification_score", "email_verified_at", "last_contacted_at"]
    list_filter = ["outreach_status", "email_status", "do_not_contact", "email_verification_provider"]
    search_fields = ["email", "business__name", "contact__name"]
    list_select_related = ["business", "contact"]
    autocomplete_fields = ["business", "contact"]
    readonly_fields = ["email_verified_at", "email_verification_provider", "last_contacted_at", "created_at",
                       "updated_at", "emails_link"]
    fieldsets = [
        (None, {"fields": ["business", "contact", "email", "do_not_contact"]}),
        ("Verification (Module 4)", {"fields": ["email_status", "email_verification_provider", "email_verified_at",
                                                "qualification_score"]}),
        ("Outreach", {"fields": ["outreach_status", "outreach_priority", "outreach_facts", "research_summary",
                                 "last_contacted_at", "emails_link"]}),
        ("Timestamps", {"fields": ["created_at", "updated_at"], "classes": ["collapse"]}),
    ]
    actions = ["generate_emails", "regenerate_emails", "mark_do_not_contact", "clear_do_not_contact"]

    @admin.display(description="Email", ordering="email_status")
    def status_badge(self, obj):
        return badge(obj.email_status, obj.get_email_status_display() if obj.email_status else None)

    @admin.display(description="Outreach", ordering="outreach_status")
    def outreach_badge(self, obj):
        return badge(obj.outreach_status, obj.get_outreach_status_display())

    @admin.display(description="Emails")
    def emails_link(self, obj):
        return changelist_link(Email, f"{obj.emails.count()} email(s)", prospect__id__exact=obj.pk) if obj.pk else "-"

    @admin.action(description="Generate outreach emails (Module 5)")
    def generate_emails(self, request, queryset):
        return run_step("generate_emails", ids_arguments("--prospect-id", queryset.values_list("pk", flat=True)))

    @admin.action(description="Regenerate emails still in review or rejected (Module 5)")
    def regenerate_emails(self, request, queryset):
        return run_step("generate_emails", ["--regenerate", *ids_arguments("--prospect-id", queryset.values_list("pk", flat=True))])

    @admin.action(description="Mark do not contact")
    def mark_do_not_contact(self, request, queryset):
        count = queryset.update(do_not_contact=True)
        self.message_user(request, f"{count} prospect(s) marked do not contact; they will never be emailed.")

    @admin.action(description="Clear do not contact")
    def clear_do_not_contact(self, request, queryset):
        count = queryset.update(do_not_contact=False)
        self.message_user(request, f"{count} prospect(s) can be contacted again.")


# ---------------------------------------------------------------- Module 5: emails and opens


class EmailReviewForm(forms.ModelForm):
    body = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 16, "class": "vLargeTextField"}),
        max_length=review.MAX_BODY,
        help_text="The Axioware footer and the tracking image are added automatically below the body. "
                  "Saving sends the email back to review.",
    )

    class Meta:
        model = Email
        fields = ["subject"]
        widgets = {"subject": forms.TextInput(attrs={"class": "vLargeTextField", "maxlength": review.MAX_SUBJECT})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.fields["body"].initial = review.editable_body(self.instance)

    def clean_subject(self):
        subject = " ".join((self.cleaned_data.get("subject") or "").split())
        if len(subject) > review.MAX_SUBJECT:
            raise forms.ValidationError(f"Keep the subject under {review.MAX_SUBJECT} characters.")
        return subject


class OpenEventInline(ReadOnlyInline):
    model = EmailOpenEvent
    fields = ["opened_at", "user_agent"]
    readonly_fields = fields
    show_change_link = False
    max_num = 0


@admin.register(Email)
class EmailAdmin(PipelineAdmin):
    form = EmailReviewForm
    list_display = ["id", "business", "recipient", "subject_short", "status_badge", "generated_at", "sent_at",
                    "open_count", "edited"]
    list_display_links = ["id", "subject_short"]
    list_filter = ["status", "generation_provider", "sequence_step"]
    search_fields = ["business__name", "recipient", "subject"]
    list_select_related = ["business"]
    actions = ["approve_selected", "reject_selected", "send_selected"]
    inlines = [OpenEventInline]
    change_form_template = "admin/pipeline/email/change_form.html"
    change_list_template = "admin/pipeline/email/change_list.html"
    details = ["status_badge", "recipient", "business_link", "contact_link", "prospect_link", "written_by",
               "generated_at", "reviewed_at", "review_note", "edited_at"]
    delivery = ["sent_at", "sent_from", "message_id", "send_error", "open_count", "first_opened_at", "last_opened_at",
                "tracking_token"]

    def has_add_permission(self, request):
        return False  # emails are written by generate_emails

    def editable(self, obj) -> bool:
        return obj is not None and obj.status in review.EDITABLE

    def get_fieldsets(self, request, obj=None):
        content = ["subject", "body"] if self.editable(obj) else ["subject", "body_display"]
        return [
            ("Content", {"fields": content}),
            ("Review", {"fields": self.details}),
            ("Delivery and opens", {"fields": self.delivery, "classes": ["collapse"]}),
        ]

    def get_readonly_fields(self, request, obj=None):
        fields = [*self.details, *self.delivery, "body_display"]
        return fields if self.editable(obj) else [*fields, "subject"]

    def get_form(self, request, obj=None, change=False, **kwargs):
        if not self.editable(obj):
            kwargs["form"] = forms.ModelForm
        return super().get_form(request, obj, change=change, **kwargs)

    def save_model(self, request, obj, form, change):
        if not form.has_changed() or "body" not in form.cleaned_data:
            return
        original = Email.objects.get(pk=obj.pk)
        try:
            message = review.save_edit(original, form.cleaned_data["subject"], form.cleaned_data["body"])
        except review.ReviewError as error:
            self.message_user(request, str(error), messages.ERROR)
        else:
            self.message_user(request, message)

    def message_user(self, request, message, level=messages.INFO, *args, **kwargs):
        # The edit outcome is reported by save_model; skip Django's generic "changed successfully" notice.
        if "was changed successfully" in str(message):
            return
        super().message_user(request, message, level, *args, **kwargs)

    @admin.display(description="Subject", ordering="subject")
    def subject_short(self, obj):
        return Truncator(obj.subject).chars(70)

    @admin.display(description="Status", ordering="status")
    def status_badge(self, obj):
        return badge(obj.status, obj.get_status_display())

    @admin.display(description="Edited", boolean=True)
    def edited(self, obj):
        return obj.edited_at is not None

    @admin.display(description="Body")
    def body_display(self, obj):
        return format_html('<pre class="pipeline-pre">{}</pre>', review.editable_body(obj))

    @admin.display(description="Business")
    def business_link(self, obj):
        return admin_link(obj.business)

    @admin.display(description="Contact")
    def contact_link(self, obj):
        contact = obj.contact
        title = f" ({contact.job_title})" if contact.job_title else ""
        return admin_link(contact, f"{contact}{title}")

    @admin.display(description="Prospect")
    def prospect_link(self, obj):
        prospect = obj.prospect
        state = "ready" if prospect.is_ready else "NOT ready for outreach"
        return format_html("{} - {}", admin_link(prospect), state)

    @admin.display(description="Written by")
    def written_by(self, obj):
        return f"{obj.generation_provider} {obj.generation_model}" if obj.generation_model else "-"

    # ------------------------------------------------------------ list page

    def changelist_view(self, request, extra_context=None):
        counts = review.status_counts()
        chips = [{"status": status, "label": Email.Status(status).label, "count": counts[status]}
                 for status in review.STATUS_ORDER]
        extra_context = {**(extra_context or {}), "status_chips": chips, "email_total": sum(counts.values()),
                         "open_rate": review.open_rate(counts)}
        return super().changelist_view(request, extra_context=extra_context)

    @admin.action(description="Approve selected emails (in review only)")
    def approve_selected(self, request, queryset):
        done, skipped = review.bulk("approve", list(queryset.values_list("pk", flat=True)))
        extra = f", {skipped} skipped (wrong status or placeholder text)" if skipped else ""
        self.message_user(request, f"{done} email(s) approved{extra}.")

    @admin.action(description="Reject selected emails")
    def reject_selected(self, request, queryset):
        done, skipped = review.bulk("reject", list(queryset.values_list("pk", flat=True)))
        extra = f", {skipped} skipped (wrong status)" if skipped else ""
        self.message_user(request, f"{done} email(s) rejected{extra}.")

    @admin.action(description="Send selected approved emails (opens the send run)")
    def send_selected(self, request, queryset):
        ids = list(queryset.filter(status="approved").values_list("pk", flat=True))
        if not ids:
            self.message_user(request, "None of the selected emails is approved.", messages.WARNING)
            return None
        return run_step("send_emails", ["--send", *ids_arguments("--email-id", ids)])

    # ------------------------------------------------------------ review page

    def get_urls(self):
        actions = [
            path(f"<int:object_id>/{name}/", self.admin_site.admin_view(view), name=f"pipeline_email_{name}")
            for name, view in (("approve", self.approve_view), ("reject", self.reject_view),
                               ("reopen", self.reopen_view), ("regenerate", self.regenerate_view),
                               ("send", self.send_view))
        ]
        return actions + super().get_urls()

    def change_view(self, request, object_id, form_url="", extra_context=None):
        email = Email.objects.select_related("prospect", "business", "contact", "business__website_profile").filter(
            pk=object_id).first()
        if email is not None:
            try:
                profile = email.business.website_profile
            except BusinessWebsiteProfile.DoesNotExist:
                profile = None
            smtp = sending.smtp_config()
            extra_context = {
                **(extra_context or {}),
                "email": email,
                "preview": review.preview_html(email),
                "findings": generation.website_findings(profile.scraped_pages if profile else []),
                "qualification_reasons": (profile.qualification_reasons if profile else None) or [],
                "qualification_score": profile.qualification_score if profile else None,
                "prospect_ready": review.prospect_ready(email),
                "website_link": safe_url(email.business.website_url),
                "has_placeholder": review.has_placeholder(email),
                "smtp_problems": sending.config_problems(smtp),
                "sender": smtp["from_email"],
                "in_review_count": Email.objects.filter(status="in_review").count(),
                "next_review": review.next_in_review(email.pk),
            }
        return super().change_view(request, object_id, form_url, extra_context)

    def _review_action(self, request, object_id, action, advance=False):
        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])
        email = get_object_or_404(Email.objects.select_related("prospect"), pk=object_id)
        if not self.has_change_permission(request, email):
            return HttpResponseRedirect(reverse("admin:index"))
        try:
            message = action(email)
        except review.ReviewError as error:
            self.message_user(request, str(error), messages.ERROR)
            return HttpResponseRedirect(admin_url(email))
        following = review.next_in_review(email.pk) if advance else None
        if following:
            self.message_user(request, f"Email {email.pk}: {message} Next in review:", messages.SUCCESS)
            return HttpResponseRedirect(reverse("admin:pipeline_email_change", args=[following]))
        self.message_user(request, message, messages.SUCCESS)
        return HttpResponseRedirect(admin_url(email))

    def approve_view(self, request, object_id):
        return self._review_action(request, object_id, review.approve, advance=not request.POST.get("stay"))

    def reject_view(self, request, object_id):
        return self._review_action(request, object_id, lambda e: review.reject(e, request.POST.get("note", "")), advance=True)

    def reopen_view(self, request, object_id):
        return self._review_action(request, object_id, review.reopen)

    def regenerate_view(self, request, object_id):
        return self._review_action(request, object_id, review.regenerate)

    def send_view(self, request, object_id):
        return self._review_action(request, object_id, lambda e: review.send(e, request.POST.get("confirm", "")))


@admin.register(EmailOpenEvent)
class EmailOpenEventAdmin(PipelineAdmin):
    list_display = ["email", "opened_at", "user_agent_short"]
    search_fields = ["email__recipient", "user_agent"]
    list_select_related = ["email"]
    date_hierarchy = "opened_at"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    @admin.display(description="User agent")
    def user_agent_short(self, obj):
        return Truncator(obj.user_agent or "").chars(90)


# ---------------------------------------------------------------- pipeline runs


class PipelineRunForm(forms.ModelForm):
    command = forms.ChoiceField(choices=list(jobs.COMMANDS.items()))
    arguments_text = forms.CharField(
        label="Arguments", required=False, widget=forms.TextInput(attrs={"class": "vLargeTextField"}),
        help_text='Exactly as on the command line, e.g. --limit 5 --dry-run. See the options below.',
    )

    class Meta:
        model = PipelineRun
        fields = ["command"]

    def clean(self):
        cleaned = super().clean()
        command = cleaned.get("command")
        if command:
            try:
                cleaned["arguments"] = jobs.build_arguments(command, arguments=cleaned.get("arguments_text") or "")
            except ValueError as error:
                raise forms.ValidationError(str(error)) from error
        return cleaned


@admin.register(PipelineRun)
class PipelineRunAdmin(PipelineAdmin):
    form = PipelineRunForm
    list_display = ["id", "command", "arguments_short", "status_badge", "created_by", "created_at", "duration"]
    list_filter = ["status", "command"]
    search_fields = ["output", "command"]
    change_form_template = "admin/pipeline/pipelinerun/change_form.html"
    readonly = ["command_line", "status_badge", "exit_code", "created_by", "created_at", "started_at",
                "finished_at", "duration", "output_display"]

    def get_fields(self, request, obj=None):
        return ["command", "arguments_text"] if obj is None else self.readonly

    def get_readonly_fields(self, request, obj=None):
        return [] if obj is None else self.readonly

    def get_changeform_initial_data(self, request):
        return {"command": request.GET.get("command", ""), "arguments_text": request.GET.get("arguments", "")}

    def save_model(self, request, obj, form, change):
        if change:
            return
        obj.arguments = form.cleaned_data["arguments"]
        obj.created_by = request.user
        super().save_model(request, obj, form, change)
        transaction.on_commit(lambda: jobs.launch(obj.pk))

    def response_add(self, request, obj, post_url_continue=None):
        self.message_user(request, f"Started: {obj.command_line}", messages.SUCCESS)
        return HttpResponseRedirect(admin_url(obj))

    def changelist_view(self, request, extra_context=None):
        jobs.refresh_stale_runs()
        return super().changelist_view(request, extra_context)

    def add_view(self, request, form_url="", extra_context=None):
        extra_context = {**(extra_context or {}), "commands": [jobs.describe_command(c) for c in jobs.COMMANDS]}
        return super().add_view(request, form_url, extra_context)

    def change_view(self, request, object_id, form_url="", extra_context=None):
        jobs.refresh_stale_runs()
        run = PipelineRun.objects.filter(pk=object_id).first()
        if run is not None:
            extra_context = {**(extra_context or {}), "run": run,
                             "rerun_url": run_form_url(run.command, run.arguments) if run.command in jobs.COMMANDS else None}
        return super().change_view(request, object_id, form_url, extra_context)

    def get_urls(self):
        return [path("<int:object_id>/cancel/", self.admin_site.admin_view(self.cancel_view),
                     name="pipeline_pipelinerun_cancel")] + super().get_urls()

    def cancel_view(self, request, object_id):
        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])
        run = get_object_or_404(PipelineRun, pk=object_id)
        self.message_user(request, jobs.cancel_run(run))
        return HttpResponseRedirect(admin_url(run))

    @admin.display(description="Command")
    def command_line(self, obj):
        return format_html("<code>{}</code>", obj.command_line)

    @admin.display(description="Arguments")
    def arguments_short(self, obj):
        return Truncator(" ".join(obj.arguments)).chars(60) or "-"

    @admin.display(description="Status", ordering="status")
    def status_badge(self, obj):
        return badge(obj.status, obj.get_status_display())

    @admin.display(description="Duration")
    def duration(self, obj):
        if not obj.started_at:
            return "-"
        from django.utils import timezone

        seconds = int(((obj.finished_at or timezone.now()) - obj.started_at).total_seconds())
        return f"{seconds // 3600}h {seconds // 60 % 60}m {seconds % 60}s" if seconds >= 3600 else f"{seconds // 60}m {seconds % 60}s"

    @admin.display(description="Output")
    def output_display(self, obj):
        return format_html('<pre class="pipeline-pre pipeline-output">{}</pre>', obj.output or "(no output yet)")
