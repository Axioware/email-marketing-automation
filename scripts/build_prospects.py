"""Check every candidate email of every contact with Reacher; the verified ones become `prospects`.

For each contact the addresses are the contact's own `email` followed by `candidate_emails` (a publicly found
email is not in that list, but it is the best prospect). Only addresses Reacher confirms as deliverable are stored
in `prospects`. Every address's outcome (deliverable, undeliverable, risky, unknown) is also recorded on the contact
in `business_contacts.candidate_emails`, so reruns skip addresses already checked and nothing is lost.

If a stored prospect is rechecked and is no longer deliverable, its row is updated so it cannot stay "ready".

This script owns only the verification columns of `prospects` (`email_status`, `email_verification_provider`,
`email_verified_at`, `qualification_score` and the verification-derived `outreach_status`). Columns that later
stages fill (`outreach_priority`, `outreach_facts`, `research_summary`, `do_not_contact`, `last_contacted_at`)
are never overwritten. No email is ever sent.
"""
import argparse
import csv
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import MetaData, Table, and_, case, create_engine, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import NoSuchTableError, SQLAlchemyError

sys.path.insert(0, str(Path(__file__).resolve().parent))
from verify_contact_emails import (  # noqa: E402
    MAX_CONSECUTIVE_NO_SMTP,
    ReacherUnavailable,
    add_reacher_arguments,
    check_address,
    prepare_reacher,
)

PROVIDER = "reacher"
CONSTRAINT = "uq_prospects_contact_email"
# What verification alone says about outreach. Later stages may move a prospect on (e.g. to "contacted"); the
# upsert only ever rewrites these values and never touches a prospect that has moved on or is do_not_contact.
OUTREACH_STATUS = {"deliverable": "ready", "risky": "needs_review", "unknown": "needs_review", "undeliverable": "rejected"}
MANAGED_OUTREACH_STATUSES = ("pending", "ready", "needs_review", "rejected")
CONCLUSIVE_VERDICTS = {"safe", "invalid", "risky"}  # Reacher verdicts that stay as recorded; unknown is checked again


def candidate_addresses(contact: dict, max_candidates: int = 0) -> list[str]:
    """The contact's own email first, then candidate_emails; de-duplicated, order kept (0 = no cap)."""
    entries = [contact.get("email")] + [item.get("email") for item in contact.get("candidate_emails") or []]
    seen, ordered = set(), []
    for entry in entries:
        address = entry.strip() if isinstance(entry, str) else ""
        if "@" not in address or address.casefold() in seen:
            continue
        seen.add(address.casefold())
        ordered.append(address)
    return ordered[:max_candidates] if max_candidates else ordered


def domain_shortcut(outcome: dict) -> tuple[str, str] | None:
    """(status, reason) when this outcome means every other address on the same domain would get the same answer."""
    summary = outcome["summary"]
    if summary is None:
        return "unknown", "the mail server did not answer"
    if summary.get("is_catch_all"):
        return "risky", "the domain accepts mail for any address (catch-all)"
    if summary.get("can_connect_smtp") is False:
        return "unknown", "Reacher could not connect to the mail server"
    if outcome["status"] == "unknown" and not outcome["retryable"]:
        return "unknown", "the mail server refused the checks"
    return None


def record_check(contact: dict, address: str, outcome: dict, when: datetime) -> dict:
    """Merge one outcome into the contact's candidate_emails (in place) and return the columns to update.

    Every checked address keeps Reacher's verdict (`check`) and time. If the address is the contact's own email,
    the contact's email_status/email_checked_at are updated too (never `email` itself).
    """
    stamp = when.isoformat()
    for item in contact.get("candidate_emails") or []:
        if isinstance(item, dict) and str(item.get("email", "")).strip().casefold() == address.casefold():
            item["check"] = outcome["verdict"]
            item["checked_at"] = stamp
    values = {"candidate_emails": contact.get("candidate_emails") or []}
    if address.casefold() == (contact.get("email") or "").strip().casefold():
        values.update(email_status=outcome["status"], email_checked_at=when)
    return values


def already_decided(contact: dict, address: str, prospect_status: str | None) -> bool:
    """True when this address needs no new check: it is a stored deliverable prospect, or was already found
    undeliverable/risky. A deliverable verdict is only trusted through the prospects table."""
    if prospect_status == "deliverable":
        return True
    key = address.casefold()
    for item in contact.get("candidate_emails") or []:
        if isinstance(item, dict) and str(item.get("email", "")).strip().casefold() == key:
            if item.get("check") in CONCLUSIVE_VERDICTS and item.get("check") != "safe":
                return True
    return key == (contact.get("email") or "").strip().casefold() and contact.get("email_status") in {"undeliverable", "risky"}


