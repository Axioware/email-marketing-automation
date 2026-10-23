# Email Marketing Automation

Finds local businesses, researches and scores them, finds the decision maker and a working email, writes a personalised email (and follow-ups) for each from your templates and lets you review and approve them. You queue approved emails in the **Outbox** and the app **sends them from your own mailboxes (SMTP)**, each at its time; leads move through a **Pipeline** (New Lead → ... → Won / Lost) that you update by hand on replies. Everything runs **on your machine** (the Django app, SearXNG, Reacher and the scraper container in Docker); the data lives in **Supabase** (PostgreSQL). It is a Django project: everything is in the **Django admin** and a **REST API**, and every step is also a `manage.py` command.

**Using the admin day to day:** see the [user guide](docs/USER_GUIDE.md), also available in the admin under **Guide** (top right).

## Run the project

### One-time setup

```sh
cd email-marketing-automation
python -m venv .venv && source .venv/bin/activate    # skip if .venv already exists
python -m pip install -r requirements.txt
python -m playwright install chromium               # Modules 1 and 2
cp .env.example .env                                # then fill it in (see below)
docker compose up -d                                # SearXNG (docker-compose.yml)
python manage.py migrate                            # creates / updates every table
python manage.py createsuperuser                    # your admin login
python manage.py runserver                          # http://127.0.0.1:8000/admin/
```

Generate a `DJANGO_SECRET_KEY` for `.env` with `python -c "import secrets; print(secrets.token_urlsafe(50))"`.

**Database created before the move to Django** (with Alembic): run `python manage.py migrate --fake-initial` once instead of `migrate`. It adopts the existing tables without touching their data, converts the three remaining `json` columns to `jsonb`, re-applies the triggers and the open-tracking function, adds Django's own tables, and turns on row level security for every table (so the Supabase REST API cannot read them). The old `alembic_version` table is no longer used and can be dropped.

Also needed:

