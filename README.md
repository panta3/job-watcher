# 💼 Job Posting Watcher

Pushes a phone alert within ~15 minutes of an **entry-level job in Canada** being posted,
by reading **~940 employers' hiring systems directly** (the same data their careers pages show),
so alerts often land before the job reaches LinkedIn or Indeed. It hunts for two people from one
scan: **tech roles** for me and **business roles** for a friend, each with their own phone alerts,
password-protected web page and application tracker.

**Status:** ✅ live on AWS Lambda, scanning every 15 minutes at **~$0/month**. All numbers below
were measured on the deployed system.

<p>
  <img src="docs/web-desktop.png" alt="Web app: open jobs ranked by resume fit, filtered to confirmed 2027 starts" width="68%">
  <img src="docs/web-phone.png" alt="Web app on a phone in dark mode" width="28%">
</p>

---

## 🎯 Why
New-grad roles are often reviewed as applications arrive, so the first days after a posting
matter. Checking dozens of careers pages by hand every day doesn't scale, and job boards
lag behind the employers' own systems. This watches the source instead.

## ⚙️ How it works
```
EventBridge Scheduler (every 15 min) -> Lambda: scan
    ├─ 21 source adapters, 16 threads  -> ~63,000 postings from ~940 employers in ~70 s
    ├─ new job IDs only (SQLite remembers every ID ever seen)
    ├─ each profile filters the same postings (profiles.py):
    │     tech:     tech title · not senior · not intern/co-op · in Canada · ≤ 2 yrs · start date
    │     business: business title · not senior · not intern · in Canada · ≤ 2 yrs
    ├─ fit score vs. resume (tech), cross-feed de-duplication, closed-posting detection
    └─ ntfy push alert to that person's phone  (⭐ = explicit new grad / entry level)
EventBridge Scheduler (8:05 / 21:05 Toronto) -> Lambda: one digest per person + follow-up reminders
aarav-jobs.vercel.app -> Lambda function URL  -> web app; the password decides whose page opens
S3                                            -> jobs.db (SQLite) + one status file per person
```

## 🔌 Sources: 21 adapters, ~940 employers

| Hiring system | Employers | Examples |
|---|---:|---|
| Workday | 275 | RBC, TD, CIBC, BMO, Manulife, Sun Life, PwC, P&G, Kraft Heinz, CPP Investments, NVIDIA |
| Greenhouse | 185 | Stripe, Databricks, Cloudflare, Geotab, Faire, Instacart |
| Ashby | 144 | Wealthsimple, Cohere, 1Password, Neo Financial, Jobber, OpenAI |
| SmartRecruiters | 92 | Ubisoft, ServiceNow, Intelerad |
| Oracle Recruiting Cloud | 78 | JPMorgan, Nokia, Fortinet, Honeywell, Ford, Texas Instruments |
| Lever | 73 | PointClickCare, Waabi, Wattpad, Palantir |
| Rippling · Workable · BambooHR · Recruitee | 78 | Genetec, Valsoft, VersaFile, D-Wave |
| SAP SuccessFactors (RSS + search) | 6 | Scotiabank, Rogers, TELUS, Bell, Deloitte Canada, EY Canada |
| Custom sites | 8 | Google, Microsoft & Qualcomm (Eightfold), IBM, Amazon, AMD (Jibe), Atlassian, Shopify |
| Job boards | 2 | Canada's Job Bank (tech and business searches), the SimplifyJobs new-grad list |

**How the list was built:** ~4,200 hiring-system links were mined from public new-grad lists,
each was probed live, and the **741 employers with Canadian postings** were added automatically.
Another 78 came from probing 184 Oracle Recruiting Cloud sites, and 11 large business employers
(Kraft Heinz, Mondelēz, General Mills, Shell, Pfizer, CPP Investments…) were added for the business
profile after checking each one's Canadian postings live. Staffing agencies are included but
disabled because they flood alerts with reposts.

## 🧹 Filtering (tech profile)
1. **Tech title** (English and French): software, data, ML/AI, cloud, DevOps, security, QA, IT…
2. **Not senior** (senior, staff, lead, manager, III+, "Premier conseiller"…) and **not student**
   (intern, co-op, stagiaire, "Summer Analyst", 8-month terms…).
3. **In Canada:** structured country fields when a source has them, otherwise location parsing
   that won't confuse Burlington, ON with Burlington, MA.
