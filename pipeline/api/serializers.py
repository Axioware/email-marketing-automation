from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from pipeline import jobs, review
from pipeline.models import (
    Business,
    CampaignPrompt,
    EmailPrompt,
    EmailSequence,
    FollowUp,
    BusinessContact,
    BusinessSource,
    BusinessWebsiteProfile,
    DiscoveryCampaign,
    Email,
    EmailOpenEvent,
    PipelineRun,
    PipelineStage,
    Prospect,
    SendingSettings,
    check_output_fields,
)


def string_list(value, field):
    if value is None:
        return []
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise serializers.ValidationError(f"{field} must be a list of strings (or a comma-separated string).")
    return [item.strip() for item in value if item.strip()]


class FollowUpSerializer(serializers.ModelSerializer):
    class Meta:
        model = FollowUp
        fields = ["id", "email_sequence", "sequence", "days_after_previous", "email_prompt", "created_at", "updated_at"]
        read_only_fields = ["created_at", "updated_at"]

    def validate_email_prompt(self, value):
        if value is not None and value.kind != "follow_up":
            raise serializers.ValidationError("Choose a follow-up template (kind follow_up).")
        return value


class EmailSequenceSerializer(serializers.ModelSerializer):
    follow_ups = FollowUpSerializer(many=True, read_only=True)

    class Meta:
        model = EmailSequence
        fields = ["id", "name", "initial_template", "follow_ups", "campaigns", "created_at", "updated_at"]
        read_only_fields = ["campaigns", "created_at", "updated_at"]

    def validate_initial_template(self, value):
        if value is not None and value.kind != "initial":
            raise serializers.ValidationError("Choose an initial-email template (kind initial).")
        return value


CAMPAIGN_SETTINGS = ["min_score", "send_window_start", "send_window_end", "send_timezone"]


class DiscoveryCampaignSerializer(serializers.ModelSerializer):
    business_count = serializers.IntegerField(read_only=True, source="businesses.count")

    class Meta:
        model = DiscoveryCampaign
        fields = ["id", "name", "target_country", "target_locations", "search_terms", "status", "lead_source",
                  "campaign_prompt", "email_sequences", *CAMPAIGN_SETTINGS, "business_count",
                  "created_at", "updated_at", "started_at", "completed_at"]
        read_only_fields = ["status", "created_at", "updated_at", "started_at", "completed_at"]

    def validate(self, attrs):
        """The model's rules (send window, time zone, score range)."""
        instance = DiscoveryCampaign(**{field: getattr(self.instance, field) for field in CAMPAIGN_SETTINGS}) \
            if self.instance else DiscoveryCampaign()
        for field in CAMPAIGN_SETTINGS:
            if field in attrs:
                setattr(instance, field, attrs[field])
        try:
            instance.clean()
        except DjangoValidationError as error:
            raise serializers.ValidationError(error.message_dict) from error
        return attrs

    def validate_target_locations(self, value):
        return string_list(value, "target_locations")

    def validate_search_terms(self, value):
        return string_list(value, "search_terms")

    def validate_target_country(self, value):
        if value and "," in value:
            raise serializers.ValidationError("Enter one country only.")
        return value or None


class EmailPromptSerializer(serializers.ModelSerializer):
    """An email template: subject and body with {{variables}} (GET /api/email-variables/ lists them)."""

    class Meta:
        model = EmailPrompt
        fields = ["id", "name", "kind", "subject", "body", "created_at", "updated_at"]
        read_only_fields = ["created_at", "updated_at"]

    def validate(self, attrs):
        from pipeline.services import templates

        known = templates.known_variables()
        kind = attrs.get("kind", getattr(self.instance, "kind", "initial"))
        errors = {}
        for field in ("subject", "body"):
            value = attrs.get(field, getattr(self.instance, field, ""))
            if not (value or "").strip():
                errors[field] = ["Required."]
            elif templates.problems(value, known, kind == "follow_up"):
                errors[field] = templates.problems(value, known, kind == "follow_up")
        if errors:
            raise serializers.ValidationError(errors)
        return attrs


