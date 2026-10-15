from pipeline.management.base import ServiceCommand
from pipeline.services import stakeholders


class Command(ServiceCommand):
    help = "Module 3: find decision makers of qualified businesses and their likely emails."
    service = stakeholders
