from pipeline.management.base import ServiceCommand
from pipeline.services import ghl_sync


class Command(ServiceCommand):
    help = "Create GoHighLevel contacts and hand due emails to GoHighLevel for sending."
    service = ghl_sync
