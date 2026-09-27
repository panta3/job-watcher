# job-watcher

Push alerts to my phone within ~15 minutes of an **entry-level / new-grad tech job in Canada** being
posted, read straight from each company's own hiring system (the same place their careers page gets
data from), so alerts often arrive before the job shows up on LinkedIn/Indeed.

- **~840 employers across 13 hiring systems, plus 2 job boards.** Big names (RBC, TD, CIBC, BMO,
  Scotiabank, Rogers, TELUS, Manulife, Sun Life, Desjardins, CAE, Amazon, Microsoft, AMD, NVIDIA,
  ...) and hundreds of smaller ones (Genetec, Clio, Jonas, Intelerad, Telesat, Mirego, VersaFile, ...).
  Hiring systems: Workday, Greenhouse, Lever, Ashby, SmartRecruiters, Workable, Rippling, BambooHR,
  Recruitee, Amazon, Jibe/iCIMS, Eightfold, SAP SuccessFactors RSS.
- **Two job boards:** Canada's **Job Bank** (small employers) and the crowd-maintained
  **SimplifyJobs new-grad list** on GitHub (hourly). If a job shows up both there and on the
  company's own board, you get one alert, using the company's own link.
- **How the list was built:** ~4,200 company feeds were pulled from every link in the SimplifyJobs
  new-grad + internship lists, each one was probed live, and the 741 with Canadian postings (now or
  historically) were added automatically. 4 staffing agencies are included but disabled.
- **Filters** ([filters.py](filters.py)): tech title (English + French), not senior/lead/manager/III+,
  no intern/co-op/student, in Canada, and the description asks for at most `MAX_YEARS` (2) years.
  ⭐ + max priority when the title/description says new grad / entry level / junior / "I" / 2027.
- **No alert spam:** a company's first scan saves its open jobs silently. After that, only
  never-seen jobs posted in the last 3 days alert, capped at 15 per run.
- **Morning (8:05) and night (21:05) digest** of everything matched, plus a warning when a feed
  has been failing for 6 scans in a row (i.e. the company moved its careers site).

## Start date: May 2027 or later

I graduate in April 2027, so every match is sorted by start date (`filters.start_verdict`):
- **✓ 2027 start**: a 2027 title, a new-grad program, "class of 2027", or a start from May 2027. Ranked first (+10 fit), always alert.
- **Wants a start before May 2027**: "ASAP", "immediate start", a start in 2026 or Jan–Apr 2027,
  a fixed-term contract, or "must have completed a degree". Hidden and never alert.
- **Start date not stated**: shown on the page and in digests, but only buzzes the phone from
  `UNCLEAR_ALERTS_FROM` (Feb 1, 2027), when a normal hire could realistically start in May.

## Web UI

Every open match on one page: **Apply ↗** opens the posting (and then asks whether you applied),
**✓ Applied** / **Hide** keep the list clean, and there's search, a ⭐ entry-level filter, a
"posted within" filter and a NEW badge for anything that appeared since your last visit.
Works on a phone (bookmark it / add to home screen) and follows your light/dark setting.

- Locally: `python3 watcher.py serve`, then open http://localhost:8765
- On AWS: `deploy.sh` prints a link (and sends it to your phone). The link carries a secret key
  (`?k=...`); without it the page returns 403. Applied/Hide marks are saved in S3, so phone and
  laptop agree.

## Use

```bash
python3 watcher.py scan --dry-run   # see what it would alert, touches nothing
python3 watcher.py serve            # web UI on http://localhost:8765
python3 watcher.py recent [days]    # open matches posted in the last N days (default 30)
python3 watcher.py test-notify      # ping your phone
```

Phone: install the **ntfy** app and subscribe to the topic in `config.env`.

## Run it

- **AWS (always on):** `./deploy.sh`: Terraform in [infra/](infra/) creates a Python 3.12 Lambda,
  EventBridge Scheduler (scan every 15 min, digests in America/Toronto time), an S3 bucket holding
  `jobs.db`, and least-privilege IAM. It fits in the Lambda always-free tier; S3 costs a few cents a
  month at most. It uploads the local `jobs.db` first and removes the local cron line.
- **Local fallback:** cron `*/15` scan + `5 8,21` digest (WSL has to be running).
- A full scan is ~70k postings in ~55 s and ~600 MB of memory (the Lambda gets 1 GB).

## Cost: $0

| Piece | Monthly use | Free allowance | Cost |
|---|---|---|---|
| Lambda (1 GB, ~60 s scan every 15 min) | ~175,000 GB-s, ~3,000 runs | 400,000 GB-s + 1M requests, **always free** | $0 |
| EventBridge Scheduler | ~3,000 triggers | 14M/month, always free | $0 |
| S3 storage (jobs.db ~30 MB) | 0.03 GB | $0.023/GB | < $0.001 |
| S3 writes (hourly + on alert) | ~900 PUTs | $0.005 per 1,000 | ~$0.005 (AWS rounds to $0.00) |
| CloudWatch Logs (14-day retention) | a few MB | 5 GB always free | $0 |
| ntfy.sh, GitHub, Job Bank | | free | $0 |

The only item not in an always-free allowance is S3 write requests, which is why jobs.db is written
back hourly instead of every 15 minutes (every scan would be ~$0.03/month). Set `budget_email` in
`infra/terraform.tfvars` to get an email if the forecast bill ever passes $1 (AWS budgets are free).
Running it locally with cron costs nothing, but only works while the laptop and WSL are on.

## Add a company

Find which hiring system its careers page uses (the job links give it away: `myworkdayjobs.com`,
`greenhouse.io`, `jobs.lever.co`, `ashbyhq.com`, ...) and add a line to [companies.json](companies.json).
Workday ids are `tenant/wdN/site` from `https://tenant.wdN.myworkdayjobs.com/site`.

## Bugs found while validating against live data

1. **Workday pinned postings hid every new TD job.** TD pins ~50 old postings above its
   date-sorted list, so "stop paging when a page is all seen" stopped before ever reaching new jobs.
   Fixed by paging until the date-sorted part is reached (ages drop from 3+ days to 0–1 days).
2. **False positives on the first real scan:** bank "Networking Event" listings, "*Mobile* Financial
   Services Representative", "Financial *security* advisor", a bodyguard role titled "Security
   advisor", Amazon loss prevention, and a CAE intern role missed because its title misspells
   *stagiaire* as "Stagaire". All added to the filters.
3. Microsoft's API rate-limits bursts (HTTP 429), so requests now retry with backoff.
4. **The first scan hid still-open jobs**, e.g. Stripe's Toronto new-grad role posted 26 days
   earlier. The window is now 45 days, and `recent` shows each job's posted date.
5. **Loose words after adding 740 companies:** "Flight *Information* Region" (air traffic control)
   hit the pattern meant for *informatique*, French *sécurité* also means workplace safety, and
   "AVP" / "Premier conseiller" are senior titles. Tightening those wrongly dropped 9 real roles
   (Forward Deployed Engineer, GIS, Guidewire, bioinformatics), which were added back after
   checking every reclassified job by hand.
6. Some Walmart Workday postings have no link path, which crashed that feed; those are skipped now.

## Known gaps

Shopify, Google, Apple, Meta, Qualcomm and Bell use custom or bot-protected sites and aren't
covered. SuccessFactors RSS only returns the 20 newest jobs, fine at a 15-minute cadence.
The years filter reads the *smallest* "N years of experience" in a description, so a job saying
"5 years, or 2 with a Master's" might be kept or dropped depending on wording.
