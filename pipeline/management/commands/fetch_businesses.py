from pipeline.management.base import ServiceCommand
from pipeline.services import maps


class Command(ServiceCommand):
    help = "Module 1: fetch Google Maps businesses for a discovery campaign."
    service = maps
