"""Endpoints called by GoHighLevel workflows and by cron (Vercel Cron, or any scheduler).

GoHighLevel webhooks (header `X-Webhook-Secret: <GHL_WEBHOOK_SECRET>`, JSON body):
- POST /api/ghl/email-sent/      {"contact_id": "{{contact.id}}", "followup_count": "{{contact.followup_count}}"}
  after the sending workflow sent the email in the contact's fields.
- POST /api/ghl/stop-sequence/   {"contact_id": "{{contact.id}}", "reason": "replied"}
  when the contact replied or a person moved the opportunity: no more emails to this prospect.

Cron (header `Authorization: Bearer <CRON_SECRET>`, as Vercel Cron sends it, or the API's `Auth` header):
- GET /api/cron/release/     every minute: create GHL contacts, hand due emails to GHL.
- GET /api/cron/followups/   every 10 minutes: write the next follow-ups for review (a few per call).
"""
import hmac
import os
import time

from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from pipeline.api.security import auth_header_valid
from pipeline.services import followups, generation, ghl, outreach

FOLLOWUP_TIME_BUDGET = 45  # seconds; stop starting new follow-ups after this, to stay inside a serverless time limit


def _secret_matches(supplied: str, expected: str) -> bool:
    return bool(expected) and hmac.compare_digest((supplied or "").encode(), expected.encode())


def _first(data, *keys):
    """The first non-empty value among `keys`, at the top level or inside customData (GHL's custom-data block)."""
    sources = [data] + [data.get(name) for name in ("customData", "custom_data") if isinstance(data.get(name), dict)]
    for source in sources:
        for key in keys:
            value = source.get(key)
            if value not in (None, ""):
                return value
    return None


class GhlWebhookView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def check_secret(self, request):
        expected = os.environ.get("GHL_WEBHOOK_SECRET", "").strip()
        if not expected:
            return Response({"detail": "Webhooks are disabled: set GHL_WEBHOOK_SECRET."},
                            status=status.HTTP_503_SERVICE_UNAVAILABLE)
        if not _secret_matches(request.headers.get("X-Webhook-Secret", ""), expected):
            return Response({"detail": "Wrong or missing X-Webhook-Secret header."}, status=status.HTTP_401_UNAUTHORIZED)
        return None

    def contact_id(self, request):
        data = request.data if isinstance(request.data, dict) else {}
        return data, _first(data, "contact_id", "contactId")

    def handle(self, request, action):
        refused = self.check_secret(request)
        if refused is not None:
            return refused
        data, contact_id = self.contact_id(request)
        if not contact_id:
            return Response({"detail": "contact_id is required."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            return Response(action(str(contact_id), data))
        except outreach.OutreachError as error:
            code = status.HTTP_404_NOT_FOUND if "No prospect" in str(error) else status.HTTP_409_CONFLICT
            return Response({"detail": str(error)}, status=code)
        except ghl.GhlError as error:  # GHL retries the webhook; the call is safe to repeat
            return Response({"detail": str(error)}, status=status.HTTP_502_BAD_GATEWAY)


class EmailSentView(GhlWebhookView):
    @extend_schema(exclude=True)
    def post(self, request):
        return self.handle(request, lambda contact_id, data: outreach.record_sent(
            contact_id, _first(data, "followup_count", "follow_up_count", "Follow-up Count")))


class StopSequenceView(GhlWebhookView):
    @extend_schema(exclude=True)
    def post(self, request):
        return self.handle(request, lambda contact_id, data: outreach.stop_sequence(
            contact_id, str(_first(data, "reason") or "stopped")))


class CronView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def allowed(self, request) -> bool:
        bearer = request.headers.get("Authorization", "")
        cron_secret = os.environ.get("CRON_SECRET", "").strip()
        return (bearer.startswith("Bearer ") and _secret_matches(bearer[7:], cron_secret)) or auth_header_valid(request)

    def refused(self):
        return Response({"detail": "Send Authorization: Bearer <CRON_SECRET> (or the Auth header)."},
                        status=status.HTTP_401_UNAUTHORIZED)


class ReleaseCronView(CronView):
    @extend_schema(exclude=True)
    def get(self, request):
        if not self.allowed(request):
            return self.refused()
        if not ghl.configured():
            return Response({"detail": "GoHighLevel is not set up; nothing to do."})
        try:
            contacts = outreach.sync_contacts()
            released = outreach.release_due()
        except ghl.GhlError as error:
            return Response({"detail": str(error)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response({"contacts": contacts, "emails": released, "stuck": outreach.stuck_emails().count()})


class FollowupsCronView(CronView):
    @extend_schema(exclude=True)
    def get(self, request):
        if not self.allowed(request):
            return self.refused()
        llm = generation.make_llm_client()
        if llm is None:
            return Response({"detail": "No LLM key (GROQ_API_KEY or OPENAI_API_KEY)."},
                            status=status.HTTP_503_SERVICE_UNAVAILABLE)
        try:
            limit = max(1, min(int(request.query_params.get("limit", 5)), 20))
        except ValueError:
            limit = 5
        started = time.monotonic()
        result = {"saved": 0, "skipped": 0, "failed": 0, "left": 0}
        due = followups.find_due({"limit": None})
        cache: dict = {}
        for index, item in enumerate(due):
            if index >= limit or time.monotonic() - started > FOLLOWUP_TIME_BUDGET:
                result["left"] = len(due) - index
                break
            try:
                outcome, _ = followups.write_followup(item, llm, cache=cache)
            except Exception:  # noqa: BLE001 - one bad follow-up must not stop the others
                result["failed"] += 1
                continue
            result["saved" if outcome == "saved" else "skipped"] += 1
        return Response(result)
