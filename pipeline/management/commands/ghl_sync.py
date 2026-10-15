from pipeline.management.base import ServiceCommand
from pipeline.services import ghl_sync


class Command(ServiceCommand):
    help = "Create GoHighLevel contacts and hand approved emails (whose hand-over failed) to GoHighLevel."
    service = ghl_sync
