"""Endpoints called by GoHighLevel workflows (JSON body). The secret GHL_WEBHOOK_SECRET goes in the Custom Data as
`secret` (GHL's Webhook action cannot set headers); the header `X-Webhook-Secret` or `?secret=` also work.
`contact_id` comes at the top level of GHL's payload.

- POST /api/ghl/email-sent/      customData {"secret": ..., "followup_count": "{{contact.followup_count}}"}
  right after the sending workflow sent the email in the contact's fields. The app marks it sent, sets the fields
  for what comes next and writes the next follow-up for review.
- POST /api/ghl/stop-sequence/   customData {"secret": ..., "reason": "replied"}
  when the contact replied, bounced, unsubscribed, or a person moved the opportunity: no more emails to them.
- POST /api/ghl/resume-sequence/ customData {"secret": ...}
  when a stop was a mistake (an automatic reply taken for a reply): the sequence continues where it stopped.
"""
import hmac
import os

from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from pipeline.services import ghl, outreach


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
        data = request.data if isinstance(request.data, dict) else {}
        supplied = (request.headers.get("X-Webhook-Secret") or request.query_params.get("secret")
                    or str(_first(data, "secret") or ""))
        if not _secret_matches(supplied, expected):
            return Response({"detail": "Wrong or missing secret (Custom Data key `secret`)."},
                            status=status.HTTP_401_UNAUTHORIZED)
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


class ResumeSequenceView(GhlWebhookView):
    @extend_schema(exclude=True)
    def post(self, request):
        return self.handle(request, lambda contact_id, data: outreach.resume_sequence(contact_id))


class StopSequenceView(GhlWebhookView):
    @extend_schema(exclude=True)
    def post(self, request):
        return self.handle(request, lambda contact_id, data: outreach.stop_sequence(
            contact_id, str(_first(data, "reason") or "stopped")))
