from pipeline.management.base import ServiceCommand
from pipeline.services import research


class Command(ServiceCommand):
    help = "Module 2: research business websites with an LLM agent and score them."
    service = research
