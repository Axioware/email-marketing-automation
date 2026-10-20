from pipeline.management.base import ServiceCommand
from pipeline.services import sending


class Command(ServiceCommand):
    help = "Send the queued emails from your mailbox (SMTP), each at its send time; stops when the queue is empty."
    service = sending
