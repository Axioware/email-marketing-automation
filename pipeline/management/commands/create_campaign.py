from django.core.management.base import BaseCommand, CommandError

from pipeline.models import DiscoveryCampaign
from pipeline.services.console import interactive
from pipeline.services.maps import parse_comma_separated_values


def prompt_for_campaign_name() -> str:
    while True:
        name = input("Campaign name: ").strip()
        if name:
            return name
        print("Campaign name is required.")


def prompt_for_country() -> str | None:
    while True:
        country = input("Target country (single country, optional): ").strip()
        if "," not in country:
            return country or None
        print("Enter one country only; do not separate countries with commas.")


class Command(BaseCommand):
    help = "Module 1: create a discovery campaign (asks for anything not given as an option)."

    def add_arguments(self, parser):
        parser.add_argument("--name", help="Campaign name.")
        parser.add_argument("--country", help="Target country (one country).")
        parser.add_argument("--locations", help="Target locations, comma-separated.")
        parser.add_argument("--search-terms", help="Search terms, comma-separated.")

    def handle(self, *args, **options):
        ask = interactive() and options["name"] is None
        if ask:
            print("Create discovery campaign")
        name = (options["name"] or "").strip() or (prompt_for_campaign_name() if ask else "")
        if not name:
            raise CommandError("--name is required")
        country = options["country"] if options["country"] is not None else (prompt_for_country() if ask else None)
        country = (country or "").strip() or None
        if country and "," in country:
            raise CommandError("Enter one country only; do not separate countries with commas.")
        locations = options["locations"]
        if locations is None and ask:
            locations = input("Target locations (comma-separated): ")
        search_terms = options["search_terms"]
        if search_terms is None and ask:
            search_terms = input("Search terms (comma-separated): ")

        campaign = DiscoveryCampaign.objects.create(
            name=name,
            target_country=country,
            target_locations=parse_comma_separated_values(locations),
            search_terms=parse_comma_separated_values(search_terms),
        )
        print(f"Campaign created: id={campaign.id}, status={campaign.status}")
        print(f"Created at: {campaign.created_at.isoformat()}")