4. **Experience:** the smallest "N years of experience" in the description must be ≤ 2.
5. **Start date** (I graduate April 2027):
   - ✓ **2027 start:** a 2027 title, a new-grad program, "class of 2027", a start from May 2027. Ranked first, always alerts.
   - ✗ **Wants a start before May 2027:** ASAP, immediate start, fixed-term contracts, January starts. Hidden, never alerts.
   - **Not stated:** shown on the page and in digests; phone alerts begin February 2027.

## 👥 Profiles
One scan, several people. `profiles.py` lists who the watcher hunts for:

| | Tech (me) | Business (a friend) |
|---|---|---|
| Roles | software, data, ML/AI, cloud, security, QA, IT | finance, accounting, marketing, sales, consulting, HR, supply chain, analyst |
| Level | entry level / new grad, ≤ 2 yrs asked | entry level / new grad, ≤ 2 yrs asked |
| Start date | not before May 2027 (I graduate in April) | can start now |
| Phone buzz | every match (start-date rules apply) | only explicit entry-level jobs; the rest go to the page and digest |
| Fit score | yes, against my resume | no |

Each company is listed once per scan and every profile filters the same postings its own way, so
the second person added no measurable scan time. Matches live in their own table (one row per
profile per job). Each person has their own password, ntfy topic, digests and Applied/Hide file.
On the shared address the password you type decides whose page opens, and **Sign out / switch**
returns to the password box.

The business filter was tuned on two weeks of real postings: a first draft matched ~300 jobs/day
(store associates, brand ambassadors, part-time shifts); excluding store, warehouse, part-time,
seasonal, agent and tech titles brought it to ~130/day of real office roles. That's still too many to
buzz a phone for, so only the ~20/day that say entry level, new grad or junior do. A new profile's
page is filled once from the last 45 days of postings (titles and locations only, since older
postings' descriptions weren't stored).

## ⭐ Fit score (tech profile)
`fit.py` scores each job 0–100 against the skills on my resume (Python, AWS, Terraform, React,
security, PyTorch/RAG, testing…) plus title family, experience asked, and commute distance from
Hamilton. It's plain keyword scoring, so it's free and explainable: every card shows which skills
matched, and flags like *French required*, *Clearance* or *PhD*.

## 🖥️ Web app
- **Apply ↗** opens the posting, then asks whether you applied.
- **My applications:** stages (applied → online assessment → interview → offer / rejected / no response),
  notes, and a follow-up nudge in the morning digest after 14 days without an update.
- **Referral shortcuts:** alumni (McMaster for me) or people and recruiters at the company on LinkedIn, salary and reviews.
- Search, "Best fit" / "Newest" sort, start-date (tech) and posted-within filters, NEW badges since your last visit.
- Works on a phone, follows light/dark mode.
- **Sign-in:** a short Vercel address passes every request through to the Lambda. Each person types
  their password once per device and gets a year-long cookie that holds a hash, not the password.
  Changing a password signs that person's devices out.

## ☁️ AWS and cost
Provisioned with Terraform (`infra/main.tf`): one Python 3.12 Lambda (1 GB), EventBridge Scheduler
(15-minute scans and digests in Toronto time), an S3 bucket, a function URL, and least-privilege
IAM scoped to three S3 objects and one log group. The short address is a free Vercel project with
a single rewrite rule. Measured: a scan takes ~72 s and peaks at ~700 MB.

| Piece | Monthly use | Free allowance | Cost |
|---|---|---|---|
| Lambda | ~210,000 GB-s | 400,000 GB-s, always free | $0 |
| EventBridge Scheduler | ~3,000 runs | 14 million, always free | $0 |
| S3 writes | ~1,500 PUTs | not free | ~$0.008 (rounds to $0) |
| CloudWatch Logs (14-day retention) | a few MB | 5 GB, always free | $0 |
| Vercel (rewrite to the Lambda) | a few hundred page loads | Hobby plan, free | $0 |

S3 writes are the only paid item, so the database is saved hourly or right after an alert instead
of every scan (every scan would be ~$0.03/month).

