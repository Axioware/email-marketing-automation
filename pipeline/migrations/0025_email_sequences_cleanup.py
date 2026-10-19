import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    """Follow-ups belong to sequences now; campaigns choose sequences and prospects keep theirs (0023 moved the data)."""

    dependencies = [
        ('pipeline', '0024_email_sequences_data'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='followup',
            options={'ordering': ['email_sequence_id', 'sequence'], 'verbose_name': 'follow-up'},
        ),
        migrations.RemoveField(
            model_name='followup',
            name='campaign',
        ),
        migrations.AlterField(
            model_name='followup',
            name='email_sequence',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='follow_ups', to='pipeline.emailsequence', verbose_name='sequence'),
        ),
        migrations.AddConstraint(
            model_name='followup',
            constraint=models.UniqueConstraint(fields=('email_sequence', 'sequence'), name='uq_follow_ups_email_sequence'),
        ),
        migrations.RemoveField(
            model_name='discoverycampaign',
            name='initial_email_prompts',
        ),
        migrations.RemoveField(
            model_name='prospect',
            name='initial_email_prompt',
        ),
    ]