- **Supabase**: the database (`DATABASE_URL`; the pooler URL if your network has no IPv6). Run `python manage.py migrate` against it after pulling new code.
- **Docker**: `docker compose up -d` starts SearXNG (`SEARXNG_URL=http://127.0.0.1:8888`, reachable from this machine only). Email verification (Module 4) starts Reacher itself (see "Docker setup" below); the Google Maps scraper container runs from its own repo (`MAPS_SCRAPER_URL`, default `http://127.0.0.1:8001`).
- **Your mailboxes** for sending: add them in the admin under **Mailboxes**, and set `MAILBOX_ENCRYPTION_KEY` in `.env` (the same on every machine) so their passwords can be stored encrypted (see [Sending from your mailbox](#sending-from-your-mailbox-the-outbox)).

### Docker setup (Reacher, for email verification)

Email verification runs [Reacher](https://github.com/reacherhq/check-if-email-exists) in a local Docker container. You do not create it by hand: the verify step builds and configures it on its first run. You only need Docker working and a few `.env` values.

1. **Install Docker** ([docs.docker.com/engine/install](https://docs.docker.com/engine/install/)) and check it works without `sudo`:

   ```sh
   docker run --rm hello-world
   ```

   On Linux, if you get "permission denied", add yourself to the `docker` group and log in again: `sudo usermod -aG docker $USER`.

2. **Check that outbound port 25 is open.** Reacher talks to mail servers on port 25, and many home ISPs and cloud hosts block it (results then come back `unknown`):

   ```sh
   python -c "import socket; socket.create_connection(('gmail-smtp-in.l.google.com', 25), timeout=8); print('port 25 open')"
   ```

3. **Set the sender identity in `.env`.** Reacher announces these to the mail servers it checks; the first must be an address that can receive mail, and the second a name with a dot:

   ```sh
   VERIFY_MAIL_FROM=you@yourdomain.com
   VERIFY_HELO=yourdomain.com
   ```

   With no domain of your own, see "No domain of your own?" in Module 4 below. Placeholder domains such as `example.org` are refused.

4. **Run the verifier.** The first run downloads the image (`reacherhq/backend`) and starts a container named `reacher`, bound to `127.0.0.1:8080`, which restarts with Docker. It prints `Using Reacher <version>` when ready:

   ```sh
   python manage.py verify_emails --limit 1 --dry-run
   ```

   Starting verification from the admin (Businesses or Contacts > **Verify contact emails**, or **Run full pipeline**) does the same: the container is created or started automatically.

Managing the container:

```sh
docker ps --filter name=reacher     # is it running?
docker logs reacher                 # what is it doing?
docker stop reacher                 # stop it (the next run starts it again)
docker rm -f reacher                # delete it (the next run recreates it from .env)
```

If you change `VERIFY_MAIL_FROM` or `VERIFY_HELO`, the next run recreates the container automatically. Port 8080 already in use? Set `REACHER_URL=http://127.0.0.1:8081` in `.env` and that port is used. Add `--no-auto-start` to manage the container yourself (command in Module 4 below).

`.env` keys (`.env.example` lists every one):

| Needed for | Keys |
|---|---|
| Everything | `DATABASE_URL` (Supabase), `DJANGO_SECRET_KEY` (`DJANGO_DEBUG`, `DJANGO_ALLOWED_HOSTS` optional) |
| The API | `AUTH_TOKEN` (sent in every request's `Auth` header) |
| Website research | `GROQ_API_KEY` (or `GROK_API_KEY`), otherwise `OPENAI_API_KEY` |
| Decision makers | `SEARXNG_URL` (the local SearXNG) |
| Email verification | `VERIFY_MAIL_FROM`, `VERIFY_HELO` |
| Sending | `MAILBOX_ENCRYPTION_KEY` (encrypts the mailbox passwords; the mailboxes themselves are in the admin), `SENDER_NAME` (the sign-off; empty: "The Axioware team") |

### Docker setup (Google Maps scraper, optional second lead source)

Businesses can be found two ways, chosen per campaign (**Lead source**) or per run (`--source`):

- **Built-in browser** (default): this app drives its own Chromium through Playwright. Needs `python -m playwright install chromium`.
- **Scraper container**: the [Google Maps scraper](https://github.com/conor-is-my-name/google-maps-scraper) runs in Docker and does the browsing; this app calls its API and saves the results exactly as it saves its own.

To run the container (from its folder, e.g. `~/Repositories/google-maps-scraper`):

```sh
docker network create shark      # once: its docker-compose.yml expects this network
docker compose up -d --build      # serves http://127.0.0.1:8001
curl -H "X-API-Key: $SCRAPER_API_KEY" http://127.0.0.1:8001/   # {"message": "Google Maps Scraper API is running."}
```

Its `docker-compose.yml` publishes the port as `127.0.0.1:8001:8001` (this machine only), not `8001:8001`, which opens it to the whole network, and needs `SCRAPER_API_KEY` in a `.env` next to it; set the same value as `MAPS_SCRAPER_KEY` in this app's `.env`. Set `MAPS_SCRAPER_URL` in `.env` if it runs elsewhere (default `http://127.0.0.1:8001`). The dashboard's **Get leads** panel shows whether it answers. Each search runs inside the container and must finish in its 5-minute limit, so one search asks for at most 40 places (3 places took about 100 seconds in testing; a search that times out is skipped and reported). Its own guidance: at most about 500 places a day per IP, at least a minute between searches.

### The pipeline

Run the steps in order. Each one reads what the previous one stored, and each skips work it has already done, so it is safe to rerun. Every step can be started three ways: from the admin, through the API, or as a command.

**All at once:** open a campaign and click **Run full pipeline** (or `python manage.py run_pipeline --campaign-id 1 --fetch-limit 20`, or `POST /api/campaigns/1/run-pipeline/`). It runs steps 2-6 for that campaign's businesses, continues past partial failures, stops if a step cannot run, and never sends: emails wait for review.

| Step | Admin | Command |
|---|---|---|
| 1. Campaign | Discovery campaigns > Add | `python manage.py create_campaign` |
| 2. Find businesses | Dashboard > **Get leads**, or Campaign > **Fetch with built-in browser** / **Fetch with scraper container** | `python manage.py fetch_businesses --campaign-id 1 --limit 20 [--source browser\|scraper_api]` |
| 2b. Fill missing details | Businesses > action **Refresh Google Maps details** | `python manage.py refresh_businesses` |
| 3. Research and score | Businesses > action **Research websites** | `python manage.py research_websites` |
| 4. Find owners | Businesses > action **Find decision makers** | `python manage.py find_stakeholders` |
| 5. Verify emails | Businesses or Contacts > action **Verify emails** | `python manage.py verify_emails` |
| 3b. Review low scores | Businesses > filter **Needs your review** > **Approve** / **Reject** | - |
| 5b. Review unverified addresses | Prospects > filter Needs review > **Approve** / **Reject** | - |
| 6. Write emails | Prospects or Businesses > action **Write emails from templates** | `python manage.py generate_emails [--email-prompt-id N]` |
| 7. Review | Emails: edit, Approve (= a send time), Reject, Fill in again | - |
| 8. Send | **Outbox**: tick approved emails, **Queue for sending**; each is sent from your mailbox at its time | `python manage.py send_emails` (the sender; queueing starts it) |
| 8b. Leads | **Pipeline**: move leads on replies, demos, won/lost (stops or resumes their emails) | - |
| 9. Follow-ups | **Email sequences** (initial template, then days + template per follow-up); the campaign picks one or more (A/B); written for review once the previous email is sent | `python manage.py generate_followups` |

Every command has `--help`. Before running one on real data, try it with `--limit 1 --dry-run`, which shows what it would do and saves nothing:

```sh
python manage.py verify_emails --limit 2 --dry-run
python manage.py generate_emails --limit 1 --dry-run
```

### Pipeline runs (background jobs)

Steps started from the admin or the API run as **pipeline runs**: a background process executes the same command and streams its output into the run (Pipeline runs in the admin). An admin action first opens the run form pre-filled with the command and the selected IDs, so you can adjust the arguments (e.g. add `--dry-run`) before clicking Save. The run page refreshes itself while it works and has a **Stop this run** button; **Run again** repeats it. Runs keep going if the web server restarts; a run whose process died is marked failed.

Notes:

- **Reacher starts itself.** The first verification run creates and configures the Docker container. It needs outbound port 25.
- **Step 4 searches through SearXNG** (`SEARXNG_URL`); without it only the website is used.
- **Opens are counted only after an email is sent**, through the deployed edge function.

## REST API

Everything in the admin is also in the API at `/api/`. Interactive docs (Swagger) are at `/api/docs/` and the OpenAPI schema at `/api/schema/` (both need an admin login; in the docs click **Authorize** and enter the key to try requests).

**Every request must send the `Auth` header** with the value of `AUTH_TOKEN` from `.env`; anything else gets `401`. With `AUTH_TOKEN` empty the API refuses every request. Generate a key with `python -c "import secrets; print(secrets.token_urlsafe(32))"` and restart the server after changing it. Optionally also send `Authorization: Token <user token>` (Admin > Auth Token > Tokens) so runs you start are attributed to that user.

| Endpoint | What |
|---|---|
| `/api/campaigns/` | list, create, edit, delete: `lead_source`, `campaign_prompt`, `email_sequences` (ids; several = A/B split), `min_score`, `send_window_start`/`send_window_end`, `send_timezone`; `POST /api/campaigns/{id}/fetch-businesses/` `{"limit": 20, "source": "scraper_api"}` |
| `/api/settings/` | the global settings: `daily_cap`, `gap_fixed_seconds`, `gap_random_min_seconds`, `gap_random_max_seconds`, `reply_window_days`, `footer_text` (`GET`, `PATCH`, `PUT`) |
| `/api/email-sequences/`, `/api/follow-ups/` | email sequences (`name`, `initial_template`, read-only `follow_ups` and `campaigns`) and their follow-up steps (`email_sequence`, `sequence`, `days_after_previous`, `email_prompt` = its template); `POST /api/emails/{id}/mark-replied/` (also on prospects and businesses) stops that prospect's sequence |
| `/api/campaign-prompts/` | campaign prompts qualify businesses (the research agent's instructions, used as stored; shared by campaigns) |
| `/api/email-templates/` (also `/api/email-prompts/`), `/api/email-variables/` | email templates (`name`, `kind`: `initial` or `follow_up`, `subject`, `body` with `{{variables}}`; unknown variables are refused with `400`) and the variables they can use |
| `POST /api/emails/queue/` `{"ids": [...]}`, `POST /api/emails/unqueue/` | queue approved (or failed) emails for sending (the sender starts), or take them out of the queue |
| `GET /api/pipeline/`, `POST /api/prospects/{id}/move-stage/` `{"stage": "replied", "note": ""}` | leads per pipeline stage; move a lead (stopping or resuming its emails, see [The Pipeline](#the-pipeline-leads)) |
| `/api/businesses/`, `/api/business-sources/`, `/api/website-profiles/` | Modules 1-2 data, with filters (`?city=`, `?website_profile__qualification_score__gte=50`), `?search=` and `?ordering=` |
| `/api/contacts/`, `/api/prospects/` | Modules 3-4 data (e.g. `PATCH` a prospect's `do_not_contact`; filter `?pipeline_stage=`) |
| `/api/emails/` | list, view, `PATCH` `subject`/`body` (goes back to review), delete |
| `/api/emails/{id}/approve/`, `reject/` `{"note"}`, `reopen/`, `regenerate/` | review actions; `409` when not allowed in the email's current status |
| `/api/emails/{id}/preview/`, `/api/emails/bulk/` `{"action": "approve", "ids": [...]}`, `/api/emails/stats/` | preview HTML, bulk review, counts and open rate |
| `/api/email-opens/` | recorded opens |
| `/api/runs/` | start a pipeline step, list and follow runs; `GET /api/runs/commands/` lists commands and options; `POST /api/runs/{id}/cancel/` |
| `/api/stats/` | counts across the whole pipeline |
| `/api/usage/`, `/api/usage/summary/` | AI tokens and Hunter credits per call (action, provider, model, business, prospect, run); `?from=YYYY-MM-DD&to=YYYY-MM-DD`, `?action=`, `?provider=`, `?business=`; the summary totals them per action and model |

Start a step and follow it:

```sh
curl -X POST -H "Auth: $AUTH_TOKEN" -H "Content-Type: application/json" \
  -d '{"command": "verify_emails", "options": {"limit": 50, "primary_only": true}}' \
  http://127.0.0.1:8000/api/runs/
curl -H "Auth: $AUTH_TOKEN" http://127.0.0.1:8000/api/runs/1/     # status, exit_code, output
```

`options` uses the command's option names (`--dry-run` is `"dry_run": true`, repeatable options take a list: `"business_id": [1, 2]`); raw `"arguments": ["--limit", "5"]` works too. Invalid options are refused with `400` before anything starts.

## Create a discovery campaign

In the admin: Discovery campaigns > Add (locations and search terms are comma-separated). From the terminal:

```sh
python manage.py create_campaign --name "Lahore dentists" --country Pakistan --locations "Lahore, DHA" --search-terms "dental clinic, dentist"
python manage.py create_campaign      # or answer the prompts
```

## Fetch businesses from Google Maps

Two lead sources (see [Docker setup (Google Maps scraper)](#docker-setup-google-maps-scraper-optional-second-lead-source)): the **built-in browser** (needs Chromium, `python -m playwright install chromium`) and the **scraper container**. Each campaign has a **Lead source** (default: built-in browser), used whenever it is fetched without `--source`. From the admin: the dashboard's **Get leads** panel has a button per source for each campaign; a campaign's page has **Fetch with built-in browser** and **Fetch with scraper container**; the Campaigns list has an action for each. From the terminal:

```sh
python manage.py fetch_businesses --campaign-id 1 --limit 20                         # the campaign's lead source
python manage.py fetch_businesses --campaign-id 1 --limit 20 --source scraper_api    # through the container
```

Container options: `--lang` (Google Maps language, default `en`) and `--concurrency` (places opened at once, 1-20, default 5). The full pipeline takes `--source` too and passes it to its fetch step.

At a terminal, `--campaign-id` and `--limit` are asked for when omitted; in a background run they are required. The fetcher searches that campaign's terms and locations, stores up to the requested number of new businesses, and leaves unavailable Maps fields as `NULL`. Use `--headed` if Google requires manual consent. Automated Google Maps access may be restricted by its terms; for production use, prefer the official Places API.

Each new business is saved with a `business_sources` record containing the unnormalized Google Maps payload in PostgreSQL `JSONB`. `source` says which lead source found it: `google_maps` (built-in browser) or `google_maps_scraper` (container). Both are de-duplicated the same way against **every** saved business, in any campaign (Google place id, Maps link, then name + address or website): a business is saved once, so it gets one research, one set of contacts and one email sequence however many campaigns find it. One another campaign already has is skipped and named in the run's output. The database also refuses a second business with the same Google place id (migration 0011 stops with a list if duplicates already exist).

## Research business websites

Set an LLM key in `.env`: `GROQ_API_KEY` (or `GROK_API_KEY`; used when present; optionally `GROQ_MODEL`, default `openai/gpt-oss-120b`) or, as the fallback, `OPENAI_API_KEY` (optionally `OPENAI_MODEL`, default `gpt-5.4-mini`). The script prints which provider and model it uses. Groq is called through its OpenAI-compatible API, so no extra package is needed. The agent needs strict JSON-schema output, so a different `GROQ_MODEL` must support it. No browser is needed: each page's HTML is fetched over HTTP (links, emails) and its text comes from Jina Reader, which renders the page (JavaScript included) in its own browser. Jina's public endpoint needs no key and allows 20 requests per minute; `JINA_API_KEY` raises that. If Jina fails, the page's own HTML text is used. `--local-only` never sends URLs to Jina (sites built by JavaScript then show less).

```sh
python manage.py research_websites --limit 5
```

The script processes businesses with websites that do not yet have a completed profile. Failed profiles are skipped on normal runs; pass `--retry-failed` to retry them. Use `--limit N` for a bounded batch or `--business-id ID` (repeatable) for specific businesses; in the admin, select businesses and use the **Research websites** action. It asks the agent to SCORE, SCRAPE, or EXIT after each page, enforces a maximum of five unique page visits per business, and stores compact page summaries/findings, discovered links, emails, and URLs in `business_website_profiles`. Only the current page's full cleaned text is sent on each agent turn; raw HTML is not saved.

**What it scores against** is the business's campaign's **campaign prompt** (table `campaign_prompts`, edited in the admin under Campaign prompts): what you sell, what a good and a poor prospect look like and what each score range means. The agent receives **exactly that text** as its instructions; nothing is added in front of it, so everything about qualifying lives in the database. The app only fixes the shape of each answer: a strict JSON schema (`AgentResponse` in `pipeline/services/research.py`) whose field descriptions say how to ask for another page (`SCRAPE` + a URL from `available_links`), score (`SCORE` + `score`, `reasons`, `reasoning`) or stop (`EXIT`); the campaign prompt's page shows it under **Answer format (set by the app)**. An output format written inside the prompt is overridden by that schema. Businesses whose campaign has no campaign prompt are skipped and reported (they stay unresearched); if none of the selected businesses has one, the run stops. Businesses scoring below their campaign's **minimum score** (`min_score`, default 50), whose research failed, or without a website wait for a person: Businesses > **Needs your review** > **Approve** (they go on to finding decision makers) or **Reject**.

## Find decision makers and contact emails (Module 3)

> **Two ways to find and verify contacts**, chosen on the admin's **Settings** page ("Find and verify contacts with", `contact_provider` in `/api/settings/`): **Websites + web search, verified by Reacher** (the default, described below; free) or **Hunter** (`HUNTER_API_KEY` in `.env`; uses Hunter credits). With Hunter, Find decision makers asks Hunter's Domain Search for the people at the business's domain (its company name when it has no website; up to 10 emails, 1 credit) and stores them as contacts (`email_source` = `hunter`), ranked by the roles file: target roles first, then other people by seniority and Hunter's confidence; excluded roles (receptionist...) are left out and a generic address (info@) is used only when no person is found. Verify emails then checks each contact's own address with Hunter's Email Verifier (1 credit each; `--max-candidates N` for more) and stores the prospect like Reacher's results (provider `hunter`): valid = deliverable, accept_all = risky (catch-all), invalid/disposable = undeliverable, webmail/unknown = needs review. An address Hunter already answered is not checked again (`--recheck` forces it). A refused key, used-up credits or rate limit stop the run; what was done is kept. Reacher and SearXNG are not used in this mode.

No browser and no paid services: the business's pages are read through Jina Reader and web search goes to your own SearXNG instance (`SEARXNG_URL`, e.g. `http://your-server:8080`; its `settings.yml` must list `json` under `search: formats`; `SEARXNG_KEY` is sent as `X-API-Key` when set). In the admin: select businesses, action **Find decision makers**.

```sh
python manage.py find_stakeholders --limit 5
```

For each qualified business (research score at least its campaign's **minimum score**, or approved by a person; rejected ones never; `--min-score` overrides the campaign's minimum) it runs a waterfall:

1. **Module 2 data:** scraped page text, findings, emails and LinkedIn links are mined for people and job titles.
2. **Site pages** (only if no target-role person yet; `--max-pages`, default 6): team/about/contact pages are read through Jina Reader, which renders JavaScript-built pages as a visitor sees them. Pages Module 2 already read are skipped.
3. **Role ranking:** people are classified with `config/target_roles_dental.json` (target roles, fallback roles, excluded roles). Swap the file with `--roles-file` for other niches.
4. **Web search** (only if still no target-role person, at most `--max-searches` per business, default 3): SearXNG results, each title and snippet read on its own. The searches use the business's name without the branch Google Maps adds ("Alvi Dental Hospital — SMCHS" is searched as "Alvi Dental Hospital"). A result counts only if it contains the business's phone number, or its name plus location/domain.
5. **Email:** an email found publicly is stored with `email_source` `website` or `search`; otherwise a pattern guess is stored as `inferred`. All other guesses are kept in `candidate_emails`. Every email is `unverified`; verification is Module 4.
6. **Selection:** the highest-ranked role becomes the primary contact (`is_primary`); other target-role people are kept as secondary contacts. `source_urls` and `discovery_reasoning` record the evidence.

Options: `--business-id ID` (repeatable), `--redo` (reprocess and replace this module's contacts), `--no-search`, `--no-crawl`, `--dry-run`, `--delay SECONDS` (between searches, default 1).

## Verify contact emails and build prospects (Module 4)

> **Excluded patterns and the AI email score (Module 4b).** On the **Settings** page (Email scoring): **excluded patterns** (one per line, `*` = anything, e.g. `info@*`, `business@*`, `*@gmail.com`) disqualify matching addresses: verification does not check them (no Reacher time, no Hunter credit) and stores them Rejected (verdict `excluded`). With **Require an AI score** on (default), a deliverable address then waits as `pending` until **Score emails** (`python manage.py score_emails`; Prospects or Businesses > action **Score emails with the AI**) sends it to the AI with the contact and the business, using the **scoring prompt** from Settings. The AI answers a score (0-100) and a reasoning (`prospects.email_score`, `email_score_reason`, shown on Prospects): a deliverable address at or above the **minimum email score** becomes `ready`; a lower score, or a risky/unknown address, goes to `needs_review` with the reasoning. Scored addresses are not sent again unless `--rescore` (e.g. after changing the prompt); a pattern added later also disqualifies addresses verified before. Addresses a person approved are left alone. Turn the requirement off and deliverable addresses are ready right after verification, as before.

One command, `python manage.py verify_emails` (admin: select businesses or contacts, action **Verify ... emails**):

1. You run it.
2. For each contact in `business_contacts` it takes the contact's own `email` plus every address in `candidate_emails`.
3. Each address is checked with [Reacher](https://github.com/reacherhq/check-if-email-exists) (`reacherhq/backend`) running in **your own Docker**; no hosted service or API key is used, the command does no DNS or SMTP checking itself, and no email is sent.
4. Every checked address is stored in `prospects` (one row per contact and email) with its status and a detailed verdict (e.g. `catch_all`). Only deliverable ones are `ready` to email. The outcome is also recorded on the contact's `candidate_emails`.

Requirements: Docker, and **outbound port 25** open (Reacher opens the SMTP connections from its container, which normally shares your host's network path; many home ISPs and cloud providers block it, in which case results come back `unknown`).

### Setup

Put a sender address and a fully-qualified hostname on a domain you control in `.env`. **The sender's domain must be real and able to receive mail**: mail servers reject checks sent from a sender that cannot receive mail (for example Hostinger answers `550 Sender address rejected: Domain example.org does not accept mail`), and Reacher reports that rejection as an invalid mailbox, which turns real addresses into false `undeliverable` results. It therefore refuses placeholder domains (`example.com`, `*.invalid`, ...) and asks Reacher to confirm the sender's domain accepts mail before checking any contact.

```sh
VERIFY_MAIL_FROM=verify@yourdomain.com
VERIFY_HELO=yourdomain.com
```

Reacher ignores the sender and HELO per request and defaults to `localhost`, which mail servers such as Hostinger refuse (giving false results), so they are container settings. The command manages the container for you: on each run it creates the `reacher` container if missing (`127.0.0.1:8080`, `--restart unless-stopped`), starts it if stopped, and **recreates it if its sender/HELO differ from `.env`**. Use `--no-auto-start` to manage Docker yourself, e.g.:

```sh
docker run -d --name reacher --restart unless-stopped -p 127.0.0.1:8080:8080 \
  -e RCH__FROM_EMAIL=verify@yourdomain.com -e RCH__HELLO_NAME=mail.yourdomain.com \
  reacherhq/backend:latest
```

**No domain of your own?** A mailbox you own works as the sender (`VERIFY_MAIL_FROM=you@gmail.com`; the script warns because some servers check SPF for the sender and may refuse the checks), and a host without a name can announce its IP as an address literal: `VERIFY_HELO=[203.0.113.7]` (RFC 5321). Home IPs change, so update the literal when yours does. A reverse-DNS name for your IP, if you have one (`dig +short -x <your ip>`), is better than the literal. Never use someone else's domain.

`REACHER_URL` may change the local port; non-local URLs are refused.

```sh
python manage.py verify_emails --primary-only --limit 5 --dry-run
```

### What is stored

Reacher's `is_reachable` is mapped as: `safe` -> `deliverable`, `invalid` -> `undeliverable`, `risky` -> `risky`, `unknown` -> `unknown`. Every checked address becomes a `prospects` row:

| `prospects` column | Set by this step |
|---|---|
| `business_id`, `contact_id`, `email` | the contact and address |
| `email_status` | `deliverable`, `undeliverable`, `risky` or `unknown` |
| `verdict` | why: see the table below |
| `verification_note` | the reason in words, e.g. "domain accepts mail for any address (catch-all)" |
| `verification_details` | Reacher's findings (catch-all, role account, SMTP error, ...) and `probed` (false when the address was not checked because its domain already gave the answer) |
| `email_verification_provider`, `email_verified_at` | `reacher` and when it was checked |
| `qualification_score` | copied from the business's website profile (Module 2) |
| `outreach_status` | `ready` (deliverable), `needs_review` (risky, unknown) or `rejected` (undeliverable) |

| Status | Verdicts |
|---|---|
| `deliverable` | `deliverable` |
| `undeliverable` | `mailbox_not_found`, `disabled`, `no_mail_server`, `invalid_syntax` |
| `risky` | `catch_all`, `full_inbox`, `disposable`, `risky` |
| `unknown` | `smtp_unreachable`, `blocked` (the server refused the check), `temporary_failure` (greylisting), `check_failed`, `unknown` |

Only `ready` prospects get emails: generation skips everything else. A rerun skips addresses already stored as deliverable, undeliverable or risky and checks `unknown` ones again; `--recheck` checks everything. Rechecking refreshes the verification columns; `outreach_priority`, `outreach_facts`, `research_summary`, `do_not_contact` and `last_contacted_at` belong to later stages and are never overwritten, and a prospect with `do_not_contact` set or one a later stage has moved on (e.g. `contacted`) keeps its `outreach_status`.

If a domain is a catch-all, or its mail server cannot be reached, the remaining addresses on that domain are not probed (they would get the same answer) and are recorded with that outcome (verdict `catch_all`, `probed: false`). Catch-all domains are remembered across runs, so new addresses on them are recorded the same way without a probe; `--probe-all` checks every address anyway. The run stops early if Reacher becomes unreachable, or if it cannot open an SMTP connection to 3 servers in a row (port 25 blocked); results so far are kept.

```sh
python manage.py verify_emails --limit 5 --dry-run --report out.csv
```

Options: `--business-id`, `--contact-id` (both repeatable), `--limit` (contacts), `--primary-only`, `--min-score`, `--recheck`, `--max-candidates`, `--probe-all`, `--delay`, `--reacher-timeout`, `--mail-from`, `--helo`, `--no-auto-start`, `--report out.csv`, `--dry-run`.

## Write outreach emails from templates and track opens (Module 5)

### Write emails

`python manage.py generate_emails` (admin: select prospects or businesses, action **Write emails from templates**) fills one email per prospect that is ready for outreach (`email_status = 'deliverable'` or approved by a person, `outreach_status = 'ready'`, not `do_not_contact`) and stores it in `emails` with status `in_review`. **Nothing is sent, and no AI is involved:** each email is an **email template** (table `email_prompts`, admin **Email templates**) with its variables filled in for that prospect. No AI key is needed for this step (only research uses one).

A template has a **Kind** (initial email or follow-up), a **Subject** and a **Body** (plain text, blank lines between paragraphs; the footer is added automatically: edit it on the admin's **Settings** page under **Email footer**, or `footer_text` in `/api/settings/`; it applies to emails written or edited from then on). Variables:

- `{{first_name}}` is replaced by the prospect's value.
- `{{first_name|there}}` uses `there` when the prospect has no value.
- A variable with no value and no fallback **holds that email back** (the run lists it as "held back" and names the variable), so no email goes out with a gap. Give it a fallback, or fill in the missing data, and run again.
- The variables: `first_name`, `last_name`, `full_name`, `job_title`, `business_name`, `business_short_name` (without the branch Google Maps adds), `category`, `city`, `country`, `website`, `google_rating`, `google_review_count`, `campaign_name`, `sender_name` (`SENDER_NAME`), `sender_company`, `previous_subject` (follow-ups only), and every output field of the campaign prompts (what research extracted, e.g. `team_size`, `languages`; lists are joined with commas, yes/no fields show yes or no). The admin's **Email variables** page (button on the Email templates list, link on each template) lists them all with a real prospect's values; `GET /api/email-variables/` returns the same.
- Saving a template with an unknown variable, `{{previous_subject}}` in an initial template, or broken braces is refused with the reason. A template's page shows a preview filled in for the latest ready prospect.

Templates are put in order by **email sequences** (table `email_sequences`, admin **Email sequences**): an initial-email template, then follow-up steps (`follow_ups`: number, days after the previous email, template). A campaign chooses one or more sequences; with several it is an A/B test of the whole flow: each new prospect gets the sequence with the fewest prospects of the campaign so far (`prospects.email_sequence`) and keeps it to the end, so sequences can differ in templates, timing and length. The dashboard and each sequence's page show sent emails and reply rates per sequence and step. `--email-prompt-id` (or `--template-id`) overrides the template. Templates and sequences are reusable across campaigns. A prospect whose campaign has no sequence, or whose sequence has no initial-email template, is skipped and reported. Migrations 0023-0025 turned each campaign's old setup into sequences (one per initial-email template it had, each with the campaign's follow-ups, named "Campaign - Template" or just the campaign's name) and kept every prospect in its A/B group. Migration 0022 replaced the old AI prompts' text with starter templates (an initial one and a follow-up one): edit them in the admin.

```sh
python manage.py generate_emails --limit 1 --dry-run
```

Options: `--prospect-id`, `--business-id` (both repeatable), `--email-prompt-id` (default: the initial-email template of each prospect's sequence), `--limit`, `--regenerate` (fills again emails in review or rejected, keeping their tracking token; approved and sent emails are never modified), `--dry-run` (prints the filled emails without saving).

### Open tracking (switched off)

**Open tracking is off: emails contain no image, and opens are not recorded.** Emails carry the body and the footer text (company details and unsubscribe line) only. Everything tracking needs is kept as a placeholder so it can be turned back on: each email's tracking token and open fields (`open_count`, `first_opened_at`, `last_opened_at`, status `opened`), the `email_open_events` table, the `record_email_open` database function and the Supabase edge function. While it is off, the dashboard and the Emails list show the open rate as "off" and `EMAIL_TRACKING_BASE_URL` is not needed.

To turn it back on, set `OPEN_TRACKING = True` in `pipeline/services/generation.py`: new emails then carry the tracked footer image again. How it works when on:

Each email gets a random tracking token (`secrets.token_urlsafe(32)`) that contains nothing about the recipient. The HTML footer image points at the Supabase Edge Function `email`:

```
https://<project>.supabase.co/functions/v1/email/footer/{tracking_token}
```

When an email client loads the image, the function calls the database function `record_email_open`, which, in one atomic statement, sets `first_opened_at` (once), `last_opened_at`, increments `open_count`, changes `status` from `sent` to `opened`, and stores an `email_open_events` row (time, user agent). Only emails with `sent_at` set count, so previews of unsent emails are ignored. The function always returns the footer image, even for unknown tokens, and asks clients not to cache it. `emails` and `email_open_events` have row level security on with no policies, so only the service role (the edge function and this backend) can read them.

The tracking base URL is derived from a Supabase `DATABASE_URL`; set `EMAIL_TRACKING_BASE_URL` to override it.

**Deploy (once):**

1. Apply the migrations (`python manage.py migrate`). They also create the public Storage bucket `email-assets`.
2. Upload `assets/email-footer.png` to Storage as `email-assets/footer.png` (Dashboard -> Storage). It is the Axioware footer (logo, tagline, site, email, city). The text footer in emails (the **Email footer** on the Settings page) carries the same details for clients that block images. If it is missing, the function returns a transparent 1x1 image so emails never show a broken image. Other names: set the `FOOTER_BUCKET` / `FOOTER_PATH` function secrets.
3. Deploy the function without JWT verification (email clients send no auth header; `supabase/config.toml` sets this too):

   ```sh
   npx supabase login
   npx supabase functions deploy email --project-ref <project-ref> --no-verify-jwt
   ```

`SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` are provided to the function by Supabase automatically.

Opens are rate limited in the database: at most 10 recorded opens per email per minute (`record_email_open` locks the email row, so concurrent requests are counted exactly). Over the limit the image is still returned but no open is recorded. The function also keeps a per-IP limit (60/min) in memory, but Supabase spreads requests across many function instances, so that one is only a light extra guard.

**Limitations:** an open means the image was requested, not that a person read the email. Clients that block images miss opens, image proxies and caches can hide repeat opens, and security scanners can load the image before the recipient does. Treat it as an engagement signal.

### Review in the admin

Every new email starts in `in_review`. Review them in the admin under **Emails**:

- **List**: counts per status at the top (click one to filter), search by business, recipient or subject, and the actions **Approve selected**, **Queue for sending**, **Take out of the queue**, **Reject selected** and **Mark replied**.
- **Email page**: edit the subject and body (the footer is added automatically), see a preview, and see what earlier modules found about the business (website findings, qualification reasons). Actions: **Approve** (gives it a send time, then jumps to the next email in review), **Queue for sending** / **Take out of the queue**, **Reject** with an optional note, **Back to review**, and **Fill in again from template** (optionally with another template). Editing, rejecting or sending it back to review frees its time and takes it out of the queue.
- Editing an approved email sends it back to review. Emails with `PLACEHOLDER` text cannot be approved. Sent emails are read-only and show their opens.

Statuses: `in_review` -> `approved` (send time given) -> `queued` (Outbox) -> `sending` -> `sent` -> `replied`; plus `rejected` and `failed` (the send failed; queue it again). `opened` is kept for open tracking.

## Sending from your mailbox (the Outbox)

The app sends the emails itself, over SMTP from your own mailboxes. Nothing runs on a timer: you queue emails, and a background sender sends them at their times.

**Mailboxes** (admin: **Mailboxes**, table `mailboxes`): one row per SMTP account: name, from address and name, server, port, security (`starttls`, `ssl` for port 465, or `none`), username, password (Gmail / Google Workspace: an app password), **order** and **active**. Passwords are stored encrypted (Fernet) with `MAILBOX_ENCRYPTION_KEY` from `.env`; every machine running the app needs the same key. **Test the login** (an action on the list) connects and logs in without sending anything. The list shows each mailbox's planned emails today against the cap, and its last problem.

1. **Approval = a mailbox and a send time.** The limits on the **Settings** page (`daily_cap`, `gap_fixed_seconds` + a random `gap_random_min_seconds`..`gap_random_max_seconds`) apply **to each mailbox separately**. An approved email takes the **first active mailbox in order that still has room** on the earliest day it can go, at that mailbox's next slot (its latest send + the gaps, inside the campaign's send window); when the first mailbox is full for the day the next one is used, and when **every mailbox is full** the email waits for the next day (`emails.mailbox`, `emails.scheduled_at`). A follow-up prefers the mailbox the lead's previous email went from (same sender, same thread) when it has room, and never goes before "previous email sent + its days". It stays **Approved**; nothing is sent yet. Without any mailbox an email still gets a time; queueing it needs one (it is given a mailbox then).
2. **Queue (Outbox).** The admin's **Outbox** lists approved, queued and failed emails with their send times (in each campaign's time zone). Tick them and choose **Queue for sending** (also on the email page, or `POST /api/emails/queue/`). An email whose time has already passed is sent right away. Queueing emails that are still queued (e.g. after the app crashed) only makes sure the sender runs. Queueing starts the **sender** (a pipeline run, `send_emails`) unless it is already running.
3. **The sender** (`pipeline/services/sending.py`) takes the queued email with the earliest time, waits until then (looking again every 30 s, so emails queued meanwhile are picked up), sends it and goes on; it stops when the queue is empty. It only runs while this machine and the app are on: if it was stopped (crash, restart), the Outbox and the dashboard say so; queue the remaining emails again (or **Start the sender**) and the ones whose time passed go out right away, one after the other. `python manage.py send_emails` runs it by hand (`--once`: only what is due now).
4. **Each email** goes as text + HTML (both with the footer from Settings), from its mailbox (`From name <from address>`), with a `List-Unsubscribe` header (a reply to that address). A follow-up is sent as a reply to the email before it (`In-Reply-To`), so it lands in the same thread. After it is sent: status **Sent**, the prospect is contacted, the lead moves on the Pipeline, and the next follow-up is written for review right away (approve and queue it the same way).
5. **Failures.** A refused recipient or a server error marks that email **Failed** with the reason (Outbox, dashboard); queue it again to retry. A mailbox that refuses its login gets the error noted on it; its emails stay queued and the other mailboxes go on (the run then ends as failed, naming the mailbox: fix its password and click **Start the sender**). A mailbox removed or turned off before its email went: that email becomes Failed; queue it again and it is given another mailbox. An email the sender was in the middle of when it stopped becomes Failed on the next start: check your Sent folder before queueing it again.

Without an active mailbox (or without `MAILBOX_ENCRYPTION_KEY`) nothing is sent: queued emails wait and the dashboard says so. `SENDER_NAME` in `.env` is only the `{{sender_name}}` sign-off in templates; the From name comes from the mailbox.

## The Pipeline (leads)

The admin's **Pipeline** page shows every lead (a ready prospect) in its stage: New Lead, Ready to Send, Initial Email, Follow-up, Replied, Demo Booked, Demo Done, Won, Lost, Quota Exhausted. Filter by campaign or search; each card has a stage menu and **Move**. Every move is kept with who made it (the prospect's page, **Stage history**). `GET /api/pipeline/` and `POST /api/prospects/{id}/move-stage/` do the same.

- **Sending moves leads forward** on its own: first email approved → Ready to Send, sent → Initial Email, a follow-up sent → Follow-up. It never moves a lead out of a stage a person chose (e.g. Replied).
- **Quota Exhausted**: when a lead's sequence has no step left and its last email went out more than the reply window ago (Settings, `reply_window_days`, default 3) with no reply, it moves there when the Pipeline page or the dashboard is opened.
- **Your moves stop or resume emails.** Replied, Demo Booked, Demo Done, Won, Lost and Quota Exhausted stop the lead's sequence: emails not sent yet are cancelled (Replied also marks the latest sent email replied). Moving a stopped lead back to Initial Email or Follow-up resumes it (e.g. the "reply" was an automatic one): the replied email counts as sent again, the cancelled emails go back to review, and the next follow-up is written if it does not exist yet. Each prospect has its own sequence; colleagues at the same business are not affected.
- **Mark replied** on an email (admin or API) moves its lead to Replied.

## Tests

```sh
python manage.py test
npx deno test supabase/functions/email/handler_test.ts
```

Tests never use the `.env` database or real services: they run against a throwaway Postgres container started in Docker on port 5432 (or `TEST_DATABASE_URL` if set), the test runner removes the SMTP, LLM, scraper and Reacher settings from the environment, and emails go to Django's in-memory outbox. Reacher, the scraper container and the LLM are replaced by local fakes; one test starts the real Reacher image on a spare port, and one starts a real background run.

## Project layout

| Path | What |
|---|---|
| `emailautomation/` | Django settings and URLs |
| `pipeline/models.py` | one model per table |
| `pipeline/services/` | the pipeline steps (Google Maps, scraper container, research agent, stakeholders, Reacher, generation, follow-ups), `outreach.py` (send times, the queue, stopping and resuming), `sending.py` (the SMTP sender), `stages.py` (the lead pipeline) |
| `docker-compose.yml`, `docker/` | the local SearXNG |
| `pipeline/services/searxng.py` | web search through your SearXNG instance (decision makers) |
| `pipeline/management/commands/` | one `manage.py` command per step |
| `pipeline/admin.py`, `pipeline/templates/` | the admin, including the email review page |
| `pipeline/api/` | the REST API |
| `pipeline/review.py` | email review rules shared by the admin and the API |
| `pipeline/jobs.py` | background pipeline runs |
| `pipeline/migrations/` | the schema, including the SQL functions, triggers and row level security |
| `supabase/functions/email/` | the open-tracking edge function |
