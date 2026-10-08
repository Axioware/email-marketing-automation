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

The left sidebar lists the pages in pipeline order: **Campaigns > Businesses > Website research > Contacts > Prospects > Emails**, then **Pipeline runs**, **Email opens** and **Google Maps listings**.

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

## 4. Step by step

### Step 1: Create a campaign

1. Sidebar > **Campaigns** > **Add campaign**.
2. Fill in:
   - **Name**: anything you will recognise, e.g. "Lahore dentists".
   - **Target country**: one country, e.g. Pakistan.
   - **Target locations**: cities or areas, separated by commas, e.g. `Lahore, DHA, Gulberg`.
   - **Search terms**: what you would type into Google Maps, separated by commas, e.g. `dental clinic, dentist`.
3. Click **Save**.

Every search term is searched in every location, so 2 terms and 3 locations means 6 searches.

### Step 2: Fetch businesses

1. Open the campaign and click **Fetch businesses** (top right).
2. The run form opens with `--limit 20`. Change the number to fetch more or fewer businesses.
3. Click **Start run** and watch the progress (see [Pipeline runs](#5-pipeline-runs)).

The campaign's status changes to **Completed** when it finishes. The businesses appear under **Businesses**.

### Step 3: Research websites

1. Sidebar > **Businesses**. Tick the businesses to research. To select all of them, tick the box in the table header, then click "Select all".
2. In the **Action** menu choose **Research websites (Module 2)** and click **Go**.
3. Click **Start run**.

For each business the AI reads up to 5 pages of its website and gives a score with reasons. The score and status show in the Businesses list; click a business and then its **Website research** link to see what the AI read and why it scored it that way.

Businesses without a website are skipped. If research fails for one (the site was down, for example), run it again later; **Retry failed** on the dashboard does this.

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
| Unknown | Mail server unreachable, Mail server refused the check, Temporary failure (greylisting), Check failed | **Needs review**: checked again on the next run |

On the Prospects list, use the **By email status** and **By verdict** filters on the right, e.g. to see every catch-all address. Hover over a verdict to see the reason in words; open a prospect to see the details Reacher found. Only **Ready** prospects are ever emailed.

> **Sure a risky address is real?** Open the prospect, set **Email status** to **Deliverable** and **Outreach status** to **Ready**, and save. It will then get an email like any other. (Running Verify with `--recheck` later would put it back to its checked result.)

### Step 6: Generate emails

1. Sidebar > **Prospects**, tick the prospects (or use **Generate emails** on the dashboard for all of them).
2. Action **Generate outreach emails (Module 5)** > **Go** > **Start run**.

The AI writes one short email per prospect, based only on what was found about the business. Every new email starts as **In review**. Nothing is sent.

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
| **Regenerate with AI** | Writes a new version (it stays in review) |
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
| `--retry-failed` | Research | Also retry businesses whose research failed |
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
