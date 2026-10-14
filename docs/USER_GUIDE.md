# User guide

This tool finds local businesses, works out who runs each one and their email address, writes a personal cold email (and follow-ups) for each person, and lets you review and approve every email. Approved emails are scheduled and **GoHighLevel sends them**.

You do everything from the **admin** in your browser. Nothing is ever sent without your approval.

## 1. Start the app and log in

Open a terminal in the project folder and run:

```sh
source .venv/bin/activate
python manage.py runserver
```

Then open **http://127.0.0.1:8000/admin/** and log in. Keep the terminal open while you work; press `Ctrl+C` in it to stop the app.

> **New team member?** An admin creates your login under **Users > Add user**. Tick **Staff status** and **Superuser status** so you can use every page.

## 2. The dashboard

The home page shows the whole pipeline at a glance:

- **The counters** follow a business from left to right: found on Google Maps, website researched, qualified, contact found, ready to email, email in review, scheduled, sent. Click any counter to see those records.
- **Get leads** lists your campaigns with two buttons each, one per way of finding businesses on Google Maps: **Built-in browser** and **Scraper container**. It also shows whether the scraper container is running. See [Step 2](#step-2-fetch-businesses).
- **What to do next** lists the jobs that are waiting, with a button for each, e.g. "3 emails are waiting for your review: Start reviewing".
- **Recent pipeline runs** shows the jobs you started and whether they finished.
- **Yellow warnings** at the top mean something in the settings is missing (for example the AI key). See [Settings](#9-settings).

The left sidebar lists the pages in pipeline order (its **Jump to a page** box only narrows this list of pages, it does not filter records): **Campaigns > Businesses > Website research > Contacts > Prospects > Emails**, then **Pipeline runs**, **Email opens** and **Google Maps listings**.

## 3. How the pipeline works

| Step | What happens | Where |
|---|---|---|
| 1. Campaign | You say what to search for and where | Campaigns |
| 2. Find businesses | Google Maps is searched and each business is saved | Campaign > **Fetch businesses** |
| 3. Research | An AI reads each business's website and scores it 0-100 | Businesses > **Research websites** |
| 4. Find the decision maker | The owner or manager is found, and likely email addresses are guessed | Businesses > **Find decision makers** |
| 5. Verify emails | Each guessed address is checked and saved with its result; only real ones are ready to email | Businesses > **Verify contact emails** |
| 3b. Review low scores | Businesses below the campaign's minimum score (or not researchable) wait for you | Businesses > **Needs your review** |
| 5b. Review unverified addresses | Addresses verification could not confirm wait for you | Prospects > Needs review |
| 6. Write emails | The AI writes one email per prospect; each becomes a GoHighLevel contact | Prospects > **Generate outreach emails** |
| 7. Review | You read, edit, approve or reject each email; approving schedules it | Emails |
| 8. Send | GoHighLevel sends each email at its time | Automatic |
| 9. Follow-ups | Written for review after each email is sent, sent the same way | Emails |

Each step only processes what still needs it, so it is always safe to run a step again.

### Run the whole pipeline at once

Open a campaign and click **Run full pipeline** (top right), or tick it on the Campaigns list and use the action **Run the full pipeline**. The run form opens with `--campaign-id N --fetch-limit 20`; change the number of businesses to fetch and click **Start run**.

It runs steps 2 to 6 one after another for that campaign's businesses: find businesses, research websites, find decision makers, verify emails, write emails. You can follow every step on the run page. It **never sends anything**: the new emails wait in **Emails > In review** for you to approve (step 7).

- Each step only does what is still needed, so running the pipeline again continues where it left off.
- If a few items fail (one website is down, for example), the pipeline carries on with the next step.
- If a step cannot run at all (no LLM key, Docker/Reacher not running, Google CAPTCHA), the pipeline stops and tells you why. Everything done so far is kept; fix the problem and run it again.
- Options: `--source scraper_api` (or `browser`) fetches through that source instead of the campaign's own, `--fetch-limit 0` skips fetching new businesses, `--skip verify` skips a step (fetch, research, stakeholders, verify, generate), `--min-score 60`, `--email-prompt-id N`, `--headed`.

## 4. Step by step

### Step 1: Create a campaign

1. Sidebar > **Campaigns** > **Add campaign**.
2. Fill in:
   - **Name**: anything you will recognise, e.g. "Lahore dentists".
   - **Target country**: one country, e.g. Pakistan.
   - **Target locations**: cities or areas, separated by commas, e.g. `Lahore, DHA, Gulberg`.
   - **Search terms**: what you would type into Google Maps, separated by commas, e.g. `dental clinic, dentist`.
   - **Lead source** (under Leads): how businesses are found on Google Maps: **Built-in browser** (default) or **Scraper container**. You can still pick the other one for a single fetch.
   - **Minimum score** (under Leads, default 50): businesses scoring at least this go straight on to finding decision makers; the rest wait for you to approve or reject them.
   - **Campaign prompt** (under Prompts): what makes a good prospect; the AI scores each business's website against it. Pick one, or click **+** to write a new one. Several campaigns can share one. Without one, the campaign's businesses are not researched.
   - **Initial-email prompts** (under Prompts): tick one or more. With several, prospects are split equally between them (an A/B test) and each person keeps their prompt.
   - **Sending** (optional): the gap between two sends (default 2 minutes fixed plus a random 3-5 minutes), the **daily cap** (default 40), and a **send window** (e.g. 09:00 to 18:00) with its time zone. See [Sending](#step-8-sending-through-gohighlevel).
   - **Follow-ups** table (bottom): one row per follow-up with its days and its prompt. See [Follow-ups](#follow-ups).
3. Click **Save**.

Every search term is searched in every location, so 2 terms and 3 locations means 6 searches. The campaign's **Status** (Pending, Running, Completed, Failed) is set automatically when businesses are fetched; you cannot edit it.

### Step 2: Fetch businesses

There are two ways to find businesses, and both save exactly the same details:

| | Built-in browser | Scraper container |
|---|---|---|
| What does the browsing | This app's own Chromium | The Google Maps scraper running in Docker (port 8001) |
| Needs | Chromium installed | The container running (`docker compose up -d` in its folder) |
| Speed | One place at a time | Several places at once (`--concurrency`, default 5) |
| Google consent page | `--headed` lets you click through it | Handled inside the container |

1. On the dashboard's **Get leads** panel, click **Built-in browser** or **Scraper container** next to the campaign. Or open the campaign and click **Fetch with built-in browser** / **Fetch with scraper container** (top right).
2. The run form opens with `--limit 20` and the source. Change the number to fetch more or fewer businesses.
3. Click **Start run** and watch the progress (see [Pipeline runs](#5-pipeline-runs)).

The campaign's **Lead source** decides which one is used when nothing else is chosen, e.g. by **Run full pipeline** or a scheduled job. With the scraper container, each search asks for at most 40 places so it finishes within the container's 5-minute limit; a search that runs out of time is skipped and the run says so.

The campaign's status changes to **Completed** when it finishes. The businesses appear under **Businesses**.

**No duplicates, even across campaigns.** A business that is already saved (in this campaign or any other) is skipped, and the run says which campaign has it, e.g. `Skipped DH Dental Clinic: already in campaign "Lahore clinics"`. So one business is researched once and gets one email sequence, even when two campaigns search overlapping areas.

Each business gets its category, Google rating, number of reviews, opening hours, phone, website and address, and its city, state/province and postal code are worked out from the address (no AI is used for any of this).

**Missing a category or review count?** Google sometimes shows a reduced page without them, especially after many pages in a row. On **Businesses**, tick the businesses (or none, for all that are missing something), choose **Refresh Google Maps details (category, reviews, city)** and start the run. It revisits each business's Google Maps page, retries pages Google reduced, and fills in what was missing; it never changes a business's website. The dashboard shows a **Refresh Google Maps details** button while any business has no review count. A business with no reviews on Google keeps an empty review count.

### Finding businesses: filters

On **Businesses**, click one of the **quick filter boxes** above the list (Not researched, Needs your review, Qualified, Score 80+, 100+ reviews, Rated 4.5+, No contacts yet, Ready to email, Email sent), or type in the **search box**: it matches name, category, city, address, phone and website. The **Filter** panel on the right narrows the list further, and filters combine: by campaign, qualification (needs your review, qualified, approved, rejected), website, website research (not researched, researched, failed), qualification score (80+, 50-79, below 50), decision-maker search, contacts, prospects (ready, needs review, contacted, none), emails (in review, approved, sent, opened, none), Google rating, number of Google reviews, category, country and city. Select the filtered rows and run any action on them, e.g. "score 80 and above" + "Not searched yet" → **Find decision makers**.

### Step 3: Research websites

1. Sidebar > **Businesses**. Tick the businesses to research. To select all of them, tick the box in the table header, then click "Select all".
2. In the **Action** menu choose **Research websites (Module 2)** and click **Go**.
3. Click **Start run**.

For each business the AI reads up to 5 pages of its website and scores it 0-100 against its campaign's **campaign prompt** (the qualification criteria; see [Prompts](#prompts)), with reasons. Businesses whose campaign has no campaign prompt are skipped and the run says so. The score and status show in the Businesses list; click a business and then its **Website research** link to see what the AI read and why it scored it that way.

**Below the minimum score?** Businesses scoring under their campaign's minimum score, those whose research failed and those without a website wait for you: on **Businesses** click **Needs your review**, tick them, and choose **Approve** (they go on to finding decision makers) or **Reject** (they never do). The dashboard shows how many are waiting.

Businesses you select are researched even if they were researched before (the new score replaces the old one). The **Research websites** button on the dashboard only does businesses that have not been researched yet. Businesses without a website are skipped. If research fails for one (the site was down, for example), run it again later; **Retry failed** on the dashboard does this.

### Step 4: Find decision makers

1. Sidebar > **Businesses**, tick the businesses (only qualified ones are processed: a score of at least their campaign's minimum, or approved by you; rejected ones never).
2. Action **Find decision makers (Module 3)** > **Go** > **Start run**.

This opens Firefox in the background. It looks at the business's own pages first, then Google's AI answer, then DuckDuckGo. The people it finds are listed under **Contacts**, with their job title and every email address that might be theirs (for example `ali@clinic.pk`, `ali.khan@clinic.pk`).

> **Tip:** Google sometimes shows a "consent" or CAPTCHA page. Add `--headed` to the arguments to see the Firefox window and click through it yourself. The run waits up to 5 minutes for you.

### Step 5: Verify emails

1. Sidebar > **Businesses** (or **Contacts**), tick the rows.
2. Action **Verify contact emails (Module 4)** > **Go** > **Start run**.

Each address is checked with the mail server, **without sending anything**, and **every address is saved under Prospects** with its result and a **verdict** that says why:

| Result | Verdicts you may see | Outreach status |
|---|---|---|
| Deliverable | Deliverable: the mailbox exists | **Ready**: it gets an email in step 6 |
| Undeliverable | Mailbox not found, Mailbox disabled, Domain has no mail server, Invalid address | **Rejected**: never emailed |
| Risky | Catch-all domain (the server accepts any address, so nobody can tell if this one is real), Inbox full, Disposable address | **Needs review**: not emailed |

**Catch-all domains:** when one address on a domain turns out to be catch-all, the other addresses on that domain are not checked at all; they are all saved as **Catch-all domain** (their details say "not checked"). The domain is remembered, so addresses on it found later are marked the same way without checking. Add `--recheck` to check them again.
| Unknown | Mail server unreachable, Mail server refused the check, Temporary failure (greylisting), Check failed | **Needs review**: checked again on the next run |

On the Prospects list, use the **By email status** and **By verdict** filters on the right, e.g. to see every catch-all address. Hover over a verdict to see the reason in words; open a prospect to see the details Reacher found. Only **Ready** prospects are ever emailed.

> **Sure a risky or unknown address is real?** On **Prospects**, filter **Needs review**, tick it and choose **Approve**: it gets emails and becomes a GoHighLevel contact like a verified one. **Reject** means it is never emailed. Undeliverable addresses cannot be approved.

### Step 6: Generate emails

1. Sidebar > **Prospects** (or **Businesses**), tick the rows, choose **Generate outreach emails (Module 5)** and click **Go**. To write emails for every ready prospect, click **Generate emails** on the dashboard instead.
2. The **Generate emails** page opens:
   - **Email prompt**: leave it on "Each campaign's initial-email prompts (A/B split)", or pick one prompt for all of them. Click **+** to write a new email prompt or the pencil to edit the selected one, without leaving the page.
   - **Rewrite emails that are in review or rejected**: tick to replace those drafts. Approved and sent emails are never changed.
   - **Limit** and **Dry run** work like everywhere else.
3. Click **Start generating**.

The AI writes one short email per prospect, based only on what was found about the business. Every new email starts as **In review**, and records which email prompt wrote it. Nothing is sent until you approve it.

### Prompts

Both kinds of prompt are edited in the admin; nothing is built into the code. Campaigns choose their prompts, and several campaigns can share a prompt.

- **Campaign prompts** (sidebar) **qualify businesses**. They describe what you sell, what a good and a poor prospect look like, and what each score range means. In step 3 the AI reads each business's website and scores it 0-100 against its campaign's campaign prompt. The AI receives **exactly what you write** there. Only the shape of its answer (read another page, give the score with reasons, or stop) is set by the app; expand **Answer format (set by the app)** on the prompt's page to see it. An output format you write in the prompt is overridden by it.
- **Email prompts** (sidebar) **write emails**. Each has a **Kind**: **Initial email** or **Follow-up**. Each is complete on its own: who we are and what we offer, the rules, structure, call to action, length and style. The AI receives exactly that text, plus the facts about the prospect (follow-ups also get the emails already sent). A campaign picks one or more initial-email prompts (several = A/B test, split equally) and one follow-up prompt per follow-up step. An email prompt's page shows which campaigns use it.

- New prompts start empty. The campaign page warns when it has no campaign prompt, no initial-email prompt, or a follow-up step without a prompt.
- Every email prompt should ask for a subject and a body: the app reads the AI's answer as those two fields.

### Step 7: Review emails

Sidebar > **Emails**. The boxes at the top show how many emails are in each status; click **In review** to see the ones waiting for you. Click a subject to open it.

The email page has three parts:

1. **Review** (top left): the buttons, plus what we know about the business (city, website, rating, findings from its website, why it qualified).
2. **Preview** (top right): exactly what the recipient will see, including the Axioware footer. Viewing it never counts as an open.
3. **Content** (below): the subject and body you can edit.

What you can do:

| Button | What it does |
|---|---|
| **Approve** | Approves and **schedules** it (the page then shows its send time), and jumps to the next email in review |
| **Reject** | Puts it aside; add a reason so you remember why |
| **Regenerate with AI** | Writes a new version (it stays in review). The menu next to it lets you pick a different email prompt; by default it uses the same one |
| **Back to review** | Undoes Approve or Reject; a scheduled email loses its send time |
| **Save** | Saves your edits to the subject or body |

Editing:

- Change the **Subject** or **Body**, then click **Save** at the bottom of the page.
- Write only the message and your sign-off. The footer (company details and unsubscribe line) and the tracking image are added automatically, so do not add them yourself.
- Saving an approved email sends it back to **In review**, so every email is approved in its final form.
- **Next in review** (top of the page) moves through the queue without approving.

To approve or reject many emails at once: on the Emails list tick them, choose **Approve selected emails** or **Reject selected emails** in the Action menu, and click **Go**. Emails that still contain the word `PLACEHOLDER` cannot be approved.

### Step 8: Sending (through GoHighLevel)

GoHighLevel sends the emails from your connected mailbox; this app decides what and when.

- **Contacts:** every prospect ready to email (verified, or approved by you) is created in GoHighLevel with the tags `outreach-app` and `campaign-<name>`. Your GHL workflow turns them into **New Lead** opportunities.
- **Approve = schedule.** Each approved email gets a send time: the campaign's previous one plus the gap (default 2 minutes plus a random 3-5 minutes), only inside the send window, at most the daily cap per day. A follow-up never goes before its days after the previous email. Its contact gets **Email Due At** (the date) and, for an initial email, **Initial Email Type** (the prompt's name).
- **At the send time** the app writes the email into the contact (**Email Subject**, **Email Body**, **Follow-up Count**), and the GHL workflow sends it at once and reports back. The email becomes **Sent**.
- **Changed your mind?** Edit, reject or send a scheduled email back to review before its time: it loses its slot and is not sent.
- **Replies:** when someone replies (or you move their opportunity by hand), GHL tells the app: that person's sequence stops and anything not sent yet is cancelled. Other people at the same business are not affected. **Mark replied** in the app does the same.

Statuses: In review → Approved → **Scheduled** → **Handed to GoHighLevel** → **Sent** (→ Replied). The dashboard warns if GoHighLevel is not set up, if an email was handed over 30 minutes ago and never reported sent (check the GHL workflow), or if a GHL update failed (retried every minute).

### Follow-ups

A campaign can have follow-up emails for people who did not reply.

**1. Set the steps.** Open the campaign. In the **Follow-ups** table at the bottom add one row per follow-up: **Sequence** (1, 2, ...), **Days after previous** (e.g. 3, then 7) and **Email prompt** (a follow-up prompt). The first email is step 0.

**2. Written right after the previous email is sent.** As soon as GoHighLevel reports an email sent, the next follow-up is written for review (automatically every 10 minutes, or the **Write follow-ups** button on the dashboard). You have the days in between to review it. The AI sees the emails already sent.

**3. Review and approve** like first emails. Approving schedules it for no earlier than the previous email's send time plus its days.

**4. The end.** After the last follow-up is sent, the contact's **Follow-up Count** is emptied, which tells GoHighLevel the sequence is over (Quota Exhausted). A follow-up step without a prompt also ends the sequence there.

No follow-up is written once the person replied, their sequence was stopped, or they are do-not-contact.

**Results:** the dashboard's **Results by step and initial-email prompt (A/B)** table shows, for the first email and each follow-up, per initial-email prompt: emails sent, replies and reply rate.

## 5. Pipeline runs

Steps 2-6 can take minutes, so they run in the background as **pipeline runs**.

- **Starting:** an action opens the run form with the command and the selected records filled in. You can edit the **Arguments** before clicking **Start run**. All available options for each step are listed under the form.
- **Watching:** the run page shows the output as it happens and refreshes every few seconds. You can leave the page and come back; the run continues.
- **Stopping:** **Stop this run** stops it within a few seconds. Items already finished are kept; the interrupted one is picked up by the next run.
- **Repeating:** **Run again** opens the form with the same settings.
- **Status:** Queued, Running, Succeeded, Failed (the output explains why) or Cancelled.

Useful arguments:

| Argument | Works with | Meaning |
|---|---|---|
| `--dry-run` | most steps | Show what would happen, save nothing |
| `--limit 5` | all steps | Process at most 5 items |
| `--headed` | Fetch, Research, Find decision makers | Show the browser window |
| `--source scraper_api` | Fetch, Run full pipeline | Find businesses through the scraper container (`browser` for the built-in one); default: the campaign's lead source |
| `--concurrency 3` | Fetch (scraper container) | Places the container opens at once (1-20, default 5; lower if Google starts blocking) |
| `--all` | Refresh Google Maps details | Refresh every business, not only those missing details |
| `--address-only` | Refresh Google Maps details | Only fill city, state and postal code from the address (no browser) |
| `--retry-failed` | Research | Also retry businesses whose research failed |
| `--redo` | Research | Research again businesses that were already researched |
| `--redo` | Find decision makers | Search again for businesses already done |
| `--min-score 60` | Find decision makers, Verify | Only businesses scoring 60 or more |
| `--recheck` | Verify | Check addresses that were already checked |
| `--primary-only` | Verify | Only the main contact of each business |
| `--regenerate` | Generate emails | Rewrite emails that are in review or rejected |
| `--campaign-id N` | Write due follow-ups | Only that campaign's follow-ups |

## 6. Managing prospects

- **Do not contact:** on the Prospects list, tick them and choose **Mark do not contact**. They will never be emailed, and verification never changes them back. Use this when someone replies "unsubscribe". **Clear do not contact** undoes it.
- **Add a contact by hand:** **Contacts > Add contact**, then run **Verify** for that contact. Hand-added contacts are kept when Find decision makers runs again.
- **Status meanings:** Ready (can be emailed), Needs review (risky or unknown address), Rejected (undeliverable), Contacted (an email was sent). The **Verdict** column says exactly why an address is not ready.

## 7. Open tracking (off)

Open tracking is **switched off**: emails are sent without any image, so nobody's opens are recorded. The email still ends with the Axioware footer text (company details and the unsubscribe line).

What this means in the admin:
- The open rate shows as "off" on the dashboard and the Emails list, and emails stay **Sent** (they never become Opened).

Tracking can be switched back on later by a developer (see the README); everything it needs is still in place.

## 8. Automating with the API

Everything in the admin is also available through the API, so a scheduled job (cron) can run steps automatically.

1. Every API request must include the header `Auth` with the API key, which is `AUTH_TOKEN` in `.env`. Requests without it, or with a wrong key, are refused. Keep this key secret: anyone who has it can use the whole API.
2. Start a step:

   ```sh
   curl -X POST http://127.0.0.1:8000/api/runs/ \
     -H "Auth: YOUR_AUTH_TOKEN" -H "Content-Type: application/json" \
     -d '{"command": "verify_emails", "options": {"limit": 50}}'
   ```

3. Follow it at `/api/runs/<id>/` (with the same header).

The full list of endpoints is at **API** (top right, or http://127.0.0.1:8000/api/docs/). To try requests there, click **Authorize**, paste the key into **AuthHeader**, and click **Authorize** again.

To change the key, edit `AUTH_TOKEN` in `.env` and restart the app; the old key stops working at once.

## 9. Settings

Settings live in the `.env` file in the project folder. After changing it, restart the app (`Ctrl+C`, then `python manage.py runserver`).

| Setting | Needed for |
|---|---|
| `DATABASE_URL` | Everything (the Supabase database) |
| `DJANGO_SECRET_KEY` | Logging in (any long random text) |
| `AUTH_TOKEN` | The API: every request must send it in the `Auth` header |
| `GROQ_API_KEY` or `OPENAI_API_KEY` | Research (step 3) and writing emails (step 6) |
| `VERIFY_MAIL_FROM`, `VERIFY_HELO` | Verifying emails (step 5) |
| `SENDER_NAME` | Optional: the name in each email's sign-off (empty: "The Axioware team") |
| `GHL_API_KEY`, `GHL_LOCATION_ID` | Sending: the GoHighLevel sub-account's Private Integration token and location id |
| `GHL_WEBHOOK_SECRET` | Sending: GoHighLevel sends it with its webhooks (header `X-Webhook-Secret`) |
| `CRON_SECRET` | The cron endpoints (Vercel Cron sends it) |
| `MAPS_SCRAPER_URL` | Optional: where the scraper container answers (default `http://127.0.0.1:8001`) |

## 10. Troubleshooting

| Problem | What to do |
|---|---|
| A yellow warning on the dashboard | Add the missing setting to `.env` and restart the app |
| A run failed | Open it under **Pipeline runs**; the last lines of the output say why |
| Verify: every result is "unknown", or "port 25 is probably blocked" | Your network blocks the port used to check mailboxes; try another network |
| Verify: "Cannot reach Reacher" | Docker is not running; start Docker and run again |
| "No worker is running" | Start `python manage.py run_worker` on the machine with the browsers and Docker (only when the app runs on Vercel) |
| "handed to GoHighLevel ... never reported sent" | The GHL sending workflow is not published, or its webhook to `/api/ghl/email-sent/` fails (check the URL and the `X-Webhook-Secret` header) |
| An email shows a GoHighLevel error | Check `GHL_API_KEY`/`GHL_LOCATION_ID` and the custom fields; it is retried every minute |
| Fetch: "The scraper container ... is not reachable" | Start it (`docker compose up -d` in its folder) or fix `MAPS_SCRAPER_URL`; or use the built-in browser |
| Fetch: "the container gave up after 5 minutes" | Lower `--limit`, or raise `--concurrency` a little |
| Find decision makers: "Google presented a CAPTCHA" | Run again later, or add `--headed` and solve it in the window. DuckDuckGo is used meanwhile |
| Find decision makers: "Could not start Firefox" | Install Firefox and geckodriver |
| Generate: "No prospects need an email" | Every ready prospect already has one; use `--regenerate` to rewrite those in review |
| "Only emails in review can be approved" | The email was already approved, rejected or sent, perhaps by someone else |
| A run stays "Running" after the computer restarted | Open **Pipeline runs**; it is marked Failed automatically. Run it again |
| API answers 401 "Send the API key in the 'Auth' header" | Add `-H "Auth: <AUTH_TOKEN>"` with the exact value from `.env` |
| API answers "The API is disabled" | Set `AUTH_TOKEN` in `.env` and restart the app |
| Forgot your password | In a terminal: `python manage.py changepassword admin` |
