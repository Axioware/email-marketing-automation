from pipeline.management.base import ServiceCommand
from pipeline.services import verification


class Command(ServiceCommand):
    help = "Module 4: verify candidate emails with Reacher; deliverable ones become prospects. Sends no email."
    service = verification
