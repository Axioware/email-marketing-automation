# Email Marketing Automation

Finds local businesses, researches and scores them, finds the decision maker and a working email, writes a personalised email for each, lets you review and approve them, sends them, and tracks opens. It is a Django project: everything is in the **Django admin** and a **REST API**, and every step is also a `manage.py` command. Data lives in PostgreSQL (Supabase).

**Using the admin day to day:** see the [user guide](docs/USER_GUIDE.md), also available in the admin under **Guide** (top right).

## Run the project

### One-time setup

```sh
cd email-marketing-automation
python -m venv .venv && source .venv/bin/activate    # skip if .venv already exists
python -m pip install -r requirements.txt
python -m playwright install chromium               # Modules 1 and 2
cp .env_example .env                                # then fill it in (see below)
python manage.py migrate                            # creates / updates every table
python manage.py createsuperuser                    # your admin login
python manage.py runserver                          # http://127.0.0.1:8000/admin/
```

Generate a `DJANGO_SECRET_KEY` for `.env` with `python -c "import secrets; print(secrets.token_urlsafe(50))"`.

**Database created before the move to Django** (with Alembic): run `python manage.py migrate --fake-initial` once instead of `migrate`. It adopts the existing tables without touching their data, converts the three remaining `json` columns to `jsonb`, re-applies the triggers and the open-tracking function, adds Django's own tables, and turns on row level security for every table (so the Supabase REST API cannot read them). The old `alembic_version` table is no longer used and can be dropped.

Also needed:

- **Docker**: email verification (Module 4) runs Reacher in a container (see "Docker setup" below).
- **Firefox and geckodriver**: contact discovery (Module 3).
- **Supabase**: only for open tracking (Module 5). Deploy the edge function and upload the footer image once (see "Open tracking").

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

Managing the container:

```sh
docker ps --filter name=reacher     # is it running?
docker logs reacher                 # what is it doing?
docker stop reacher                 # stop it (the next run starts it again)
docker rm -f reacher                # delete it (the next run recreates it from .env)
```

If you change `VERIFY_MAIL_FROM` or `VERIFY_HELO`, the next run recreates the container automatically. Port 8080 already in use? Set `REACHER_URL=http://127.0.0.1:8081` in `.env` and that port is used. Add `--no-auto-start` to manage the container yourself (command in Module 4 below).

`.env` keys (`.env_example` lists every one):

