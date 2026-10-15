from pipeline.management.base import ServiceCommand
from pipeline.services import followups


class Command(ServiceCommand):
    help = "Write the follow-ups that are due (campaign follow-up schedule), saved for review. Sends nothing."
    service = followups