## 🚀 Setup
Python 3.10+ standard library only; no `pip install` needed.
```bash
cp config.env.example config.env        # set JOBS_NTFY_TOPIC, then subscribe to it in the ntfy app
python3 watcher.py scan --dry-run      # what it would alert on, touches nothing
python3 watcher.py scan                # first run per company saves silently; later runs alert
python3 watcher.py serve               # web app on http://localhost:8765
python3 watcher.py recent 7            # open matches from the last 7 days, in the terminal
python3 watcher.py test-notify         # check your phone gets alerts
```
**Deploy to AWS** (Terraform + AWS CLI configured):
```bash
printf 'ntfy_topic = "your-topic"\n' > infra/terraform.tfvars
./deploy.sh      # first time: creates everything, uploads state, sends the web app link to your phone
./update.sh      # afterwards: ships code changes (never touches the database in S3)
```
`update.sh` asks for your page password once, generates the second profile's password and ntfy
topic (kept in `infra/terraform.tfvars`, never committed), applies Terraform, redeploys Vercel,
checks that the page answers HTTP 200 (retrying if not), and prints the second profile's login
and topic. `./update.sh --new-address` also replaces the AWS address behind the short link.

**Add a person:** add a `Profile` to `profiles.py` with a title filter, then a password and topic
variable in `infra/main.tf`/`update.sh` and the status file in the IAM policy.

**Add a company:** find which hiring system its careers page uses (job links give it away:
`myworkdayjobs.com`, `greenhouse.io`, `jobs.lever.co`, `ashbyhq.com`…) and add a line to
`companies.json`. Workday ids are `tenant/wdN/site` from `https://tenant.wdN.myworkdayjobs.com/site`.

## 🐛 Real bugs found by validating against live data
- **Pinned postings hid every new TD job.** TD pins ~50 old postings above its date-sorted
  Workday list, so "stop paging at an all-seen page" never reached new jobs. Fixed by paging
  until the date-sorted section starts.
- **A SQLite lock leak froze scans for an hour.** A thread that failed mid-transaction kept the
  write lock. Fixed with one short, always-closed transaction per company, serialized writes,
  WAL mode, and a lock so only one scan runs at a time.
- **The first scan hid still-open jobs** (e.g. a Stripe new-grad role posted 26 days earlier);
  the first-scan window is now 45 days.
- **Loose words matched the wrong jobs:** "Flight *Information* Region" (air traffic control)
  matched a pattern meant for *informatique*; French *sécurité* also means workplace safety;
  "Assurance auto*mobile*" matched *mobile*; Burlington, MA and Hamilton, NJ counted as Canada.
  Every fix was checked against each job it reclassified. One pass dropped 101 jobs and 3 real
  roles were restored.
- **A false "Microsoft feed broken" alert.** Microsoft's API answered 429 Too Many Requests on
  many of its polls (64 times in five days), and that adapter had no retry. Worse, the stored fail count could
  only grow: a failure at the hourly save was uploaded, but the recovery 15 minutes later never was.
  Now every request goes through one retry-with-backoff helper, a recovered feed forces a save, and
  rate limits and timeouts only alert after 24 straight failures (a 404 still alerts after 6).
- **A deleted job board failed 257 scans in a row** (404 since it was removed); it's disabled now.
- **The short address served Vercel's 404 twice.** A deploy that hung finished late and took over
  production. `update.sh` now verifies the live page after deploying and redeploys or fails loudly.

## ⚠️ Known limits
- Not covered: Apple (private API), Meta and Tesla (bot protection), Uber, SAP, Kinaxis, OpenText, CGI.
- SuccessFactors RSS returns only the 20 newest postings, fine at a 15-minute cadence.
- Fit scoring, start-date detection and the business-title filter are keyword/regex-based: they
  can't tell a required skill from a nice-to-have, and a few odd titles slip through either way.
- Business jobs at the big banks repeat per branch ("Personal Banking Associate - <city>"); identical
  titles are merged, but branch-specific titles each show up.
- Job Bank brings many small employers, and some postings come from immigration-consulting firms.
- Closed postings are detected on feeds that return a whole board, plus a daily check for Workday;
  elsewhere a job drops off after 45 days.

## 🗂️ Layout
| File | What's in it |
|---|---|
| `sources.py` | the 21 adapters, one per hiring system or site |
| `filters.py` | tech / business / seniority / student / Canada / experience / start-date rules |
| `profiles.py` | who the watcher hunts for, and each person's settings |
| `fit.py` | resume-fit scoring |
| `watcher.py` | scan, alerts, digests, follow-ups, CLI |
| `webui.py` | the web app (single page, no build step) |
| `lambda_function.py` | Lambda entry point: S3 sync, schedules, password sign-in, function URL |
| `infra/main.tf`, `deploy.sh`, `update.sh` | AWS infrastructure, one-command deploy, and updates |
| `companies.json` | the employer list |