class CampaignPromptSerializer(serializers.ModelSerializer):
    campaigns = serializers.PrimaryKeyRelatedField(many=True, read_only=True)

    class Meta:
        model = CampaignPrompt
        fields = ["id", "name", "prompt", "answer_schema", "output_fields", "campaigns", "created_at", "updated_at"]
        read_only_fields = ["created_at", "updated_at"]
        extra_kwargs = {"answer_schema": {"help_text": "The JSON schema the AI answers in (edit descriptions, add your "
                                                       "own fields); null: the app's fields plus output_fields."},
                        "output_fields": {"help_text": 'Extra facts the agent returns when scoring: [{"name": "team_size", '
                                                       '"type": "integer", "description": "..."}]; types: text, integer, '
                                                       'number, yes/no, text list, integer list.'}}

    def validate_output_fields(self, value):
        try:
            return check_output_fields(value)
        except DjangoValidationError as error:
            raise serializers.ValidationError(error.messages) from error

    def validate(self, attrs):
        """An edited answer_schema decides the output fields; without one they come from output_fields."""
        from pipeline.services.research import SchemaError, check_answer_schema

        if attrs.get("answer_schema") is not None:
            try:
                attrs["answer_schema"], attrs["output_fields"] = check_answer_schema(attrs["answer_schema"])
            except SchemaError as error:
                raise serializers.ValidationError({"answer_schema": str(error)}) from error
        elif "output_fields" in attrs:
            attrs["answer_schema"] = None  # rebuilt from the app's fields and these output fields
        return attrs


class BusinessSerializer(serializers.ModelSerializer):
    qualification_score = serializers.IntegerField(read_only=True, source="website_profile.qualification_score", default=None)
    research_status = serializers.CharField(read_only=True, source="website_profile.status", default=None)

    class Meta:
        model = Business
        fields = "__all__"
        read_only_fields = ["created_at", "updated_at"]


class BusinessSourceSerializer(serializers.ModelSerializer):
    class Meta:
        model = BusinessSource
        fields = "__all__"


class BusinessWebsiteProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = BusinessWebsiteProfile
        fields = "__all__"
        read_only_fields = ["business", "created_at", "updated_at"]


class BusinessContactSerializer(serializers.ModelSerializer):
    class Meta:
        model = BusinessContact
        fields = "__all__"
        read_only_fields = ["created_at", "updated_at", "email_checked_at"]


class ProspectSerializer(serializers.ModelSerializer):
    class Meta:
        model = Prospect
        fields = "__all__"
        read_only_fields = ["email_sequence", "pipeline_stage", "stage_changed_at", "sequence_stopped_at",
                            "email_score", "email_score_reason", "email_scored_at",
                            "sequence_stop_reason",
                            "email_verification_provider", "email_verified_at", "verdict", "verification_note",
                            "verification_details", "last_contacted_at", "created_at", "updated_at"]


class EmailOpenEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = EmailOpenEvent
        fields = ["id", "email", "opened_at", "user_agent"]


class EmailSerializer(serializers.ModelSerializer):
    """`body` is the part a person writes (without the footer). Editing subject/body sends the email back to review."""

    body = serializers.CharField(max_length=review.MAX_BODY, required=False, trim_whitespace=True)
    prospect_ready = serializers.SerializerMethodField()

    class Meta:
        model = Email
        fields = ["id", "prospect", "business", "contact", "sequence_step", "recipient", "subject", "body",
                  "body_text", "body_html", "status", "prospect_ready", "generation_provider", "generation_model",
                  "email_prompt",
                  "generated_at", "reviewed_at", "review_note", "edited_at", "scheduled_at", "queued_at",
                  "send_error", "sent_at", "sent_from", "message_id", "open_count", "first_opened_at", "last_opened_at", "tracking_token", "created_at",
                  "updated_at"]
        read_only_fields = [field for field in fields if field not in ("subject", "body")]
        extra_kwargs = {"subject": {"max_length": review.MAX_SUBJECT, "required": False}}

    def get_prospect_ready(self, obj) -> bool:
        return obj.prospect.is_ready

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data["body"] = review.editable_body(instance)
        return data

    def update(self, instance, validated_data):
        subject = validated_data.get("subject", instance.subject)
        body = validated_data.get("body", review.editable_body(instance))
        try:
            review.save_edit(instance, subject, body)
        except review.ReviewError as error:
            raise serializers.ValidationError({"detail": str(error)}) from error
        instance.refresh_from_db()
        return instance


class RegenerateSerializer(serializers.Serializer):
    email_prompt = serializers.PrimaryKeyRelatedField(queryset=EmailPrompt.objects.all(), required=False, allow_null=True,
                                                      help_text="Default: the prompt the email was written with.")


class RejectSerializer(serializers.Serializer):
    note = serializers.CharField(required=False, allow_blank=True, max_length=2000)


class BulkReviewSerializer(serializers.Serializer):
    action = serializers.ChoiceField(choices=["approve", "reject"])
    ids = serializers.ListField(child=serializers.IntegerField(), allow_empty=False, max_length=1000)


