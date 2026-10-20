from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('pipeline', '0028_local_pipeline_data'),
    ]

    operations = [
        migrations.AddConstraint(
            model_name='email',
            constraint=models.CheckConstraint(condition=models.Q(('status__in', ['in_review', 'approved', 'queued', 'rejected', 'sending', 'sent', 'opened', 'failed', 'replied'])), name='ck_emails_status'),
        ),
    ]
