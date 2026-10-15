"""Vercel Queues subscriber (pyproject.toml, [[tool.vercel.subscribers]]): runs the batches of web runs
(pipeline/tasks.py). Vercel builds it as a private function that only Vercel Queues can invoke."""
import os

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "emailautomation.settings")
django.setup()

from pipeline.tasks import app  # noqa: E402

__all__ = ["app"]
