# User guide

This tool finds local businesses, works out who runs each one and their email address, writes a personal cold email (and follow-ups) for each person, and lets you review and approve every email. You queue approved emails in the **Outbox** and the app sends them from **your own mailbox**, each at its time. Leads move through a **Pipeline** you update when people reply. Everything runs on your computer.

You do everything from the **admin** in your browser. Nothing is ever sent without your approval.

## 1. Start the app and log in

Open a terminal in the project folder and run:

```sh
docker compose up -d          # the database and web search (once after each restart of the computer)
source .venv/bin/activate
python manage.py runserver
```

Then open **http://127.0.0.1:8000/admin/** and log in. Keep the terminal open while you work; press `Ctrl+C` in it to stop the app. Queued emails are only sent while the app (and the computer) is on.

> **New team member?** An admin creates your login under **Users > Add user**. Tick **Staff status** and **Superuser status** so you can use every page.

## 2. The dashboard

The home page shows the whole pipeline at a glance:

- **The counters** follow a business from left to right: found on Google Maps, website researched, qualified, contact found, ready to email, email in review, approved, queued, sent. Click any counter to see those records.
- **Get leads** lists your campaigns with two buttons each, one per way of finding businesses on Google Maps: **Built-in browser** and **Scraper container**. It also shows whether the scraper container is running. See [Step 2](#step-2-fetch-businesses).
- **Pipeline** shows how many leads are in each stage; click one to open the [Pipeline](#step-9-the-pipeline-leads) page.
- **What to do next** lists the jobs that are waiting, with a button for each, e.g. "3 emails are waiting for your review: Start reviewing".
- **Recent pipeline runs** shows the jobs you started and whether they finished.
- **Yellow warnings** at the top mean something in the settings is missing (for example the AI key). See [Settings](#9-settings).

The left sidebar lists the pages in pipeline order (its **Jump to a page** box only narrows this list of pages, it does not filter records): **Campaigns > Businesses > Website research > Contacts > Prospects > Emails > Outbox > Pipeline**, then **Pipeline runs**, **Settings** and **Google Maps listings**.

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
| 6. Write emails | Your email template is filled in for each prospect | Prospects > **Write emails from templates** |
| 7. Review | You read, edit, approve or reject each email; approving gives it a send time | Emails |
| 8. Send | You queue approved emails; each goes from your mailbox at its time | **Outbox** |
| 9. Leads | You move leads when they reply, book a demo, win or lose | **Pipeline** |
| 10. Follow-ups | Written for review after each email is sent, queued and sent the same way | Emails, Outbox |

Each step only processes what still needs it, so it is always safe to run a step again.

### Run the whole pipeline at once

Open a campaign and click **Run full pipeline** (top right), or tick it on the Campaigns list and use the action **Run the full pipeline**. The run form opens with `--campaign-id N --fetch-limit 20`; change the number of businesses to fetch and click **Start run**.

It runs steps 2 to 6 one after another for that campaign's businesses: find businesses, research websites, find decision makers, verify emails, write emails. You can follow every step on the run page. It **never sends anything**: the new emails wait in **Emails > In review** for you to approve (step 7).

- Each step only does what is still needed, so running the pipeline again continues where it left off.
- If a few items fail (one website is down, for example), the pipeline carries on with the next step.
- If a step cannot run at all (no AI key for research, Docker/Reacher not running, Google CAPTCHA), the pipeline stops and tells you why. Everything done so far is kept; fix the problem and run it again.
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
   - **Email sequences**: tick one or more. A sequence is a whole email flow (first email, then its follow-ups). With several, prospects are split equally between them (an A/B test) and each person stays in their sequence to the end. See [Email sequences](#email-sequences-and-follow-ups).
   - **Sending** (optional): a **send window** (e.g. 09:00 to 18:00) with its time zone. The gap between two sends and the **daily cap** are the same for every campaign: sidebar > **Settings** (defaults: 2 minutes fixed plus a random 3-5 minutes, 40 emails a day). See [Sending](#step-8-sending-through-gohighlevel).
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

It reads the business's own team and about pages first, then searches the web through your SearXNG. No browser opens and there are no CAPTCHAs to solve. The people it finds are listed under **Contacts**, with their job title and every email address that might be theirs (for example `ali@clinic.pk`, `ali.khan@clinic.pk`).

> **Tip:** each run does as many businesses as fit in about 4 minutes, then says how many are left. Click **Run again** on the run's page to continue.

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

> **Sure a risky or unknown address is real?** On **Prospects**, filter **Needs review**, tick it and choose **Approve**: it gets emails like a verified one. **Reject** means it is never emailed. Undeliverable addresses cannot be approved.

### Step 6: Write emails

1. Sidebar > **Prospects** (or **Businesses**), tick the rows, choose **Write emails from templates (Module 5)** and click **Go**. To write emails for every ready prospect, click **Write emails** on the dashboard instead.
2. The **Write emails** page opens:
   - **Template**: leave it on "The initial-email template of each prospect's sequence (A/B split)", or pick one template for all of them. Click **+** to write a new template or the pencil to edit the selected one, without leaving the page.
   - **Fill in again emails still in review or rejected**: tick to replace those drafts. Approved and sent emails are never changed.
   - **Limit** and **Dry run** work like everywhere else.
3. Click **Write emails**.

Each prospect gets its campaign's template with the variables filled in (no AI is used). Every new email starts as **In review** and records which template it came from. Nothing is sent until you approve it. A prospect whose template needs a value it does not have (see fallbacks below) is **held back**: the run says which variable, and no email is saved for it.

### Prompts and templates

Both are edited in the admin; nothing is built into the code. Campaigns choose them, and several campaigns can share one.

- **Campaign prompts** (sidebar) **qualify businesses**. They describe what you sell, what a good and a poor prospect look like, and what each score range means. In step 3 the AI reads each business's website and scores it 0-100 against its campaign's campaign prompt. The AI receives **exactly what you write** there. The shape of its answer is the **Answer format (JSON schema)** on the prompt's page; extra fields you add there (e.g. `team_size`) are stored per business and become email variables.
- **Email templates** (sidebar) **are the emails**. Each has a **Kind** (**Initial email** or **Follow-up**), a **Subject** and a **Body**. Write the email as you want it sent, with variables where the prospect's details go. **Email sequences** put them in order (see [Email sequences](#email-sequences-and-follow-ups)). A template's page shows a preview for a real prospect and which sequences use it.

**Variables.** Open **Email templates** > **Email variables** for the full list with example values. The common ones:

| Write | You get |
|---|---|
| `{{first_name}}` | the contact's first name, e.g. Ayesha |
| `{{first_name|there}}` | the first name, or "there" when we have none |
| `{{business_short_name}}` | the business without its branch, e.g. Alvi Dental Hospital |
| `{{city}}`, `{{google_rating}}`, `{{job_title}}` | the business's city, its Google rating, the contact's title |
| `{{team_size}}`, `{{languages}}` | what research found (any field in a campaign prompt's answer format) |
| `{{sender_name}}` | your name for the sign-off |
| `{{previous_subject}}` | follow-ups only: the subject of the email before it (e.g. `Re: {{previous_subject}}`) |

- After `|` comes the **fallback**, used when the prospect has no value. A variable **without** a fallback and without a value holds the email back, so nobody gets "Hi ,". Use a fallback on anything that may be missing (names, research fields).
- A template with a misspelled variable, or `{{previous_subject}}` in an initial email, cannot be saved: the page says what is wrong.
- The footer (company details and unsubscribe line) is added automatically; do not write it. Change it in sidebar > **Settings** > **Email footer**: emails written or edited after that get the new footer, emails already written keep theirs (use **Fill in again from template** to update one).
- The campaign page warns when it has no campaign prompt, no email sequence, a sequence without an initial-email template, or a follow-up step without a template.

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
| **Fill in again from template** | Fills the template in again, e.g. after you edited the template or the prospect's data (it stays in review). The menu next to it lets you pick a different template; by default it uses the same one |
| **Back to review** | Undoes Approve or Reject; a scheduled email loses its send time |
| **Save** | Saves your edits to the subject or body |

Editing:

- Change the **Subject** or **Body**, then click **Save** at the bottom of the page.
- Write only the message and your sign-off. The footer (company details and unsubscribe line, set on the **Settings** page) is added automatically, so do not add it yourself.
- Saving an approved email sends it back to **In review**, so every email is approved in its final form.
- **Next in review** (top of the page) moves through the queue without approving.

To approve or reject many emails at once: on the Emails list tick them, choose **Approve selected emails** or **Reject selected emails** in the Action menu, and click **Go**. Emails that still contain the word `PLACEHOLDER` cannot be approved.

### Step 8: Sending (the Outbox)

Emails go from **your own mailbox**, at the time the app gives each one. Nothing is sent until you queue it.

1. **Approve** an email (Step 7): it gets a send time. That time comes after the latest planned email of any campaign, plus the gap (Settings; default 2 minutes plus a random 3-5 minutes). It is only inside its campaign's send window, and the daily cap (default 40) applies to all campaigns together, since one mailbox sends them all. A follow-up never goes before its days after the previous email.
2. Sidebar > **Outbox**. It lists approved, queued and failed emails with their send times (in each campaign's time zone).
3. **Tick the ones to send**, choose **Queue for sending** in the Action menu and click **Go**. (One email: **Queue for sending** on its page.)
4. The **sender** starts in the background (it shows under Pipeline runs). It waits until each queued email's time, sends it, and stops when nothing is queued any more.

What else to know:

- **Queued late?** An email whose time has already passed gets a new time when you queue it, so a backlog never goes out all at once.
- **Changed your mind?** **Take out of the queue** keeps it approved with the same time. Editing it, rejecting it or sending it back to review also takes it out of the queue.
- **After it is sent**, it becomes **Sent**, the lead moves to Initial Email or Follow-up on the Pipeline, and the next follow-up is written for your review.
- **A send failed?** The Outbox shows it as **Failed** with the reason. Fix the problem and queue it again.
- **The computer was off or restarted?** The Outbox says "queued but the sender is not running": click **Start the sender**. Emails whose time passed go out one after another.
- **Set up once:** your mailbox's SMTP details in `.env` (see [Settings](#9-settings)). Until then queued emails just wait, and the dashboard says so.

Statuses: In review → Approved → **Queued** → **Sent** (→ Replied), or **Failed**.

### Step 9: The Pipeline (leads)

Sidebar > **Pipeline** shows every lead in its stage: **New Lead, Ready to Send, Initial Email, Follow-up, Replied, Demo Booked, Demo Done, Won, Lost, Quota Exhausted**. Search or pick a campaign at the top. On each card, choose a stage and click **Move**.

- **The app moves leads while sending:**
  - first email approved → Ready to Send;
  - sent → Initial Email;
  - a follow-up sent → Follow-up;
  - no reply within the reply window (Settings, default 3 days) after the last email of their sequence → Quota Exhausted.
- **You move leads when something happens.** Read your inbox and move the lead:
  - **Replied, Demo Booked, Demo Done, Won, Lost** or **Quota Exhausted** stops their emails: anything not sent yet is cancelled. **Replied** also marks their last email as replied.
  - **Back to Initial Email or Follow-up** resumes a stopped lead (e.g. the "reply" was an out-of-office). The cancelled emails come back to review, and the next follow-up is written if needed.
- Other people at the same business are not affected; each person has their own sequence.
- Every move is recorded: the prospect's page shows the **Stage history** (who moved it and when).

### Email sequences and follow-ups

An **email sequence** is one whole flow: the first email, then follow-ups for people who did not reply. For example:

- **Sequence A:** Initial email > Follow-up 1 (3 days later)
- **Sequence B:** Initial email > Follow-up 1 (2 days later) > Follow-up 2 (5 days later) > Follow-up 3 (9 days later)

Each step uses its own template, so B can open with another first email, follow up sooner, and send more follow-ups than A.

**1. Build a sequence.** Sidebar > **Email sequences** > **Add**. Give it a **Name** (it is what the Pipeline and the results table show), pick its **Initial-email template**, and add one row per follow-up in the **Follow-ups** table: **Sequence** (1, 2, ...), **Days after previous** and **Template** (a follow-up template). Save. The page shows the flow, e.g. *Initial email: First touch → Follow-up 1 (+3 days): Gentle nudge*, and which campaigns use it.

**2. Choose sequences on the campaign.** Open the campaign and tick one or more under **Email sequences**. With one, everybody gets it. With several, it is an A/B test: new prospects are split equally between them, and each person keeps their sequence to the end (first email and every follow-up). A sequence can be used by several campaigns. Changing a campaign's sequences later does not move people who already started one.

**3. Follow-ups are written right after the previous email is sent.** As soon as an email is sent, the next step of *that person's* sequence is filled in for review (if it was held back for a missing value, fix it and use **Write follow-ups** on the dashboard). You have the days in between to review it.

**4. Review and approve** like first emails. Approving schedules it for no earlier than the previous email's send time plus that step's days.

**5. The end.** After a sequence's last follow-up is sent and the reply window passes with no reply, the lead moves to **Quota Exhausted**. A follow-up step without a template also ends the sequence there. Sequences can have different lengths: in the example, A ends after one follow-up and B after three.

No follow-up is written once the person replied, their sequence was stopped, or they are do-not-contact.

**Results:** the dashboard's **Results by email sequence (A/B) and step** table shows, per sequence and step: emails sent, replies and reply rate. Each sequence's page shows its own numbers.

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
| `--headed` | Fetch with the built-in browser (your own machine only) | Show the browser window |
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
| `--regenerate` | Write emails | Fill in again emails that are in review or rejected |
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
| `DATABASE_URL`, `LOCAL_DB_PASSWORD` | Everything (the database on this computer, from `docker compose up -d`) |
| `DJANGO_SECRET_KEY` | Logging in (any long random text) |
| `AUTH_TOKEN` | The API: every request must send it in the `Auth` header |
| `GROQ_API_KEY` or `OPENAI_API_KEY` | Research (step 3) |
| `SEARXNG_URL`, `SEARXNG_SECRET` | Web search for decision makers (step 4); the local SearXNG |
| `VERIFY_MAIL_FROM`, `VERIFY_HELO` | Verifying emails (step 5) |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_SECURITY`, `SMTP_USERNAME`, `SMTP_PASSWORD` | Sending (step 8): your mailbox's SMTP server and login (Gmail / Google Workspace: an app password) |
| `SENDER_EMAIL`, `SENDER_NAME` | Sending: the From address and name; the name is also the sign-off (empty: "The Axioware team") |
| `MAPS_SCRAPER_URL` | Optional: where the scraper container answers (default `http://127.0.0.1:8001`) |

## 10. Troubleshooting

| Problem | What to do |
|---|---|
| A yellow warning on the dashboard | Add the missing setting to `.env` and restart the app |
| A run failed | Open it under **Pipeline runs**; the last lines of the output say why |
| Verify: every result is "unknown", or "port 25 is probably blocked" | Your network blocks the port used to check mailboxes; try another network |
| Verify: "Cannot reach Reacher" | Is Docker running? The app starts Reacher itself; check `REACHER_URL` |
| Sending: "The mail server refused the login" | Check `SMTP_USERNAME` / `SMTP_PASSWORD` (Gmail needs an app password); the email stays queued, then click **Start the sender** in the Outbox |
| An email is **Failed** in the Outbox | Read the reason next to it, fix it, then queue it again |
| "queued but the sender is not running" | The app was restarted: click **Start the sender** in the Outbox |
| Fetch: "The scraper container ... is not reachable" | Start it (`docker compose up -d` in its folder) or fix `MAPS_SCRAPER_URL`; or use the built-in browser |
| Fetch: "the container gave up after 5 minutes" | Lower `--limit`, or raise `--concurrency` a little |
| Find decision makers: "Search failed: SearXNG ... is not reachable" | Run `docker compose up -d` and check `SEARXNG_URL`; the business is retried next run |
| Write emails: "No prospects need an email" | Every ready prospect already has one; use `--regenerate` to fill those in review again |
| Write emails: "held back ... no value for {{x}}" | The template needs a value this prospect does not have: give the variable a fallback (`{{x|...}}`) or fill in the data, then run again |
| "Only emails in review can be approved" | The email was already approved, rejected or sent, perhaps by someone else |
| A run stays "Running" after the computer restarted | Open **Pipeline runs**; it is marked Failed automatically. Run it again |
| API answers 401 "Send the API key in the 'Auth' header" | Add `-H "Auth: <AUTH_TOKEN>"` with the exact value from `.env` |
| API answers "The API is disabled" | Set `AUTH_TOKEN` in `.env` and restart the app |
| Forgot your password | In a terminal: `python manage.py changepassword admin` |
