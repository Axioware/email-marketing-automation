"""REST API over the whole pipeline. Every request must send the `Auth` header (AUTH_TOKEN in .env).

Long steps (scraping, research, discovery, verification, generation) are started as pipeline runs:
`POST /api/runs/` returns at once, and `GET /api/runs/{id}/` shows progress and output.
"""
from django.db.models import Count
from django.http import HttpResponse
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from pipeline import jobs, review
from pipeline.services import stages
from pipeline.api import serializers as s
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
    Prospect,
    SendingSettings,
    UsageRecord,
)


class DiscoveryCampaignViewSet(viewsets.ModelViewSet):
    queryset = DiscoveryCampaign.objects.all()
    serializer_class = s.DiscoveryCampaignSerializer
    filterset_fields = ["status", "target_country"]
    search_fields = ["name"]
    ordering_fields = ["created_at", "name", "status"]

    @extend_schema(request=s.RunPipelineSerializer, responses={202: s.PipelineRunSerializer})
    @action(detail=True, methods=["post"], url_path="run-pipeline")
    def run_pipeline(self, request, pk=None):
        """Start the full pipeline for this campaign (fetch, research, decision makers, verify, write emails). Never
        sends; the emails wait for review."""
        campaign = self.get_object()
        params = s.RunPipelineSerializer(data=request.data)
        params.is_valid(raise_exception=True)
        options = {"campaign_id": campaign.pk, **{k: v for k, v in params.validated_data.items() if v not in (None, [])}}
        try:
            arguments = jobs.build_arguments("run_pipeline", options)
        except ValueError as error:
            return Response({"detail": str(error)}, status=status.HTTP_400_BAD_REQUEST)
        run = jobs.start_run("run_pipeline", arguments, request.user)
        return Response(s.PipelineRunSerializer(run).data, status=status.HTTP_202_ACCEPTED)

    @extend_schema(request=s.FetchBusinessesSerializer, responses={202: s.PipelineRunSerializer})
    @action(detail=True, methods=["post"], url_path="fetch-businesses")
    def fetch_businesses(self, request, pk=None):
        """Module 1: start a Google Maps fetch for this campaign."""
        campaign = self.get_object()
        params = s.FetchBusinessesSerializer(data=request.data)
        params.is_valid(raise_exception=True)
        options = {"campaign_id": campaign.pk, **{k: v for k, v in params.validated_data.items() if v is not None}}
        run = jobs.start_run("fetch_businesses", jobs.build_arguments("fetch_businesses", options), request.user)
        return Response(s.PipelineRunSerializer(run).data, status=status.HTTP_202_ACCEPTED)


class CampaignPromptViewSet(viewsets.ModelViewSet):
    """Campaign prompts: the research agent's instructions (qualification), shared by the campaigns that choose them."""

    queryset = CampaignPrompt.objects.prefetch_related("campaigns")
    serializer_class = s.CampaignPromptSerializer
    search_fields = ["name", "prompt"]


class EmailSequenceViewSet(viewsets.ModelViewSet):
    """Email sequences: an initial-email template and its follow-ups. Campaigns choose one or more (A/B split)."""

    queryset = EmailSequence.objects.prefetch_related("follow_ups", "campaigns")
    serializer_class = s.EmailSequenceSerializer
    search_fields = ["name"]


class FollowUpViewSet(viewsets.ModelViewSet):
    """A sequence's follow-ups: follow-up 1, 2, ..., each with its template and the days after the previous email."""

    queryset = FollowUp.objects.all()
    serializer_class = s.FollowUpSerializer
    filterset_fields = ["email_sequence"]


def replied_response(mark, *args, **kwargs):
    try:
        return Response({"detail": mark(*args, **kwargs)})
    except review.ReviewError as error:
        return Response({"detail": str(error)}, status=status.HTTP_409_CONFLICT)


class EmailPromptViewSet(viewsets.ModelViewSet):
    queryset = EmailPrompt.objects.all()
    serializer_class = s.EmailPromptSerializer
    filterset_fields = ["kind"]
    search_fields = ["name", "prompt"]


class BusinessViewSet(viewsets.ModelViewSet):
    queryset = Business.objects.select_related("website_profile").all()
    serializer_class = s.BusinessSerializer
    filterset_fields = {
        "discovery_campaign": ["exact"], "city": ["exact", "icontains"], "country": ["exact"],
        "category": ["exact", "icontains"], "website_profile__status": ["exact"],
        "website_profile__qualification_score": ["gte", "lte"], "website_profile__contact_discovery_status": ["exact", "isnull"],
    }
    search_fields = ["name", "category", "city", "address", "domain", "phone", "website_url"]
    ordering_fields = ["id", "name", "google_rating", "google_review_count", "website_profile__qualification_score"]

    @extend_schema(request=None, responses=OpenApiTypes.OBJECT)
    @action(detail=True, methods=["post"], url_path="mark-replied")
    def mark_replied(self, request, pk=None):
        """Someone at the business replied: its latest sent email becomes Replied; that prospect gets no more emails."""
        return replied_response(review.mark_business_replied, self.get_object())


class BusinessSourceViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = BusinessSource.objects.all()
    serializer_class = s.BusinessSourceSerializer
    filterset_fields = ["business", "source"]


class BusinessWebsiteProfileViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, mixins.UpdateModelMixin,
                                    mixins.DestroyModelMixin, viewsets.GenericViewSet):
    queryset = BusinessWebsiteProfile.objects.all()
    serializer_class = s.BusinessWebsiteProfileSerializer
    filterset_fields = {"business": ["exact"], "status": ["exact"], "qualification_score": ["gte", "lte"],
                        "contact_discovery_status": ["exact", "isnull"]}
    ordering_fields = ["qualification_score", "updated_at"]


class BusinessContactViewSet(viewsets.ModelViewSet):
    queryset = BusinessContact.objects.all()
    serializer_class = s.BusinessContactSerializer
    filterset_fields = ["business", "email_status", "is_primary", "role_type", "email_source"]
    search_fields = ["name", "email", "job_title", "business__name"]
    ordering_fields = ["id", "confidence", "business"]


class ProspectViewSet(viewsets.ModelViewSet):
    queryset = Prospect.objects.all()
    serializer_class = s.ProspectSerializer
    filterset_fields = ["business", "contact", "email_status", "verdict", "outreach_status", "email_sequence",
                        "pipeline_stage", "approved_by_person", "do_not_contact"]

    @extend_schema(request=s.MoveStageSerializer, responses=OpenApiTypes.OBJECT)
    @action(detail=True, methods=["post"], url_path="move-stage")
    def move_stage(self, request, pk=None):
        """Move the lead on the pipeline. Replied, Demo Booked, Demo Done, Won, Lost and Quota Exhausted stop its
        sequence; Initial Email or Follow-up resumes a stopped one."""
        params = s.MoveStageSerializer(data=request.data)
        params.is_valid(raise_exception=True)
        prospect = self.get_object()
        try:
            message = stages.move(prospect, params.validated_data["stage"], request.user,
                                  params.validated_data.get("note", ""))
        except stages.StageError as error:
            return Response({"detail": str(error)}, status=status.HTTP_409_CONFLICT)
        prospect.refresh_from_db()
        return Response({"detail": message, "prospect": s.ProspectSerializer(prospect).data})

    @extend_schema(request=None, responses=OpenApiTypes.OBJECT)
    @action(detail=True, methods=["post"], url_path="mark-replied")
    def mark_replied(self, request, pk=None):
        """The prospect replied: its latest sent email becomes Replied and it gets no more emails."""
        prospect = self.get_object()
        return replied_response(review.mark_business_replied, prospect.business, prospect_id=prospect.pk)
    search_fields = ["email", "business__name", "contact__name"]
    ordering_fields = ["id", "qualification_score", "email_verified_at", "last_contacted_at"]


class EmailViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, mixins.UpdateModelMixin,
                   mixins.DestroyModelMixin, viewsets.GenericViewSet):
    """Generated emails. Create them with the generate_emails run; edit and review them here."""

    queryset = Email.objects.select_related("prospect").all()
    serializer_class = s.EmailSerializer
    filterset_fields = ["status", "business", "prospect", "contact", "sequence_step", "email_prompt",
                        "generation_provider"]
    search_fields = ["business__name", "recipient", "subject"]
    ordering_fields = ["id", "generated_at", "sent_at", "open_count"]

    def _act(self, request, method, *args):
        email = self.get_object()
        try:
            message = method(email, *args)
        except review.ReviewError as error:
            return Response({"detail": str(error)}, status=status.HTTP_409_CONFLICT)
        email.refresh_from_db()
        return Response({"detail": message, "email": s.EmailSerializer(email, context={"request": request}).data})

    @extend_schema(request=None, responses={200: OpenApiResponse(description="Approved"), 409: OpenApiResponse(description="Not allowed now")})
    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        """Approve: the email gets its send time. Queue it (emails/queue/) to send it then."""
        return self._act(request, review.approve)

    @extend_schema(request=s.IdsSerializer, responses=OpenApiTypes.OBJECT)
    @action(detail=False, methods=["post"])
    def queue(self, request):
        """Queue approved (or failed) emails for sending; the sender starts and sends each at its time."""
        params = s.IdsSerializer(data=request.data)
        params.is_valid(raise_exception=True)
        emails = list(Email.objects.filter(pk__in=params.validated_data["ids"]))
        message = review.queue(emails)
        return Response({"detail": message, "queued": Email.objects.filter(pk__in=params.validated_data["ids"],
                                                                         status="queued").count()})

    @extend_schema(request=s.IdsSerializer, responses=OpenApiTypes.OBJECT)
    @action(detail=False, methods=["post"])
    def unqueue(self, request):
        """Take queued emails out of the queue (they stay approved, with the same times)."""
        params = s.IdsSerializer(data=request.data)
        params.is_valid(raise_exception=True)
        return Response({"detail": review.unqueue(list(Email.objects.filter(pk__in=params.validated_data["ids"])))})

    @extend_schema(request=s.RejectSerializer)
    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        params = s.RejectSerializer(data=request.data)
        params.is_valid(raise_exception=True)
        return self._act(request, review.reject, params.validated_data.get("note", ""))

    @extend_schema(request=None)
    @action(detail=True, methods=["post"], url_path="mark-replied")
    def mark_replied(self, request, pk=None):
        """This sent email got a reply: it becomes Replied and its business gets no more follow-ups."""
        return self._act(request, review.mark_replied)

    @extend_schema(request=None)
    @action(detail=True, methods=["post"])
    def reopen(self, request, pk=None):
        return self._act(request, review.reopen)

    @extend_schema(request=s.RegenerateSerializer)
    @action(detail=True, methods=["post"])
    def regenerate(self, request, pk=None):
        params = s.RegenerateSerializer(data=request.data)
        params.is_valid(raise_exception=True)
        return self._act(request, review.regenerate, params.validated_data.get("email_prompt"))

    @extend_schema(responses={(200, "text/html"): str})
    @action(detail=True, methods=["get"])
    def preview(self, request, pk=None):
        """The email's HTML as the recipient sees it (viewing it never counts as an open)."""
        response = HttpResponse(review.preview_html(self.get_object()), content_type="text/html; charset=utf-8")
        response["Content-Security-Policy"] = "default-src 'none'; img-src https: data:; style-src 'unsafe-inline'; sandbox"
        return response

    @extend_schema(request=s.BulkReviewSerializer, responses=OpenApiTypes.OBJECT)
    @action(detail=False, methods=["post"])
    def bulk(self, request):
        """Approve or reject many emails at once."""
        params = s.BulkReviewSerializer(data=request.data)
        params.is_valid(raise_exception=True)
        done, skipped = review.bulk(params.validated_data["action"], params.validated_data["ids"])
        return Response({"done": done, "skipped": skipped})

    @extend_schema(responses=OpenApiTypes.OBJECT)
    @action(detail=False, methods=["get"])
    def stats(self, request):
        """Emails per status and the open rate."""
        counts = review.status_counts()
        return Response({"counts": counts, "total": sum(counts.values()), "open_rate": review.open_rate(counts)})


class EmailOpenEventViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = EmailOpenEvent.objects.all()
    serializer_class = s.EmailOpenEventSerializer
    filterset_fields = ["email"]
    ordering_fields = ["opened_at"]


class PipelineRunViewSet(mixins.CreateModelMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin,
                         mixins.DestroyModelMixin, viewsets.GenericViewSet):
    """Start any pipeline command in the background and follow it. Suitable for cron: POST, then poll."""

    queryset = PipelineRun.objects.select_related("created_by").all()
    filterset_fields = ["status", "command"]
    ordering_fields = ["created_at", "finished_at"]

    def get_serializer_class(self):
        return s.PipelineRunListSerializer if self.action == "list" else s.PipelineRunSerializer

    def list(self, request, *args, **kwargs):
        jobs.refresh_stale_runs()
        return super().list(request, *args, **kwargs)

    def retrieve(self, request, *args, **kwargs):
        jobs.refresh_stale_runs()
        return super().retrieve(request, *args, **kwargs)

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        run = serializer.save()
        return Response(s.PipelineRunSerializer(run).data, status=status.HTTP_202_ACCEPTED)

    def destroy(self, request, *args, **kwargs):
        if not self.get_object().is_finished:
            return Response({"detail": "Stop the run before deleting it."}, status=status.HTTP_409_CONFLICT)
        return super().destroy(request, *args, **kwargs)

    @extend_schema(request=None)
    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        run = self.get_object()
        message = jobs.cancel_run(run)
        run.refresh_from_db()
        return Response({"detail": message, "run": s.PipelineRunSerializer(run).data})

    @extend_schema(responses=OpenApiTypes.OBJECT)
    @action(detail=False, methods=["get"])
    def commands(self, request):
        """Every command a run can execute, with its options."""
        return Response([jobs.describe_command(command) for command in jobs.COMMANDS])


