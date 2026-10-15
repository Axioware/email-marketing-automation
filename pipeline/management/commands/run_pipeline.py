from pipeline.management.base import ServiceCommand
from pipeline.services import full_pipeline


class Command(ServiceCommand):
    help = ("Run the complete pipeline for a campaign: fetch, research, decision makers, verify, write emails. "
            "Never sends; emails wait for review.")
    service = full_pipeline
