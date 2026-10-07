"""The `Auth` header every API request must carry, matching AUTH_TOKEN in .env.

The header is checked before anything else on every endpoint. A valid header is enough on its own (suitable for
cron jobs and scripts); a user token or admin session may be sent as well so runs are attributed to that user.
Without AUTH_TOKEN set, the API refuses every request.
"""
import hmac

from django.conf import settings
from drf_spectacular.extensions import OpenApiAuthenticationExtension
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import NotAuthenticated
from rest_framework.permissions import BasePermission

HEADER = "Auth"


def auth_header_valid(request) -> bool:
    expected = settings.AUTH_TOKEN
    supplied = request.headers.get(HEADER, "")
    return bool(expected) and hmac.compare_digest(supplied.encode(), expected.encode())


class AuthHeaderAuthentication(BaseAuthentication):
    """Identifies no user; it only makes refused requests answer 401 with `WWW-Authenticate: Auth`."""

    def authenticate(self, request):
        return None

    def authenticate_header(self, request):
        return HEADER


class HasAuthHeader(BasePermission):
    """Refuses (401, even for logged-in users) any request without the right `Auth` header."""

    def has_permission(self, request, view):
        if not settings.AUTH_TOKEN:
            raise NotAuthenticated("The API is disabled: set AUTH_TOKEN in .env.")
        if not auth_header_valid(request):
            raise NotAuthenticated(f"Send the API key in the '{HEADER}' header.")
        return True


class AuthHeaderScheme(OpenApiAuthenticationExtension):
    """Shows the header in the API docs, so "Authorize" there sends it with every request."""

    target_class = "pipeline.api.security.AuthHeaderAuthentication"
    name = "AuthHeader"

    def get_security_definition(self, auto_schema):
        return {"type": "apiKey", "in": "header", "name": HEADER,
                "description": "The AUTH_TOKEN value from .env."}
