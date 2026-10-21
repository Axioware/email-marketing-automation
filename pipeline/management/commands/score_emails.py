from pipeline.management.base import ServiceCommand
from pipeline.services import email_scoring


class Command(ServiceCommand):
    help = ("Score verified addresses with the AI (Settings: prompt, minimum, excluded patterns); a good score makes "
            "them ready to email. Sends nothing.")
    service = email_scoring
