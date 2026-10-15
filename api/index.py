"""Vercel entry point: every request is routed here (vercel.json) and served by the Django app."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "emailautomation.settings")

from django.core.wsgi import get_wsgi_application  # noqa: E402

app = get_wsgi_application()
