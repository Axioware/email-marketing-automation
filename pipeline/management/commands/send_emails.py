from pipeline.management.base import ServiceCommand
from pipeline.services import sending


class Command(ServiceCommand):
    help = "Send approved emails via SMTP. Previews only unless --send is given."
    service = sending
