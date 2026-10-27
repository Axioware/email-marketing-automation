from django.db import migrations, models


def lower_existing_70_thresholds(apps, schema_editor):
    Campaign = apps.get_model("pipeline", "DiscoveryCampaign")
    Campaign.objects.filter(min_score=70).update(min_score=40)


class Migration(migrations.Migration):

    dependencies = [
        ("pipeline", "0033_usage_records"),
    ]

    operations = [
        migrations.RunPython(lower_existing_70_thresholds, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="discoverycampaign",
            name="min_score",
            field=models.PositiveSmallIntegerField(
                db_default=40,
                default=40,
                help_text="Businesses scoring at least this go on to finding decision makers; lower ones wait for a person to approve or reject them.",
            ),
        ),
    ]