| Needed for | Keys |
|---|---|
| Everything | `DATABASE_URL`, `DJANGO_SECRET_KEY` (`DJANGO_DEBUG`, `DJANGO_ALLOWED_HOSTS` optional) |
| The API | `AUTH_TOKEN` (sent in every request's `Auth` header) |
| Research and email writing | `GROQ_API_KEY` (or `GROK_API_KEY`), otherwise `OPENAI_API_KEY` |
| Email verification | `VERIFY_MAIL_FROM`, `VERIFY_HELO` |
| Sending | `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM_EMAIL`, `SMTP_FROM_NAME` (host, port and security default to Hostinger) |

### The pipeline

Run the steps in order. Each one reads what the previous one stored, and each skips work it has already done, so it is safe to rerun. Every step can be started three ways: from the admin, through the API, or as a command.

**All at once:** open a campaign and click **Run full pipeline** (or `python manage.py run_pipeline --campaign-id 1 --fetch-limit 20`, or `POST /api/campaigns/1/run-pipeline/`). It runs steps 2-6 for that campaign's businesses, continues past partial failures, stops if a step cannot run, and never sends: emails wait for review.

| Step | Admin | Command |
|---|---|---|
| 1. Campaign | Discovery campaigns > Add | `python manage.py create_campaign` |
| 2. Find businesses | Campaign > **Fetch businesses** | `python manage.py fetch_businesses --campaign-id 1 --limit 20` |
| 2b. Fill missing details | Businesses > action **Refresh Google Maps details** | `python manage.py refresh_businesses` |
| 3. Research and score | Businesses > action **Research websites** | `python manage.py research_websites` |
| 4. Find owners | Businesses > action **Find decision makers** | `python manage.py find_stakeholders --headed` |
| 5. Verify emails | Businesses or Contacts > action **Verify emails** | `python manage.py verify_emails` |
| 6. Write emails | Prospects or Businesses > action **Generate outreach emails** (choose an email prompt) | `python manage.py generate_emails [--email-prompt-id N]` |
| 7. Review | Emails: edit, Approve, Reject, Regenerate | - |
| 8. Send | Email > **Send now**, or Emails > action **Send selected** | `python manage.py send_emails --send` |

Every command has `--help`. Before running one on real data, try it with `--limit 1 --dry-run`, which shows what it would do and saves nothing:

```sh
python manage.py verify_emails --limit 2 --dry-run
python manage.py generate_emails --limit 1 --dry-run
python manage.py send_emails --limit 1          # preview only; add --send to actually send
```

### Pipeline runs (background jobs)

Steps started from the admin or the API run as **pipeline runs**: a background process executes the same command and streams its output into the run (Pipeline runs in the admin). An admin action first opens the run form pre-filled with the command and the selected IDs, so you can adjust the arguments (e.g. add `--dry-run`) before clicking Save. The run page refreshes itself while it works and has a **Stop this run** button; **Run again** repeats it. Runs keep going if the web server restarts; a run whose process died is marked failed.

Notes:

- **Reacher starts itself.** The first verification run creates and configures the Docker container. It needs outbound port 25.
- **Step 4 opens Firefox.** Google may show a CAPTCHA; `--headed` lets you see the window. DuckDuckGo is the fallback. In a background run, a consent prompt waits up to 5 minutes for you to finish it in the window.
- **Opens are counted only after an email is sent**, through the deployed edge function.

## REST API

Everything in the admin is also in the API at `/api/`. Interactive docs (Swagger) are at `/api/docs/` and the OpenAPI schema at `/api/schema/` (both need an admin login; in the docs click **Authorize** and enter the key to try requests).

**Every request must send the `Auth` header** with the value of `AUTH_TOKEN` from `.env`; anything else gets `401`. With `AUTH_TOKEN` empty the API refuses every request. Generate a key with `python -c "import secrets; print(secrets.token_urlsafe(32))"` and restart the server after changing it. Optionally also send `Authorization: Token <user token>` (Admin > Auth Token > Tokens) so runs you start are attributed to that user.

| Endpoint | What |
|---|---|
| `/api/campaigns/` | list, create, edit, delete; `POST /api/campaigns/{id}/fetch-businesses/` `{"limit": 20}` |
| `/api/campaign-prompts/`, `/api/email-prompts/` | the prompts emails are written with; a campaign links to one campaign prompt (`campaign_prompt`), which has many email prompts |
| `/api/businesses/`, `/api/business-sources/`, `/api/website-profiles/` | Modules 1-2 data, with filters (`?city=`, `?website_profile__qualification_score__gte=50`), `?search=` and `?ordering=` |
| `/api/contacts/`, `/api/prospects/` | Modules 3-4 data (e.g. `PATCH` a prospect's `do_not_contact`) |
| `/api/emails/` | list, view, `PATCH` `subject`/`body` (goes back to review), delete |
| `/api/emails/{id}/approve/`, `reject/` `{"note"}`, `reopen/`, `regenerate/`, `send/` `{"confirm": "<recipient>"}` | review actions; `409` when not allowed in the email's current status |
| `/api/emails/{id}/preview/`, `/api/emails/bulk/` `{"action": "approve", "ids": [...]}`, `/api/emails/stats/` | preview HTML, bulk review, counts and open rate |
| `/api/email-opens/` | recorded opens |
| `/api/runs/` | start a pipeline step, list and follow runs; `GET /api/runs/commands/` lists commands and options; `POST /api/runs/{id}/cancel/` |
| `/api/stats/` | counts across the whole pipeline |

Start a step (e.g. from a cron job) and follow it:

```sh
curl -X POST -H "Auth: $AUTH_TOKEN" -H "Content-Type: application/json" \
  -d '{"command": "verify_emails", "options": {"limit": 50, "primary_only": true}}' \
  http://127.0.0.1:8000/api/runs/
curl -H "Auth: $AUTH_TOKEN" http://127.0.0.1:8000/api/runs/1/     # status, exit_code, output
```

`options` uses the command's option names (`--dry-run` is `"dry_run": true`, repeatable options take a list: `"business_id": [1, 2]`); raw `"arguments": ["--limit", "5"]` works too. Invalid options are refused with `400` before anything starts. A cron job can also run the command directly: `cd /path/to/project && .venv/bin/python manage.py verify_emails --limit 50`.

## Create a discovery campaign

In the admin: Discovery campaigns > Add (locations and search terms are comma-separated). From the terminal:

```sh
python manage.py create_campaign --name "Lahore dentists" --country Pakistan --locations "Lahore, DHA" --search-terms "dental clinic, dentist"
python manage.py create_campaign      # or answer the prompts
```

## Fetch businesses from Google Maps

Needs Chromium (`python -m playwright install chromium`). From the admin: select a campaign, action **Fetch businesses from Google Maps** (or the **Fetch businesses** button on the campaign). From the terminal:

```sh
python manage.py fetch_businesses --campaign-id 1 --limit 20
```

At a terminal, `--campaign-id` and `--limit` are asked for when omitted; in a background run they are required. The fetcher searches that campaign's terms and locations, stores up to the requested number of new businesses, and leaves unavailable Maps fields as `NULL`. Use `--headed` if Google requires manual consent. Automated Google Maps access may be restricted by its terms; for production use, prefer the official Places API.

Each new business is saved with a `business_sources` record containing the unnormalized Google Maps payload in PostgreSQL `JSONB`.

## Research business websites

Set an LLM key in `.env`: `GROQ_API_KEY` (or `GROK_API_KEY`; used when present; optionally `GROQ_MODEL`, default `openai/gpt-oss-120b`) or, as the fallback, `OPENAI_API_KEY` (optionally `OPENAI_MODEL`, default `gpt-5.4-mini`). The script prints which provider and model it uses. Groq is called through its OpenAI-compatible API, so no extra package is needed. The agent needs strict JSON-schema output, so a different `GROQ_MODEL` must support it. Chromium is needed (`python -m playwright install chromium`). Jina Reader's public endpoint is used by default for cleaned page text; it requires no API key and is limited to 20 requests per 60 seconds. If Jina fails, the worker falls back to local extraction. Use `--local-only` to keep page content local.

```sh
python manage.py research_websites --limit 5
```

The script processes businesses with websites that do not yet have a completed profile. Failed profiles are skipped on normal runs; pass `--retry-failed` to retry them. Use `--limit N` for a bounded batch or `--business-id ID` (repeatable) for specific businesses; in the admin, select businesses and use the **Research websites** action. It asks the agent to SCORE, SCRAPE, or EXIT after each page, enforces a maximum of five unique page visits per business, and stores compact page summaries/findings, discovered links, emails, and URLs in `business_website_profiles`. Only the current page's full cleaned text is sent on each agent turn; raw HTML is not saved.

## Find decision makers and contact emails (Module 3)

Needs Firefox and [geckodriver](https://github.com/mozilla/geckodriver/releases) installed locally; no API keys or paid services. In the admin: select businesses, action **Find decision makers**.

```sh
python manage.py find_stakeholders --limit 5 --headed
```

For each qualified business (completed profile, score >= `--min-score`, default 50) it runs a waterfall:

1. **Module 2 data:** scraped page text, findings, emails and LinkedIn links are mined for people and job titles.
2. **Rendered site crawl** (only if no target-role person yet; `--max-pages`, default 6): team/about/contact pages are opened in Firefox so JavaScript-built pages are read as a visitor sees them. Pages Module 2 already read are skipped.
3. **Role ranking:** people are classified with `config/target_roles_dental.json` (target roles, fallback roles, excluded roles). Swap the file with `--roles-file` for other niches.
4. **Web search** (only if still no target-role person, at most `--max-searches` per business, default 3): Google AI Overview first, DuckDuckGo as fallback. A result is accepted only if it contains the business's phone number, or its name plus location/domain.
5. **Email:** an email found publicly is stored with `email_source` `website` or `search`; otherwise a pattern guess is stored as `inferred`. All other guesses are kept in `candidate_emails`. Every email is `unverified`; verification is Module 4.
6. **Selection:** the highest-ranked role becomes the primary contact (`is_primary`); other target-role people are kept as secondary contacts. `source_urls` and `discovery_reasoning` record the evidence.

Options: `--business-id ID` (repeatable), `--redo` (reprocess and replace this module's contacts), `--no-search`, `--no-crawl`, `--dry-run`, `--delay SECONDS`, `--profile PATH` (copy of a Firefox profile, e.g. one signed in to Google, to reduce CAPTCHAs). Automated search may be restricted by Google's and DuckDuckGo's terms.

## Verify contact emails and build prospects (Module 4)

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

Only `ready` prospects are ever emailed: generation and sending skip everything else. A rerun skips addresses already stored as deliverable, undeliverable or risky and checks `unknown` ones again; `--recheck` checks everything. Rechecking refreshes the verification columns; `outreach_priority`, `outreach_facts`, `research_summary`, `do_not_contact` and `last_contacted_at` belong to later stages and are never overwritten, and a prospect with `do_not_contact` set or one a later stage has moved on (e.g. `contacted`) keeps its `outreach_status`.

If a domain is a catch-all, or its mail server cannot be reached, the remaining addresses on that domain are not probed (they would get the same answer) and are recorded with that outcome (verdict `catch_all`, `probed: false`). Catch-all domains are remembered across runs, so new addresses on them are recorded the same way without a probe; `--probe-all` checks every address anyway. The run stops early if Reacher becomes unreachable, or if it cannot open an SMTP connection to 3 servers in a row (port 25 blocked); results so far are kept.

```sh
python manage.py verify_emails --limit 5 --dry-run --report out.csv
```

Options: `--business-id`, `--contact-id` (both repeatable), `--limit` (contacts), `--primary-only`, `--min-score`, `--recheck`, `--max-candidates`, `--probe-all`, `--delay`, `--reacher-timeout`, `--mail-from`, `--helo`, `--no-auto-start`, `--report out.csv`, `--dry-run`.

## Generate outreach emails and track opens (Module 5)

### Generate emails

`python manage.py generate_emails` (admin: select prospects or businesses, action **Generate outreach emails**) writes one email per prospect that is ready for outreach (`email_status = 'deliverable'`, `outreach_status = 'ready'`, not `do_not_contact`) and stores it in `emails` with status `in_review`. **Nothing is sent.** The model sees only what earlier modules stored (business, contact name and title, qualification reasons, `outreach_facts`, `research_summary`). It uses Groq when `GROQ_API_KEY`/`GROK_API_KEY` is set, otherwise OpenAI (`pipeline/services/llm.py`).

The model's instructions are the campaign's **campaign prompt** followed by an **email prompt** (tables `campaign_prompts` and `email_prompts`, edited in the admin; a campaign has one campaign prompt, which has many email prompts). The built-in default (`DEFAULT_CAMPAIGN_PROMPT` + `DEFAULT_EMAIL_PROMPT` in `pipeline/services/generation.py`, saved as the first prompts) writes first-touch cold emails for Axioware: one specific observation about the business, one problem it likely has (missed calls, after-hours calls, no-shows, reception workload), Axioware's fitting offer (Ava, the AI dental receptionist, for clinics; voice agents or chatbots otherwise), and one low-pressure call to action (the live demo at axioware.tech/dental-agent or a 15-minute call). It must use only facts from the input and the Axioware facts in the prompt: no invented statistics, testimonials or prices, no claims about the contact's role, no mention of how they were found. The sign-off uses `SMTP_FROM_NAME`. Alongside the business and contact, the model gets `website_findings`: facts Module 2 noted on the business's own website (services, hours, booking, reviews), with phone numbers, email addresses and notes about the research itself removed.

```sh
python manage.py generate_emails --limit 1 --dry-run
```

Options: `--prospect-id`, `--business-id` (both repeatable), `--email-prompt-id` (default: each campaign's default email prompt), `--limit`, `--regenerate` (rewrites emails in review or rejected, keeping their tracking token; approved and sent emails are never modified), `--dry-run`.

### Open tracking

Each email gets a random tracking token (`secrets.token_urlsafe(32)`) that contains nothing about the recipient. The HTML footer image points at the Supabase Edge Function `email`:

```
https://<project>.supabase.co/functions/v1/email/footer/{tracking_token}
```

When an email client loads the image, the function calls the database function `record_email_open`, which, in one atomic statement, sets `first_opened_at` (once), `last_opened_at`, increments `open_count`, changes `status` from `sent` to `opened`, and stores an `email_open_events` row (time, user agent). Only emails with `sent_at` set count, so previews of unsent emails are ignored. The function always returns the footer image, even for unknown tokens, and asks clients not to cache it. `emails` and `email_open_events` have row level security on with no policies, so only the service role (the edge function and this backend) can read them.

The tracking base URL is derived from a Supabase `DATABASE_URL`; set `EMAIL_TRACKING_BASE_URL` to override it.

**Deploy (once):**

1. Apply the migrations (`python manage.py migrate`). They also create the public Storage bucket `email-assets`.
2. Upload `assets/email-footer.png` to Storage as `email-assets/footer.png` (Dashboard -> Storage). It is the Axioware footer (logo, tagline, site, email, city). The text footer in emails (`FOOTER_TEXT` in `pipeline/services/generation.py`) carries the same details for clients that block images. If it is missing, the function returns a transparent 1x1 image so emails never show a broken image. Other names: set the `FOOTER_BUCKET` / `FOOTER_PATH` function secrets.
3. Deploy the function without JWT verification (email clients send no auth header; `supabase/config.toml` sets this too):

   ```sh
   npx supabase login
   npx supabase functions deploy email --project-ref <project-ref> --no-verify-jwt
   ```

`SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` are provided to the function by Supabase automatically.

Opens are rate limited in the database: at most 10 recorded opens per email per minute (`record_email_open` locks the email row, so concurrent requests are counted exactly). Over the limit the image is still returned but no open is recorded. The function also keeps a per-IP limit (60/min) in memory, but Supabase spreads requests across many function instances, so that one is only a light extra guard.

**Limitations:** an open means the image was requested, not that a person read the email. Clients that block images miss opens, image proxies and caches can hide repeat opens, and security scanners can load the image before the recipient does. Treat it as an engagement signal.

### Review in the admin

Every generated email starts in `in_review`. Review them in the admin under **Emails**:

- **List**: counts per status and the open rate at the top (click one to filter), search by business, recipient or subject, and the actions **Approve selected**, **Reject selected** and **Send selected approved emails**.
- **Email page**: edit the subject and body (the footer and tracking image are added automatically), see a preview (which loads the footer directly, so viewing never counts as an open), and see what earlier modules found about the business (website findings, qualification reasons). Actions: **Approve** (then jumps to the next email in review), **Reject** with an optional note, **Back to review**, **Regenerate with AI**, and **Send now** for approved emails (type the recipient's address to confirm; uses the `SMTP_*` settings in `.env`).
- Editing an approved email sends it back to review. Emails with `PLACEHOLDER` text cannot be approved. Sent emails are read-only and show their opens.

Statuses: `in_review` -> `approved` -> `sending` -> `sent` -> `opened`, plus `rejected` and `failed`. Only `approved` emails are ever sent, from the admin, the API or `send_emails`.

### Send

`python manage.py send_emails` sends **approved** emails in bulk through SMTP (Hostinger: `smtp.hostinger.com`, port 465, SSL) using the `SMTP_*` settings in `.env`. **Without `--send` it only previews.**

```sh
python manage.py send_emails --limit 1          # preview
python manage.py send_emails --limit 1 --send   # send
```

- Only approved emails whose prospect is still ready (deliverable, `outreach_status = 'ready'`, not `do_not_contact`, same address) are sent.
- Each email is claimed (`approved` -> `sending`) before sending, so two runs never send the same email. On success it becomes `sent` (`sent_at`, `message_id`, `sent_from`) and the prospect becomes `contacted` with `last_contacted_at`; open tracking counts from then on.
- A rejected recipient or content marks that email `failed` (`send_error`). A login, connection or temporary error puts the email back to `approved` and stops the run. An email left in `sending` (the process died mid-send) is reported and never retried automatically, since it may have been delivered.
- Placeholders: `--send` refuses while `SYSTEM_PROMPT`/`FOOTER_TEXT` in `pipeline/services/generation.py` are placeholders, and skips any email containing `PLACEHOLDER`, unless `--allow-placeholders` is given.
- Each message has a plain-text and HTML part, the footer (`FOOTER_TEXT`), and a `List-Unsubscribe` header pointing at `SMTP_REPLY_TO` (or the sender).

Options: `--email-id`, `--business-id` (both repeatable), `--limit`, `--delay` (seconds between sends, default 20), `--timeout`, `--allow-placeholders`.

### Placeholders to fill

| What | Where |
|---|---|
| Footer image | upload `assets/email-footer.png` to Supabase Storage as `email-assets/footer.png` |
| SMTP account and sender | `SMTP_*` in `.env` |

## Tests

```sh
python manage.py test
npx deno test supabase/functions/email/handler_test.ts
```

Tests never use the `.env` database: they run against a throwaway Postgres container started in Docker (or `TEST_DATABASE_URL` if set). Reacher, SMTP and the LLM are replaced by local fakes; one test starts the real Reacher image on a spare port, and one starts a real background run.

## Project layout

| Path | What |
|---|---|
| `emailautomation/` | Django settings and URLs |
| `pipeline/models.py` | one model per table |
| `pipeline/services/` | the pipeline steps (Google Maps, research agent, stakeholders, Reacher, generation, SMTP) |
| `pipeline/management/commands/` | one `manage.py` command per step |
| `pipeline/admin.py`, `pipeline/templates/` | the admin, including the email review page |
| `pipeline/api/` | the REST API |
| `pipeline/review.py` | email review rules shared by the admin and the API |
| `pipeline/jobs.py` | background pipeline runs |
| `pipeline/migrations/` | the schema, including the SQL functions, triggers and row level security |
| `supabase/functions/email/` | the open-tracking edge function |