class IdsSerializer(serializers.Serializer):
    ids = serializers.ListField(child=serializers.IntegerField(), allow_empty=False, max_length=1000)


class MoveStageSerializer(serializers.Serializer):
    stage = serializers.ChoiceField(choices=PipelineStage.choices)
    note = serializers.CharField(max_length=255, required=False, allow_blank=True)


class RunPipelineSerializer(serializers.Serializer):
    fetch_limit = serializers.IntegerField(min_value=0, default=20, help_text="New businesses to fetch (0 skips fetching).")
    skip = serializers.ListField(child=serializers.ChoiceField(choices=["fetch", "research", "stakeholders", "verify",
                                                                         "generate", "followups"]), required=False)
    min_score = serializers.IntegerField(min_value=0, max_value=100, required=False)
    email_prompt_id = serializers.IntegerField(required=False)
    source = serializers.ChoiceField(choices=["browser", "scraper_api"], required=False,
                                     help_text="Lead source for fetching (default: the campaign's lead_source).")
    headed = serializers.BooleanField(default=False)


class FetchBusinessesSerializer(serializers.Serializer):
    limit = serializers.IntegerField(min_value=1, default=20)
    source = serializers.ChoiceField(choices=["browser", "scraper_api"], required=False,
                                     help_text="browser (built-in Chromium) or scraper_api (the scraper container); "
                                               "default: the campaign's lead_source.")
    lang = serializers.CharField(max_length=10, required=False, help_text="Scraper container only: Google Maps language.")
    concurrency = serializers.IntegerField(min_value=1, max_value=20, required=False,
                                           help_text="Scraper container only: places opened at once.")
    delay = serializers.FloatField(min_value=0, required=False)
    headed = serializers.BooleanField(default=False)


class PipelineRunSerializer(serializers.ModelSerializer):
    options = serializers.DictField(write_only=True, required=False,
                                    help_text='Command options by name, e.g. {"limit": 5, "dry_run": true, "business_id": [1, 2]}.')
    command_line = serializers.CharField(read_only=True)
    created_by = serializers.StringRelatedField()

    class Meta:
        model = PipelineRun
        fields = ["id", "command", "arguments", "options", "command_line", "status", "exit_code", "output",
                  "created_by", "created_at", "started_at", "finished_at"]
        read_only_fields = ["status", "exit_code", "output", "created_by", "created_at", "started_at",
                            "finished_at"]
        extra_kwargs = {"arguments": {"required": False, "help_text": 'Raw arguments, e.g. ["--limit", "5"].'}}

    def validate_command(self, value):
        if value not in jobs.COMMANDS:
            raise serializers.ValidationError(f"Choose one of: {', '.join(jobs.COMMANDS)}.")
        return value

    def validate(self, attrs):
        arguments = attrs.get("arguments") or []
        if not isinstance(arguments, (list, str)):
            raise serializers.ValidationError({"arguments": "Use a list of strings or one string."})
        try:
            attrs["arguments"] = jobs.build_arguments(attrs["command"], attrs.pop("options", None), arguments)
        except ValueError as error:
            raise serializers.ValidationError({"options": str(error)}) from error
        return attrs

    def create(self, validated_data):
        return jobs.start_run(validated_data["command"], validated_data["arguments"], self.context["request"].user)


class PipelineRunListSerializer(PipelineRunSerializer):
    class Meta(PipelineRunSerializer.Meta):
        fields = [field for field in PipelineRunSerializer.Meta.fields if field != "output"]


class SendingSettingsSerializer(serializers.ModelSerializer):
    """How fast emails go out, for all campaigns together."""

    class Meta:
        model = SendingSettings
        fields = ["daily_cap", "gap_fixed_seconds", "gap_random_min_seconds", "gap_random_max_seconds",
                  "reply_window_days", "contact_provider", "excluded_email_patterns", "email_scoring_enabled",
                  "email_min_score", "email_scoring_prompt", "footer_text", "updated_at"]
        read_only_fields = ["updated_at"]

    def validate(self, attrs):
        settings_row = SendingSettings(**{field: getattr(self.instance, field) for field in
                                          ("daily_cap", "gap_fixed_seconds", "gap_random_min_seconds",
                                           "gap_random_max_seconds", "reply_window_days", "contact_provider",
                                           "excluded_email_patterns", "email_scoring_enabled", "email_min_score",
                                           "email_scoring_prompt", "footer_text")})
        for field, value in attrs.items():
            setattr(settings_row, field, value)
        try:
            settings_row.clean()
        except DjangoValidationError as error:
            raise serializers.ValidationError(error.message_dict) from error
        return attrs