class EmailVariablesView(APIView):
    """Every variable an email template can use: {{name}}, or {{name|fallback}} for a value when there is none."""

    @extend_schema(responses=OpenApiTypes.OBJECT)
    def get(self, request):
        from pipeline.services import templates

        return Response([{"name": name, "description": description}
                         for name, description in templates.known_variables().items()])


class UsageViewSet(viewsets.ReadOnlyModelViewSet):
    """AI tokens and Hunter credits per call. Filters: ?from=YYYY-MM-DD&to=YYYY-MM-DD (inclusive), ?action=,
    ?provider=, ?model=, ?business=, ?prospect=, ?run=. GET usage/summary/ gives the totals per action and model."""

    queryset = UsageRecord.objects.all()
    serializer_class = s.UsageRecordSerializer
    filterset_fields = ["action", "provider", "model", "business", "prospect", "run"]
    search_fields = ["business__name", "detail"]
    ordering_fields = ["created_at", "input_tokens", "output_tokens", "hunter_credits"]

    def get_queryset(self):
        from datetime import date, datetime, time, timedelta

        from django.utils import timezone as tz

        queryset = super().get_queryset()
        zone = tz.get_current_timezone()
        for name, lookup, shift in (("from", "created_at__gte", 0), ("to", "created_at__lt", 1)):
            value = self.request.query_params.get(name)
            if value:
                try:
                    day = date.fromisoformat(value)
                except ValueError:
                    continue
                queryset = queryset.filter(**{lookup: datetime.combine(day + timedelta(days=shift), time.min, tzinfo=zone)})
        return queryset

    @extend_schema(responses=OpenApiTypes.OBJECT)
    @action(detail=False, methods=["get"])
    def summary(self, request):
        from django.db.models import Sum

        queryset = self.filter_queryset(self.get_queryset())
        sums = dict(input_tokens=Sum("input_tokens"), output_tokens=Sum("output_tokens"),
                    reasoning_tokens=Sum("reasoning_tokens"), hunter_credits=Sum("hunter_credits"), calls=Count("id"))
        return Response({"totals": queryset.aggregate(**sums),
                         "by_action_and_model": list(queryset.values("action", "provider", "model").annotate(**sums)
                                                     .order_by("action", "model"))})


class PipelineView(APIView):
    """The lead pipeline: how many leads are in each stage (move one with POST prospects/{id}/move-stage/)."""

    @extend_schema(responses=OpenApiTypes.OBJECT)
    def get(self, request):
        stages.expire_finished()
        return Response({"stages": [{"stage": value, "label": label, "leads": count}
                                    for (value, label), count in zip(stages.PipelineStage.choices,
                                                                     stages.counts().values())]})


class SendingSettingsView(APIView):
    """How fast emails go out, for all campaigns together: the daily cap and the gaps between sends (GET; PATCH or
    PUT to change them)."""

    @extend_schema(responses=s.SendingSettingsSerializer)
    def get(self, request):
        return Response(s.SendingSettingsSerializer(SendingSettings.current()).data)

    @extend_schema(request=s.SendingSettingsSerializer, responses=s.SendingSettingsSerializer)
    def patch(self, request):
        return self.save(request, partial=True)

    @extend_schema(request=s.SendingSettingsSerializer, responses=s.SendingSettingsSerializer)
    def put(self, request):
        return self.save(request, partial=False)

    def save(self, request, partial):
        serializer = s.SendingSettingsSerializer(SendingSettings.current(), data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


class StatsView(APIView):
    """Counts across the whole pipeline."""

    @extend_schema(responses=OpenApiTypes.OBJECT)
    def get(self, request):
        def by(queryset, field):
            return dict(queryset.values_list(field).annotate(n=Count("id")).values_list(field, "n"))

        email_counts = review.status_counts()
        return Response({
            "campaigns": by(DiscoveryCampaign.objects, "status"),
            "businesses": Business.objects.count(),
            "website_research": by(BusinessWebsiteProfile.objects, "status"),
            "contact_discovery": by(BusinessWebsiteProfile.objects.exclude(contact_discovery_status=None), "contact_discovery_status"),
            "contacts": BusinessContact.objects.count(),
            "prospects": by(Prospect.objects, "outreach_status"),
            "emails": email_counts,
            "runs": by(PipelineRun.objects, "status"),
        })
