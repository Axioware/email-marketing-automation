# User guide

This tool finds local businesses, works out who runs each one and their email address, writes a personal cold email for each person, and lets you review every email before anything is sent. Opens are tracked after sending.

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

- **The counters** follow a business from left to right: found on Google Maps, website researched, qualified, contact found, verified prospect, email in review, sent, opened. Click any counter to see those records.
- **What to do next** lists the jobs that are waiting, with a button for each, e.g. "3 emails are waiting for your review: Start reviewing".
- **Recent pipeline runs** shows the jobs you started and whether they finished.
- **Yellow warnings** at the top mean something in the settings is missing (for example the email account for sending). See [Settings](#9-settings).

The left sidebar lists the pages in pipeline order (its **Jump to a page** box only narrows this list of pages, it does not filter records): **Campaigns > Businesses > Website research > Contacts > Prospects > Emails**, then **Pipeline runs**, **Email opens** and **Google Maps listings**.

## 3. How the pipeline works

| Step | What happens | Where |
|---|---|---|
| 1. Campaign | You say what to search for and where | Campaigns |
| 2. Find businesses | Google Maps is searched and each business is saved | Campaign > **Fetch businesses** |
| 3. Research | An AI reads each business's website and scores it 0-100 | Businesses > **Research websites** |
| 4. Find the decision maker | The owner or manager is found, and likely email addresses are guessed | Businesses > **Find decision makers** |
| 5. Verify emails | Each guessed address is checked and saved with its result; only real ones are ready to email | Businesses > **Verify contact emails** |
| 6. Write emails | The AI writes one email per prospect | Prospects > **Generate outreach emails** |
| 7. Review | You read, edit, approve or reject each email | Emails |
| 8. Send | Approved emails are sent from your email account | Email > **Send now** |

Each step only processes what still needs it, so it is always safe to run a step again.

### Run the whole pipeline at once

Open a campaign and click **Run full pipeline** (top right), or tick it on the Campaigns list and use the action **Run the full pipeline**. The run form opens with `--campaign-id N --fetch-limit 20`; change the number of businesses to fetch and click **Start run**.

It runs steps 2 to 6 one after another for that campaign's businesses: find businesses, research websites, find decision makers, verify emails, write emails. You can follow every step on the run page. It **never sends anything**: the new emails wait in **Emails > In review** for you to approve (steps 7 and 8).

- Each step only does what is still needed, so running the pipeline again continues where it left off.
- If a few items fail (one website is down, for example), the pipeline carries on with the next step.
- If a step cannot run at all (no LLM key, Docker/Reacher not running, Google CAPTCHA), the pipeline stops and tells you why. Everything done so far is kept; fix the problem and run it again.
- Options: `--fetch-limit 0` skips fetching new businesses, `--skip verify` skips a step (fetch, research, stakeholders, verify, generate), `--min-score 60`, `--email-prompt-id N`, `--headed`.

## 4. Step by step

### Step 1: Create a campaign

1. Sidebar > **Campaigns** > **Add campaign**.
2. Fill in:
   - **Name**: anything you will recognise, e.g. "Lahore dentists".
   - **Target country**: one country, e.g. Pakistan.
   - **Target locations**: cities or areas, separated by commas, e.g. `Lahore, DHA, Gulberg`.
   - **Search terms**: what you would type into Google Maps, separated by commas, e.g. `dental clinic, dentist`.
   - **Campaign prompt** (under Emails): the instructions the AI follows when writing this campaign's emails. Pick one from the list, or click **+** to write a new one. Each prompt belongs to one campaign. Leave it empty to use the built-in Axioware prompt. See [Prompts](#prompts).
3. Click **Save**.

Every search term is searched in every location, so 2 terms and 3 locations means 6 searches. The campaign's **Status** (Pending, Running, Completed, Failed) is set automatically when businesses are fetched; you cannot edit it.

### Step 2: Fetch businesses

1. Open the campaign and click **Fetch businesses** (top right).
2. The run form opens with `--limit 20`. Change the number to fetch more or fewer businesses.
3. Click **Start run** and watch the progress (see [Pipeline runs](#5-pipeline-runs)).

The campaign's status changes to **Completed** when it finishes. The businesses appear under **Businesses**.

Each business gets its category, Google rating, number of reviews, opening hours, phone, website and address, and its city, state/province and postal code are worked out from the address (no AI is used for any of this).

**Missing a category or review count?** Google sometimes shows a reduced page without them, especially after many pages in a row. On **Businesses**, tick the businesses (or none, for all that are missing something), choose **Refresh Google Maps details (category, reviews, city)** and start the run. It revisits each business's Google Maps page, retries pages Google reduced, and fills in what was missing; it never changes a business's website. The dashboard shows a **Refresh Google Maps details** button while any business has no review count. A business with no reviews on Google keeps an empty review count.

### Finding businesses: filters

On **Businesses**, click one of the **quick filter boxes** above the list (Not researched, Qualified, Score 80+, 100+ reviews, Rated 4.5+, No contacts yet, Ready to email, Email sent), or type in the **search box**: it matches name, category, city, address, phone and website. The **Filter** panel on the right narrows the list further, and filters combine: by campaign, website, website research (not researched, researched, failed), qualification score (80+, 50-79, below 50), decision-maker search, contacts, prospects (ready, needs review, contacted, none), emails (in review, approved, sent, opened, none), Google rating, number of Google reviews, category, country and city. Select the filtered rows and run any action on them, e.g. "score 80 and above" + "Not searched yet" → **Find decision makers**.

### Step 3: Research websites

1. Sidebar > **Businesses**. Tick the businesses to research. To select all of them, tick the box in the table header, then click "Select all".
2. In the **Action** menu choose **Research websites (Module 2)** and click **Go**.
3. Click **Start run**.

For each business the AI reads up to 5 pages of its website and gives a score with reasons. The score and status show in the Businesses list; click a business and then its **Website research** link to see what the AI read and why it scored it that way.

Businesses you select are researched even if they were researched before (the new score replaces the old one). The **Research websites** button on the dashboard only does businesses that have not been researched yet. Businesses without a website are skipped. If research fails for one (the site was down, for example), run it again later; **Retry failed** on the dashboard does this.

### Step 4: Find decision makers

1. Sidebar > **Businesses**, tick the businesses (only those with a score of 50 or more are processed).
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

> **Sure a risky address is real?** Open the prospect, set **Email status** to **Deliverable** and **Outreach status** to **Ready**, and save. It will then get an email like any other. (Running Verify with `--recheck` later would put it back to its checked result.)

### Step 6: Generate emails

1. Sidebar > **Prospects** (or **Businesses**), tick the rows, choose **Generate outreach emails (Module 5)** and click **Go**. To write emails for every ready prospect, click **Generate emails** on the dashboard instead.
2. The **Generate emails** page opens:
   - **Email prompt**: which instructions to use. Leave it on "Each campaign's default email prompt", or pick one (e.g. a follow-up). Click **+** to write a new email prompt or the pencil to edit the selected one, without leaving the page.
   - **Rewrite emails that are in review or rejected**: tick to replace those drafts. Approved and sent emails are never changed.
   - **Limit** and **Dry run** work like everywhere else.
3. Click **Start generating**.

The AI writes one short email per prospect, based only on what was found about the business. Every new email starts as **In review**, and records which email prompt wrote it. Nothing is sent.

### Prompts

The AI's instructions come in two parts, both edited in the admin:

- **Campaign prompts** (sidebar): the standing instructions for a campaign: who we are, what we offer, how to read the facts about each business, the rules every email must follow, and the output format. Each campaign uses one campaign prompt.
- **Email prompts** (sidebar, or inside a campaign prompt's page): how to write one kind of email, e.g. "First-touch email" or "Follow-up". A campaign prompt can have many; the one marked **Default** is used unless you choose another.

When an email is written, the AI receives the campaign prompt followed by the email prompt. Open an email prompt and expand **What the model receives** to see the combined text.

- A new campaign prompt starts with the built-in Axioware text; if you save it without an email prompt, the built-in "First-touch email" is added to it.
- Your first campaign already uses **Axioware - dental clinics** with its **First-touch email**.
- A campaign without a prompt uses the built-in Axioware prompt.
- Keep the **OUTPUT** section (subject and body as JSON) in every campaign prompt; the app needs it to read the AI's answer.

### Step 7: Review emails

Sidebar > **Emails**. The boxes at the top show how many emails are in each status; click **In review** to see the ones waiting for you. Click a subject to open it.

The email page has three parts:

1. **Review** (top left): the buttons, plus what we know about the business (city, website, rating, findings from its website, why it qualified).
2. **Preview** (top right): exactly what the recipient will see, including the Axioware footer. Viewing it never counts as an open.
3. **Content** (below): the subject and body you can edit.

What you can do:

| Button | What it does |
|---|---|
| **Approve** | Marks the email ready to send and jumps to the next email in review |
| **Reject** | Puts it aside; add a reason so you remember why |
| **Regenerate with AI** | Writes a new version (it stays in review). The menu next to it lets you pick a different email prompt; by default it uses the same one |
| **Back to review** | Undoes Approve or Reject |
| **Save** | Saves your edits to the subject or body |

Editing:

- Change the **Subject** or **Body**, then click **Save** at the bottom of the page.
- Write only the message and your sign-off. The footer (company details and unsubscribe line) and the tracking image are added automatically, so do not add them yourself.
- Saving an approved email sends it back to **In review**, so every email is approved in its final form.
- **Next in review** (top of the page) moves through the queue without approving.

To approve or reject many emails at once: on the Emails list tick them, choose **Approve selected emails** or **Reject selected emails** in the Action menu, and click **Go**. Emails that still contain the word `PLACEHOLDER` cannot be approved.

### Step 8: Send

**One email:** open an approved email. Under **Send now**, type the recipient's address to confirm, then click **Send**.

**Many emails:** on the Emails list tick the approved emails, choose **Send selected approved emails** and click **Go**. The run form opens with `--send`. Click **Start run**. Emails are sent one at a time, 20 seconds apart, to stay within your email provider's limits.

> **Want to check first?** Remove `--send` from the arguments. The run then only lists what would be sent.

After sending, the email's status is **Sent** and the prospect's is **Contacted**. Only approved emails are ever sent, and never twice.

If an email shows **Failed**, the recipient's server refused it; the reason is on the email page. If sending stops with a login or connection error, the email goes back to **Approved** so you can try again later.

## 5. Pipeline runs

Steps 2-6 and bulk sending can take minutes, so they run in the background as **pipeline runs**.

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
| `--all` | Refresh Google Maps details | Refresh every business, not only those missing details |
| `--address-only` | Refresh Google Maps details | Only fill city, state and postal code from the address (no browser) |
| `--retry-failed` | Research | Also retry businesses whose research failed |
| `--redo` | Research | Research again businesses that were already researched |
| `--redo` | Find decision makers | Search again for businesses already done |
| `--min-score 60` | Find decision makers, Verify | Only businesses scoring 60 or more |
| `--recheck` | Verify | Check addresses that were already checked |
| `--primary-only` | Verify | Only the main contact of each business |
| `--regenerate` | Generate emails | Rewrite emails that are in review or rejected |
| `--send` | Send | Actually send (without it, only a preview) |

## 6. Managing prospects

- **Do not contact:** on the Prospects list, tick them and choose **Mark do not contact**. They will never be emailed, and verification never changes them back. Use this when someone replies "unsubscribe". **Clear do not contact** undoes it.
- **Add a contact by hand:** **Contacts > Add contact**, then run **Verify** for that contact. Hand-added contacts are kept when Find decision makers runs again.
- **Status meanings:** Ready (can be emailed), Needs review (risky or unknown address), Rejected (undeliverable), Contacted (an email was sent). The **Verdict** column says exactly why an address is not ready.

## 7. Open tracking

When a recipient opens an email and their email app loads images, the open is recorded: the email's status changes from **Sent** to **Opened**, and the time and email app appear under **Email opens** on the email's page. The Emails list shows the overall **open rate**.

Keep in mind:

- An open means the image was loaded. Some apps block images (no open recorded) and some security scanners load them automatically (an open that was not a person).
- Opens are only counted after an email is sent; previews in the admin never count.
- Treat the open rate as a signal, not an exact number.

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
| `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM_EMAIL`, `SMTP_FROM_NAME` | Sending (step 8); `SMTP_FROM_NAME` is also the name in each email's sign-off |
| `SMTP_REPLY_TO` | Optional: where replies and unsubscribe requests go |

## 10. Troubleshooting

| Problem | What to do |
|---|---|
| A yellow warning on the dashboard | Add the missing setting to `.env` and restart the app |
| "Sending is not set up" / "SMTP is not configured" | Fill in the `SMTP_*` settings |
| A run failed | Open it under **Pipeline runs**; the last lines of the output say why |
| Verify: every result is "unknown", or "port 25 is probably blocked" | Your network blocks the port used to check mailboxes; try another network |
| Verify: "Cannot reach Reacher" | Docker is not running; start Docker and run again |
| Find decision makers: "Google presented a CAPTCHA" | Run again later, or add `--headed` and solve it in the window. DuckDuckGo is used meanwhile |
| Find decision makers: "Could not start Firefox" | Install Firefox and geckodriver |
| Generate: "No prospects need an email" | Every ready prospect already has one; use `--regenerate` to rewrite those in review |
| "Only emails in review can be approved" | The email was already approved, rejected or sent, perhaps by someone else |
| A run stays "Running" after the computer restarted | Open **Pipeline runs**; it is marked Failed automatically. Run it again |
| API answers 401 "Send the API key in the 'Auth' header" | Add `-H "Auth: <AUTH_TOKEN>"` with the exact value from `.env` |
| API answers "The API is disabled" | Set `AUTH_TOKEN` in `.env` and restart the app |
| Forgot your password | In a terminal: `python manage.py changepassword admin` |