def prospect_values(contact: dict, address: str, status: str, now: datetime) -> dict:
    return {
        "business_id": contact["business_id"],
        "contact_id": contact["id"],
        "email": address,
        "email_status": status,
        "email_verification_provider": PROVIDER,
        "email_verified_at": now,
        "qualification_score": contact["qualification_score"],
        "outreach_status": OUTREACH_STATUS[status],
    }


def upsert_prospect(connection, prospects: Table, values: dict) -> None:
    statement = pg_insert(prospects).values(**values)
    movable = and_(prospects.c.outreach_status.in_(MANAGED_OUTREACH_STATUSES), prospects.c.do_not_contact.is_(False))
    connection.execute(
        statement.on_conflict_do_update(
            constraint=CONSTRAINT,
            set_={
                "email_status": statement.excluded.email_status,
                "email_verification_provider": statement.excluded.email_verification_provider,
                "email_verified_at": statement.excluded.email_verified_at,
                "qualification_score": statement.excluded.qualification_score,
                "outreach_status": case((movable, statement.excluded.outreach_status), else_=prospects.c.outreach_status),
            },
        )
    )


def update_existing_prospect(connection, prospects: Table, contact_id: int, address: str, status: str, now: datetime) -> None:
    """A stored prospect was rechecked and is no longer deliverable: reflect it, without touching later-stage columns."""
    movable = and_(prospects.c.outreach_status.in_(MANAGED_OUTREACH_STATUSES), prospects.c.do_not_contact.is_(False))
    connection.execute(
        prospects.update()
        .where(prospects.c.contact_id == contact_id, prospects.c.email == address)
        .values(
            email_status=status,
            email_verified_at=now,
            outreach_status=case((movable, OUTREACH_STATUS[status]), else_=prospects.c.outreach_status),
        )
    )


def load_contacts(connection, contacts: Table, profiles: Table, args) -> list[dict]:
    statement = (
        select(
            contacts.c.id,
            contacts.c.business_id,
            contacts.c.name,
            contacts.c.email,
            contacts.c.email_status,
            contacts.c.candidate_emails,
            contacts.c.is_primary,
            profiles.c.qualification_score,
        )
        .outerjoin(profiles, profiles.c.business_id == contacts.c.business_id)
        .order_by(contacts.c.business_id, contacts.c.is_primary.desc(), contacts.c.id)
    )
    if args.business_id is not None:
        statement = statement.where(contacts.c.business_id == args.business_id)
    if args.primary_only:
        statement = statement.where(contacts.c.is_primary.is_(True))
    if args.min_score is not None:
        statement = statement.where(profiles.c.qualification_score >= args.min_score)
    if args.limit is not None:
        statement = statement.limit(args.limit)
    return [dict(row._mapping) for row in connection.execute(statement)]


def existing_statuses(connection, prospects: Table, contact_ids: list[int]) -> dict[tuple[int, str], str | None]:
    if not contact_ids:
        return {}
    rows = connection.execute(
        select(prospects.c.contact_id, prospects.c.email, prospects.c.email_status).where(prospects.c.contact_id.in_(contact_ids))
    )
    return {(row.contact_id, row.email): row.email_status for row in rows}


