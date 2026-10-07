from rest_framework import serializers

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


def string_list(value, field):
    if value is None:
        return []
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise serializers.ValidationError(f"{field} must be a list of strings (or a comma-separated string).")
    return [item.strip() for item in value if item.strip()]


class DiscoveryCampaignSerializer(serializers.ModelSerializer):
    business_count = serializers.IntegerField(read_only=True, source="businesses.count")

    class Meta:
        model = DiscoveryCampaign
        fields = ["id", "name", "target_country", "target_locations", "search_terms", "status", "business_count",
                  "created_at", "updated_at", "started_at", "completed_at"]
        read_only_fields = ["status", "created_at", "updated_at", "started_at", "completed_at"]

    def validate_target_locations(self, value):
        return string_list(value, "target_locations")

    def validate_search_terms(self, value):
        return string_list(value, "search_terms")

    def validate_target_country(self, value):
        if value and "," in value:
            raise serializers.ValidationError("Enter one country only.")
        return value or None


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
        read_only_fields = ["email_verification_provider", "email_verified_at", "last_contacted_at", "created_at",
                            "updated_at"]


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
                  "generated_at", "reviewed_at", "review_note", "edited_at", "sent_at", "sent_from", "message_id",
                  "send_error", "open_count", "first_opened_at", "last_opened_at", "tracking_token", "created_at",
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


class RejectSerializer(serializers.Serializer):
    note = serializers.CharField(required=False, allow_blank=True, max_length=2000)


class SendSerializer(serializers.Serializer):
    confirm = serializers.CharField(help_text="The recipient's address, typed again to confirm sending.")


class BulkReviewSerializer(serializers.Serializer):
    action = serializers.ChoiceField(choices=["approve", "reject"])
    ids = serializers.ListField(child=serializers.IntegerField(), allow_empty=False, max_length=1000)


class FetchBusinessesSerializer(serializers.Serializer):
    limit = serializers.IntegerField(min_value=1, default=20)
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
        read_only_fields = ["status", "exit_code", "output", "created_by", "created_at", "started_at", "finished_at"]
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
