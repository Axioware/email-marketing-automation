from pipeline.management.base import ServiceCommand
from pipeline.services import generation


class Command(ServiceCommand):
    help = "Module 5: write one outreach email per ready prospect, stored for review. Sends nothing."
    service = generation