def write_report(path: Path, rows: list[dict]) -> None:
    fields = ["contact_id", "business_id", "email", "status", "verdict", "probed", "catch_all", "role_account", "note"]
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    print(f"Report written to {path}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check every candidate email with Reacher; verified (deliverable) ones become prospects. Sends no email."
    )
    parser.add_argument("--business-id", type=int, help="Process one business only.")
    parser.add_argument("--limit", type=int, help="Maximum number of contacts in this run.")
    parser.add_argument("--primary-only", action="store_true", help="Only process primary contacts.")
    parser.add_argument("--min-score", type=int, help="Only businesses with at least this qualification score.")
    parser.add_argument("--recheck", action="store_true", help="Check again addresses that were already checked.")
    parser.add_argument("--max-candidates", type=int, default=0, help="Cap on addresses checked per contact (0 = all).")
    parser.add_argument("--probe-all", action="store_true",
                        help="Check every address even after the domain is known to be catch-all or unreachable.")
    add_reacher_arguments(parser)
    parser.add_argument("--report", type=Path, help="Write a CSV report (with reasons) of this run's results.")
    parser.add_argument("--dry-run", action="store_true", help="Check and print results without writing to the database.")
    args = parser.parse_args()
    if args.delay < 0 or args.reacher_timeout <= 0 or args.max_candidates < 0:
        parser.error("--delay and --max-candidates cannot be negative and --reacher-timeout must be > 0")

    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        parser.error("DATABASE_URL is not set in the environment or project .env file")
    client = prepare_reacher(parser, args)
    if client is None:
        return 1

    engine = create_engine(database_url, pool_pre_ping=True)
    metadata = MetaData()
    try:
        try:
            contacts = Table("business_contacts", metadata, autoload_with=engine)
            profiles = Table("business_website_profiles", metadata, autoload_with=engine)
            prospects = Table("prospects", metadata, autoload_with=engine)
        except NoSuchTableError as error:
            print(f"Table {error} does not exist. Run `alembic upgrade head` first.")
            return 1
        with engine.connect() as connection:
            queue = load_contacts(connection, contacts, profiles, args)
            known = existing_statuses(connection, prospects, [c["id"] for c in queue])
        if not queue:
            print("No contacts to process.")
            return 0

        print(f"Checking candidate emails for {len(queue)} contact(s).")
        last_call: dict[str, float] = {}
        domain_state: dict[str, tuple[str, str]] = {}
        counts: dict[str, int] = {}
        report: list[dict] = []
        skipped = streak = prospect_count = 0
        aborted = False
        for contact in queue:
            addresses = candidate_addresses(contact, args.max_candidates)
            print(f"Contact {contact['id']} ({contact['name']}), business {contact['business_id']}: {len(addresses)} address(es)")
            for address in addresses:
                prospect_status = known.get((contact["id"], address))
                if not args.recheck and already_decided(contact, address, prospect_status):
                    skipped += 1
                    continue
                domain = address.rpartition("@")[2].casefold()
                if domain in domain_state and not args.probe_all:
                    status, reason = domain_state[domain]
                    outcome = {"status": status, "verdict": status, "summary": None, "note": f"not checked: {reason}"}
                    probed = False
                else:
                    try:
                        outcome = check_address(address, client, args.delay, last_call)
                    except ReacherUnavailable as error:
                        print(f"{error} Stopping; rerun once Reacher is back. Results so far are saved.")
                        aborted = True
                        break
                    probed = True
                    shortcut = domain_shortcut(outcome)
                    if shortcut and not args.probe_all:
                        domain_state[domain] = shortcut
                    summary = outcome["summary"] or {}
                    if summary.get("can_connect_smtp") is False:
                        streak += 1
                    elif summary.get("can_connect_smtp"):
                        streak = 0
                status = outcome["status"]
                counts[status] = counts.get(status, 0) + 1
                summary = outcome["summary"] or {}
                note = outcome["note"] or ""
                now = datetime.now(timezone.utc)
                stored = ""
                if status == "deliverable":
                    stored = " -> prospect"
                elif prospect_status is not None:
                    stored = " -> existing prospect updated"
                print(f"  [{status}] <{address}>{' (not checked)' if not probed else ''}{stored} {note}".rstrip())
                if not args.dry_run:
                    with engine.begin() as connection:
                        connection.execute(contacts.update().where(contacts.c.id == contact["id"]).values(**record_check(contact, address, outcome, now)))
                        if status == "deliverable":
                            upsert_prospect(connection, prospects, prospect_values(contact, address, status, now))
                        elif prospect_status is not None:
                            update_existing_prospect(connection, prospects, contact["id"], address, status, now)
                else:
                    record_check(contact, address, outcome, now)  # keep the in-memory view consistent within the run
                if status == "deliverable":
                    prospect_count += 1
                known[(contact["id"], address)] = status if (status == "deliverable" or prospect_status is not None) else prospect_status
                report.append({
                    "contact_id": contact["id"], "business_id": contact["business_id"], "email": address,
                    "status": status, "verdict": outcome["verdict"], "probed": probed,
                    "catch_all": summary.get("is_catch_all"), "role_account": summary.get("is_role_account"), "note": note,
                })
                if streak >= MAX_CONSECUTIVE_NO_SMTP:
                    print(f"Reacher could not open an SMTP connection to {streak} servers in a row; outbound port 25 is "
                          "probably blocked for Docker or your network. Stopping. Results so far are saved.")
                    aborted = True
                    break
            if aborted:
                break

        total = sum(counts.values())
        verb = "would be" if args.dry_run else "added or updated as"
        print(f"Done: {total} address(es) checked ("
              + (", ".join(f"{n} {st}" for st, n in sorted(counts.items())) if counts else "none")
              + f"); {prospect_count} {verb} prospects"
              + (f"; {skipped} skipped (already checked)" if skipped else "") + ".")
        needs_review = counts.get("risky", 0) + counts.get("unknown", 0)
        if needs_review:
            print(f"{needs_review} address(es) need review (risky/unknown); they are not prospects and must not be emailed automatically.")
        if args.report:
            write_report(args.report, report)
        return 1 if aborted else 0
    except SQLAlchemyError as error:
        print(f"Database operation failed ({type(error).__name__}): {str(error).splitlines()[0]}")
        print("Check DATABASE_URL and run `alembic upgrade head` first.")
        return 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
