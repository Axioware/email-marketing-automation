from django.db import migrations

SENT = ("sent", "opened")


def to_local(apps, schema_editor):
    """GoHighLevel's statuses become local ones, and leads get the pipeline stage their emails show."""
    Email = apps.get_model("pipeline", "Email")
    Prospect = apps.get_model("pipeline", "Prospect")
    # In GoHighLevel (scheduled), or written into it (sending): approved here, with the same send time; queue them in
    # the Outbox to send them from this machine.
    Email.objects.filter(status__in=("scheduled", "sending")).update(status="approved")
    for prospect in Prospect.objects.filter(emails__isnull=False).distinct():
        emails = list(Email.objects.filter(prospect=prospect).order_by("sequence_step"))
        reason = (prospect.sequence_stop_reason or "").lower()
        if any(email.status == "replied" for email in emails) or reason.startswith("repl"):
            stage = "replied"
        elif prospect.sequence_stopped_at is not None:
            stage = "lost"
        elif any(email.status in SENT and email.sequence_step > 1 for email in emails):
            stage = "follow_up"
        elif any(email.status in SENT for email in emails):
            stage = "initial_email"
        elif any(email.status == "approved" for email in emails):
            stage = "ready_to_send"
        else:
            continue  # New Lead
        Prospect.objects.filter(pk=prospect.pk).update(pipeline_stage=stage)


class Migration(migrations.Migration):

    dependencies = [
        ('pipeline', '0027_local_pipeline_and_sending'),
    ]

    operations = [
        migrations.RunPython(to_local, migrations.RunPython.noop),
    ]
