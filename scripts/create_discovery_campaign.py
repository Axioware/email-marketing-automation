import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import MetaData, Table, create_engine, insert
from sqlalchemy.exc import SQLAlchemyError


def parse_comma_separated_values(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


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


def main() -> int:
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL is not set in the environment or project .env file.")
        return 1

    print("Create discovery campaign")
    name = prompt_for_campaign_name()
    target_country = prompt_for_country()
    target_locations = parse_comma_separated_values(
        input("Target locations (comma-separated): ")
    )
    search_terms = parse_comma_separated_values(input("Search terms (comma-separated): "))

    engine = create_engine(database_url, pool_pre_ping=True)
    campaigns = Table("discovery_campaigns", MetaData(), autoload_with=engine)

    try:
        with engine.begin() as connection:
            result = connection.execute(
                insert(campaigns)
                .values(
                    name=name,
                    target_country=target_country,
                    target_locations=target_locations,
                    search_terms=search_terms,
                )
                .returning(
                    campaigns.c.id,
                    campaigns.c.status,
                    campaigns.c.created_at,
                    campaigns.c.updated_at,
                )
            )
            campaign = result.one()
    except SQLAlchemyError as error:
        print(f"Could not create campaign ({type(error).__name__}).")
        print("Check DATABASE_URL and run `alembic upgrade head` first.")
        return 1
    finally:
        engine.dispose()

    print(f"Campaign created: id={campaign.id}, status={campaign.status}")
    print(f"Created at: {campaign.created_at.isoformat()}")
    print(f"Updated at: {campaign.updated_at.isoformat()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())