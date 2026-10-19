"""Migrations 0023-0025: each campaign's initial-email templates and follow-up steps become email sequences, and prospects
keep their A/B group."""
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

BEFORE = [("pipeline", "0022_email_templates")]
AFTER = [("pipeline", "0025_email_sequences_cleanup")]


class SequenceMigrationTests(TransactionTestCase):
    def migrate(self, target):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(target)
        return executor.loader.project_state(target).apps

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_campaigns_become_sequences(self):
        apps = self.migrate(BEFORE)
        Template = apps.get_model("pipeline", "EmailPrompt")
        Campaign = apps.get_model("pipeline", "DiscoveryCampaign")
        FollowUp = apps.get_model("pipeline", "FollowUp")
        Business = apps.get_model("pipeline", "Business")
        Prospect = apps.get_model("pipeline", "Prospect")
        Contact = apps.get_model("pipeline", "BusinessContact")
        a = Template.objects.create(name="A", kind="initial", subject="s", body="b")
        b = Template.objects.create(name="B", kind="initial", subject="s", body="b")
        f1 = Template.objects.create(name="F1", kind="follow_up", subject="s", body="b")
        split = Campaign.objects.create(name="Split")
        split.initial_email_prompts.set([a, b])
        FollowUp.objects.create(campaign=split, sequence=1, days_after_previous=3, email_prompt=f1)
        FollowUp.objects.create(campaign=split, sequence=2, days_after_previous=7, email_prompt=None)
        single = Campaign.objects.create(name="Single")
        single.initial_email_prompts.set([a])
        Campaign.objects.create(name="Empty")
        business = Business.objects.create(discovery_campaign=split, name="Clinic")
        contact = Contact.objects.create(business=business, name="Ali")
        in_b = Prospect.objects.create(business=business, contact=contact, email="b@x.org", initial_email_prompt=b)
        waiting = Prospect.objects.create(business=business, contact=contact, email="w@x.org")
        other = Business.objects.create(discovery_campaign=single, name="Other")
        in_single = Prospect.objects.create(business=other, contact=Contact.objects.create(business=other, name="Sara"),
                                            email="s@x.org", initial_email_prompt=a)

        apps = self.migrate(AFTER)
        Campaign = apps.get_model("pipeline", "DiscoveryCampaign")
        Sequence = apps.get_model("pipeline", "EmailSequence")
        Prospect = apps.get_model("pipeline", "Prospect")
        flows = {s.name: s for s in Sequence.objects.all()}
        self.assertEqual(set(flows), {"Split - A", "Split - B", "Single"})
        self.assertEqual(set(Campaign.objects.get(name="Split").email_sequences.values_list("name", flat=True)),
                         {"Split - A", "Split - B"})
        self.assertFalse(Campaign.objects.get(name="Empty").email_sequences.exists())
        for name in ("Split - A", "Split - B"):  # each A/B group got the campaign's follow-ups
            self.assertEqual(list(flows[name].follow_ups.order_by("sequence")
                                  .values_list("sequence", "days_after_previous", "email_prompt__name")),
                             [(1, 3, "F1"), (2, 7, None)])
        self.assertEqual(flows["Single"].initial_template.name, "A")
        self.assertFalse(flows["Single"].follow_ups.exists())
        self.assertEqual(Prospect.objects.get(pk=in_b.pk).email_sequence.name, "Split - B")
        self.assertIsNone(Prospect.objects.get(pk=waiting.pk).email_sequence)  # assigned when its email is written
        self.assertEqual(Prospect.objects.get(pk=in_single.pk).email_sequence.name, "Single")
