import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('pipeline', '0022_email_templates'),
    ]

    operations = [
        migrations.CreateModel(
            name='EmailSequence',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=200, unique=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('initial_template', models.ForeignKey(blank=True, help_text='The template of the first email.', limit_choices_to={'kind': 'initial'}, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='initial_for_sequences', to='pipeline.emailprompt', verbose_name='initial-email template')),
            ],
            options={
                'verbose_name': 'email sequence',
                'db_table': 'email_sequences',
                'ordering': ['name'],
            },
        ),
        migrations.AddField(
            model_name='discoverycampaign',
            name='email_sequences',
            field=models.ManyToManyField(blank=True, help_text='The email flows (initial email and follow-ups). With several, prospects are split equally between them (A/B test) and each keeps its sequence to the end.', related_name='campaigns', to='pipeline.emailsequence', verbose_name='email sequences'),
        ),
        migrations.AddField(
            model_name='prospect',
            name='email_sequence',
            field=models.ForeignKey(blank=True, help_text='The sequence this prospect gets (A/B group), set when its first email is written; its follow-ups come from it.', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='prospects', to='pipeline.emailsequence', verbose_name='email sequence'),
        ),
        migrations.AddField(
            model_name='followup',
            name='email_sequence',
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.CASCADE, related_name='follow_ups', to='pipeline.emailsequence', verbose_name='sequence'),
        ),
        migrations.RemoveConstraint(
            model_name='followup',
            name='uq_follow_ups_campaign_sequence',
        ),
    ]
