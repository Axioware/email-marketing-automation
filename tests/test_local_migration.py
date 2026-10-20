"""Migrations 0027-0029: emails GoHighLevel was holding become Approved (queue them to send) and leads get the pipeline
stage their emails show."""
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

BEFORE = [("pipeline", "0027_local_pipeline_and_sending")]
AFTER = [("pipeline", "0029_email_status_rule")]


class LocalMigrationTests(TransactionTestCase):
    def migrate(self, target):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(target)
        return executor.loader.project_state(target).apps

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_ghl_statuses_and_stages(self):
        apps = self.migrate(BEFORE)
        Campaign = apps.get_model("pipeline", "DiscoveryCampaign")
        Business = apps.get_model("pipeline", "Business")
        Contact = apps.get_model("pipeline", "BusinessContact")
        Prospect = apps.get_model("pipeline", "Prospect")
        Email = apps.get_model("pipeline", "Email")
        business = Business.objects.create(discovery_campaign=Campaign.objects.create(name="C"), name="Clinic")
        contact = Contact.objects.create(business=business, name="Ali")

        def lead(address, *statuses, **fields):
            prospect = Prospect.objects.create(business=business, contact=contact, email=address, **fields)
            for step, status in enumerate(statuses, start=1):
                Email.objects.create(prospect=prospect, business=business, contact=contact, sequence_step=step,
                                     recipient=address, subject="s", body_text="b", body_html="h", status=status,
                                     tracking_token=f"{address}-{step}")
            return prospect

        waiting = lead("a@x.org", "scheduled")
        followed = lead("b@x.org", "sent", "sending")
        replied = lead("c@x.org", "replied")
        stopped = lead("d@x.org", "sent", sequence_stopped_at="2026-10-01T00:00:00Z", sequence_stop_reason="bounced")
        fresh = lead("e@x.org", "in_review")

        apps = self.migrate(AFTER)
        Prospect = apps.get_model("pipeline", "Prospect")
        Email = apps.get_model("pipeline", "Email")
        self.assertEqual(set(Email.objects.values_list("status", flat=True)),
                         {"approved", "sent", "replied", "in_review"})  # scheduled and sending are gone
        stage = lambda prospect: Prospect.objects.get(pk=prospect.pk).pipeline_stage  # noqa: E731
        self.assertEqual([stage(p) for p in (waiting, followed, replied, stopped, fresh)],
                         ["ready_to_send", "initial_email", "replied", "lost", None])
