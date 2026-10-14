import re

from django.db import migrations
from django.db.models import Q

PLACE_ID_PATTERN = re.compile(r"ChIJ[A-Za-z0-9_-]{10,}")  # as pipeline.services.maps.PLACE_ID_PATTERN


def backfill_place_ids(apps, schema_editor):
    """The built-in browser's place-id pattern needed a word boundary that Maps links never have ("!19sChIJ..."), so
    the businesses it found have no google_place_id although their google_maps_url holds it. Fill it in from the
    link, skipping (and listing) any place another business already has, so the unique rule holds."""
    Business = apps.get_model("pipeline", "Business")
    BusinessSource = apps.get_model("pipeline", "BusinessSource")
    taken = set(Business.objects.exclude(google_place_id__isnull=True).exclude(google_place_id="")
                .values_list("google_place_id", flat=True))
    skipped = []
    for business in Business.objects.filter(Q(google_place_id__isnull=True) | Q(google_place_id="")).order_by("id"):
        match = PLACE_ID_PATTERN.search(business.google_maps_url or "")
        if not match:
            continue
        place_id = match.group(0)
        if place_id in taken:
            skipped.append(business.pk)
            continue
        taken.add(place_id)
        Business.objects.filter(pk=business.pk).update(google_place_id=place_id)
        BusinessSource.objects.filter(business_id=business.pk, source_business_id__isnull=True).update(
            source_business_id=place_id)
    if skipped:
        print(f"\n  Businesses {skipped} share a Google place with another business; their place id was left empty.")


class Migration(migrations.Migration):

    dependencies = [
        ("pipeline", "0012_remove_smtp_sending"),
    ]

    operations = [
        migrations.RunPython(backfill_place_ids, migrations.RunPython.noop),
    ]
