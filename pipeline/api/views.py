"""REST API over the whole pipeline. Every request must send the `Auth` header (AUTH_TOKEN in .env).

Long steps (scraping, research, discovery, verification, generation, bulk sending) are started as pipeline runs:
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
from pipeline.api import serializers as s
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


class DiscoveryCampaignViewSet(viewsets.ModelViewSet):
    queryset = DiscoveryCampaign.objects.all()
    serializer_class = s.DiscoveryCampaignSerializer
    filterset_fields = ["status", "target_country"]
    search_fields = ["name"]
    ordering_fields = ["created_at", "name", "status"]

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


class BusinessViewSet(viewsets.ModelViewSet):
    queryset = Business.objects.select_related("website_profile").all()
    serializer_class = s.BusinessSerializer
    filterset_fields = {
        "discovery_campaign": ["exact"], "city": ["exact", "icontains"], "country": ["exact"],
        "category": ["exact", "icontains"], "website_profile__status": ["exact"],
        "website_profile__qualification_score": ["gte", "lte"], "website_profile__contact_discovery_status": ["exact", "isnull"],
    }
    search_fields = ["name", "domain", "phone", "address", "website_url"]
    ordering_fields = ["id", "name", "google_rating", "google_review_count", "website_profile__qualification_score"]


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
    filterset_fields = ["business", "contact", "email_status", "outreach_status", "do_not_contact"]
    search_fields = ["email", "business__name", "contact__name"]
    ordering_fields = ["id", "qualification_score", "email_verified_at", "last_contacted_at"]


class EmailViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, mixins.UpdateModelMixin,
                   mixins.DestroyModelMixin, viewsets.GenericViewSet):
    """Generated emails. Create them with the generate_emails run; edit, review and send them here."""

    queryset = Email.objects.select_related("prospect").all()
    serializer_class = s.EmailSerializer
    filterset_fields = ["status", "business", "prospect", "contact", "sequence_step", "generation_provider"]
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
        return self._act(request, review.approve)

    @extend_schema(request=s.RejectSerializer)
    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        params = s.RejectSerializer(data=request.data)
        params.is_valid(raise_exception=True)
        return self._act(request, review.reject, params.validated_data.get("note", ""))

    @extend_schema(request=None)
    @action(detail=True, methods=["post"])
    def reopen(self, request, pk=None):
        return self._act(request, review.reopen)

    @extend_schema(request=None)
    @action(detail=True, methods=["post"])
    def regenerate(self, request, pk=None):
        return self._act(request, review.regenerate)

    @extend_schema(request=s.SendSerializer)
    @action(detail=True, methods=["post"])
    def send(self, request, pk=None):
        """Send one approved email now. `confirm` must repeat the recipient's address."""
        params = s.SendSerializer(data=request.data)
        params.is_valid(raise_exception=True)
        return self._act(request, review.send, params.validated_data["confirm"])

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
            "open_rate": review.open_rate(email_counts),
            "runs": by(PipelineRun.objects, "status"),
        })
