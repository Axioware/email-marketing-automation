from django.db import migrations, models


def unique_name(Sequence, base):
    base = base[:190]
    name, number = base, 2
    while Sequence.objects.filter(name=name).exists():
        name, number = f"{base} ({number})", number + 1
    return name


def to_sequences(apps, schema_editor):
    """Each campaign's setup becomes sequences: one per initial-email template (its old A/B groups), each with a copy
    of the campaign's follow-up steps. Prospects keep their A/B group as their sequence."""
    Campaign = apps.get_model("pipeline", "DiscoveryCampaign")
    Sequence = apps.get_model("pipeline", "EmailSequence")
    FollowUp = apps.get_model("pipeline", "FollowUp")
    Prospect = apps.get_model("pipeline", "Prospect")
    for campaign in Campaign.objects.order_by("id"):
        steps = list(FollowUp.objects.filter(campaign_id=campaign.pk).order_by("sequence"))
        initials = list(campaign.initial_email_prompts.order_by("name"))
        if not initials and not steps:
            continue
        prospects = Prospect.objects.filter(business__discovery_campaign_id=campaign.pk)
        for index, template in enumerate(initials or [None]):
            base = campaign.name if len(initials) <= 1 else f"{campaign.name} - {template.name}"
            sequence = Sequence.objects.create(name=unique_name(Sequence, base), initial_template=template)
            campaign.email_sequences.add(sequence)
            for step in steps:
                if index == 0:
                    step.email_sequence = sequence
                    step.save(update_fields=["email_sequence"])
                else:
                    FollowUp.objects.create(campaign_id=campaign.pk, email_sequence=sequence, sequence=step.sequence,
                                            days_after_previous=step.days_after_previous,
                                            email_prompt_id=step.email_prompt_id)
            if len(initials) <= 1:  # one flow: every prospect that started it
                prospects.filter(models.Q(initial_email_prompt__isnull=False) | models.Q(emails__isnull=False)) \
                    .distinct().update(email_sequence=sequence)
            else:
                prospects.filter(initial_email_prompt=template).update(email_sequence=sequence)


class Migration(migrations.Migration):

    dependencies = [
        ('pipeline', '0023_email_sequences'),
    ]

    operations = [
        migrations.RunPython(to_sequences, migrations.RunPython.noop),
    ]
