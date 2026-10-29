from django.db import migrations


def use_name_aware_greeting(apps, schema_editor):
    EmailPrompt = apps.get_model("pipeline", "EmailPrompt")
    for template in EmailPrompt.objects.filter(body__contains="Hi {{first_name|there}},"):
        template.body = template.body.replace("Hi {{first_name|there}},", "{{greeting}}")
        template.save(update_fields=["body"])


class Migration(migrations.Migration):

    dependencies = [
        ("pipeline", "0035_merge_0034_business_research_score_40_0034_mailboxes"),
    ]

    operations = [
        migrations.RunPython(use_name_aware_greeting, migrations.RunPython.noop),
    ]
