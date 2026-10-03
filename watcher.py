#!/usr/bin/env python3
"""job-watcher: poll Canadian tech employers' hiring systems, alert on new entry-level jobs.

  python3 watcher.py scan            # hourly (cron): alert each new match immediately
  python3 watcher.py digest          # morning/night (cron): summary of the last 12h
  python3 watcher.py recent [days]   # print current matches in the terminal
  python3 watcher.py serve           # web UI at http://localhost:8765 (Apply links + Applied/Hide)
  python3 watcher.py backfill        # fetch descriptions for older matches (fit scores)
  python3 watcher.py discover        # monthly: find new companies with Canadian postings
  python3 watcher.py test-notify     # check your phone gets alerts
  add --dry-run to scan: no phone alerts, no database writes
"""
import concurrent.futures as cf
import json
import os
import re
import sqlite3
import threading
from datetime import timedelta
import sys
import time
import traceback
import urllib.request
from datetime import datetime

import filters
import fit
import sources
import webui
from profiles import ME, PROFILES

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get("JOBS_DB") or os.path.join(HERE, "jobs.db")
OPEN_JSON = os.path.join(os.path.dirname(DB), "open_jobs.json")   # what the web UI shows
STATUS_JSON = os.path.join(os.path.dirname(DB), "status.json")    # applied / hidden marks


def load_env():
    env = {"MAX_YEARS": "2", "MAX_AGE_DAYS": "3", "MAX_ALERTS_PER_RUN": "15", "SEED_DETAIL_DAYS": "45",
           # can't start before graduating (Apr 2027). Jobs that don't state a start date only buzz your
           # phone from UNCLEAR_ALERTS_FROM on (~3 months ahead, when a normal hire could start in May).
           "AVAILABLE_FROM": "2027-05-01", "UNCLEAR_ALERTS_FROM": "2027-02-01"}
    path = os.path.join(HERE, "config.env")
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    env.update({k: v for k, v in os.environ.items() if k.startswith("JOBS_")})
    return env


ENV = load_env()


try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("America/Toronto")
except Exception:  # no tz database: fall back to the machine's clock
    TZ = None


def now_local():
    """Toronto time, even on Lambda (which runs in UTC)."""
    return datetime.now(TZ).replace(tzinfo=None) if TZ else datetime.now()


def in_quiet_hours():
    """QUIET_HOURS=23-7 (Toronto time): hold non-⭐ alerts overnight and send them as one message at 7."""
    q = ENV.get("QUIET_HOURS", "23-7")
    if not q:
        return False
    start, end = (int(x) for x in q.split("-"))
    h = now_local().hour
    return h >= start or h < end if start > end else start <= h < end


# Applied/Hide/pipeline marks, one file per profile. Local files by default; the Lambda swaps in S3 versions.
def _load_status_file(profile=ME):
    try:
        return json.load(open(os.path.join(os.path.dirname(DB), profile.status_file)))
    except FileNotFoundError:
        return {}


def _save_status_file(st, profile=ME):
    json.dump(st, open(os.path.join(os.path.dirname(DB), profile.status_file), "w"), indent=1)


load_status, save_status = _load_status_file, _save_status_file


def log(*a):
    print(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


# ---------------------------------------------------------------- storage
DB_WRITE = threading.Lock()  # scan threads fetch in parallel but write one at a time


def connect():
    con = sqlite3.connect(DB, timeout=60)
    # WAL: the web UI and ad-hoc reads never block a scan's writes (and vice versa)
    con.execute("PRAGMA journal_mode=WAL")
    return con


def db():
    con = connect()
    con.executescript("""
    CREATE TABLE IF NOT EXISTS jobs (
        key TEXT, id TEXT, company TEXT, title TEXT, location TEXT, url TEXT,
        first_seen TEXT, matched INTEGER DEFAULT 0, reason TEXT, priority INTEGER DEFAULT 0,
        min_years INTEGER, alerted INTEGER DEFAULT 0, PRIMARY KEY (key, id));
    CREATE TABLE IF NOT EXISTS sources (
        key TEXT PRIMARY KEY, seeded INTEGER DEFAULT 0, fails INTEGER DEFAULT 0,
        last_ok TEXT, last_error TEXT);
    """)
    con.execute("""CREATE TABLE IF NOT EXISTS discovered (
        ats TEXT, id TEXT, name TEXT, canada_hist INTEGER, probed TEXT, ca_now INTEGER, added INTEGER DEFAULT 0,
        PRIMARY KEY (ats, id))""")
    for col in ("posted TEXT", "dedup TEXT", "descr TEXT", "last_seen TEXT", "closed INTEGER DEFAULT 0",
                "held INTEGER DEFAULT 0"):  # added after the first version shipped
        try:
            con.execute(f"ALTER TABLE jobs ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    con.execute("CREATE INDEX IF NOT EXISTS jobs_dedup ON jobs(dedup)")
    # jobs = every posting ever seen (shared facts); matches = which profile wants it.
    # jobs.matched now means "at least one profile wants it" (closure checks, backfill).
    con.execute("""CREATE TABLE IF NOT EXISTS matches (
        profile TEXT, key TEXT, id TEXT, priority INTEGER DEFAULT 0, alerted INTEGER DEFAULT 0,
        held INTEGER DEFAULT 0, reason TEXT DEFAULT '', PRIMARY KEY (profile, key, id))""")
    if not con.execute("SELECT 1 FROM matches WHERE profile='me' LIMIT 1").fetchone():
        with con:  # one-time move of the original single-profile matches
            con.execute("""INSERT OR IGNORE INTO matches (profile, key, id, priority, alerted, held, reason)
                           SELECT 'me', key, id, priority, alerted, COALESCE(held, 0), COALESCE(reason, '')
                           FROM jobs WHERE matched=1""")
    return con


def dedup_key(j):
    """Same job reached via two feeds (e.g. the GitHub list + the company's own board)."""
    first_word = (re.findall(r"[a-z0-9]+", j["company"].lower()) or ["?"])[0]
    return first_word + ":" + re.sub(r"[^a-z0-9]", "", j["title"].lower())


# ---------------------------------------------------------------- notifications (ntfy.sh)
def notify(title, message, url=None, priority=3, tags=None, profile=ME):
    topic = ENV.get(profile.topic_env)
    if not topic:  # never fall back to someone else's phone
        log(f"NOTIFY (no {profile.topic_env} set):", title, "|", message)
        return
    body = {"topic": topic, "title": title[:250], "message": message[:3900], "priority": priority,
            "tags": tags or []}
    if url:
        body["click"] = url
        body["actions"] = [{"action": "view", "label": "Open posting", "url": url}]
    req = urllib.request.Request(ENV.get("JOBS_NTFY_SERVER", "https://ntfy.sh"), data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    for attempt in range(3):
        try:
            urllib.request.urlopen(req, timeout=20).read()
            return
        except Exception as e:  # ntfy rate limit / network blip
            log("ntfy failed:", e)
            time.sleep(5 * (attempt + 1))


def alert_job(j, profile=ME):
    star = j.get("entry") or j.get("start") == "2027"
    yrs = j.get("min_years")
    lines = [j["company"], j["location"][:150]] + (["Starts 2027 ✓"] if j.get("start") == "2027" else [])
    if yrs is not None:
        lines.append(f"Asks for {yrs}+ yrs experience")
    notify(("⭐ " if star else "") + j["title"], "\n".join(lines), url=j["url"],
           priority=5 if star else 4, tags=["briefcase"], profile=profile)


# ---------------------------------------------------------------- scanning
def scan_company(c, con_path, dry, max_years, max_age, seed_detail_days):
    """Returns (matches_to_alert, stats). Runs in a thread; opens its own DB connection."""
    key = f'{c["ats"]}:{c["id"]}'
    con = connect()
    try:
        seeded = con.execute("SELECT seeded, fails FROM sources WHERE key=?", (key,)).fetchone()
        seen = {r[0] for r in con.execute("SELECT id FROM jobs WHERE key=?", (key,))}
    finally:
        con.close()
    first_run = not (seeded and seeded[0])
    is_seen = lambda j: j["id"] in seen

    lister = sources.LISTERS[c["ats"]]
    jobs = lister(c, is_seen) if c["ats"] == "workday" else lister(c)
    new = [j for j in {j["id"]: j for j in jobs}.values() if not is_seen(j)]
    alerts, rows, match_rows, detail_calls = [], [], [], 0
    now = datetime.now().isoformat(timespec="seconds")

    for j in new:
        # each profile's cheap title check; details are fetched once if anyone is interested
        verdicts = {p.id: p.title_verdict(j["title"]) for p in PROFILES}
        wanted = [p for p in PROFILES if verdicts[p.id][0]]
        reason = verdicts[ME.id][1] if not verdicts[ME.id][0] else ""
        age = j.get("age_days")
        too_old = age is not None and age > (seed_detail_days if first_run else max_age)
        if wanted and too_old:
            wanted, reason = [], f"old ({age:.0f}d)"
        if wanted and not filters.is_canada(j) and not filters.location_needs_details(j) \
                and c["ats"] not in sources.DETAILERS:
            wanted, reason = [], "not Canada"
        if wanted and c["ats"] in sources.DETAILERS:
            # cheap location reject before spending a request, unless the listing is vague
            if c["ats"] == "greenhouse" and not filters.is_canada(j) and not filters.location_needs_details(j) \
                    and "canada" not in j["location"].lower():
                wanted, reason = [], "not Canada"
            else:
                try:
                    sources.DETAILERS[c["ats"]](c, j)
                    detail_calls += 1
                except Exception as e:
                    log(f"  details failed {c['name']} {j['title']}: {e}")
        kept = {}
        for p in wanted:
            keep, why, prio = filters.full_verdict(j, p.max_years)
            if keep:
                kept[p.id] = prio
            elif p is ME or not reason:
                reason = why
        if kept:
            reason = ""
        posted = (datetime.now() - timedelta(days=age)).strftime("%Y-%m-%d") if age is not None else None
        rows.append((key, j["id"], j["company"], j["title"], j["location"][:500], j["url"], now,
                     int(bool(kept)), reason, kept.get(ME.id, 0), j.get("min_years"), int(ME.id in kept and not first_run),
                     posted, dedup_key(j), (j.get("description") or "")[:6000] if kept else None, now))
        for pid, prio in kept.items():
            match_rows.append((pid, key, j["id"], prio, int(not first_run)))
            if not first_run:
                alerts.append((pid, j))

    if not dry:
        with DB_WRITE:
            _save_company(key, jobs, rows, match_rows, now, c)
    matched = sum(r[7] for r in rows)
    return alerts, dict(listed=len(jobs), new=len(new), matched=matched, details=detail_calls, first_run=first_run,
                        recovered=bool(seeded and seeded[1]))


def _save_company(key, jobs, rows, match_rows, now, c):
    """One short transaction per company. Always commits or rolls back, and always closes:
    a connection left mid-transaction holds the write lock and freezes every later scan."""
    con = connect()
    try:
        with con:  # commit on success, rollback on any error
            _write_company(con, key, jobs, rows, match_rows, now, c)
    finally:
        con.close()


def _write_company(con, key, jobs, rows, match_rows, now, c):
    con.executemany("""INSERT OR IGNORE INTO jobs (key, id, company, title, location, url, first_seen, matched, reason,
                       priority, min_years, alerted, posted, dedup, descr, last_seen) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", rows)
    con.executemany("INSERT OR IGNORE INTO matches (profile, key, id, priority, alerted) VALUES (?,?,?,?,?)", match_rows)
    # Closed-job detection: matched jobs still listed get last_seen; on feeds that return the whole
    # board, a matched job that's no longer listed has been taken down.
    listed = {j["id"] for j in jobs}
    open_ids = [r[0] for r in con.execute("SELECT id FROM jobs WHERE key=? AND matched=1 AND closed=0", (key,))]
    con.executemany("UPDATE jobs SET last_seen=? WHERE key=? AND id=?", [(now, key, i) for i in open_ids if i in listed])
    if sources.is_complete(c["ats"], len(jobs)) and jobs:
        gone = [i for i in open_ids if i not in listed]
        if len(gone) <= max(5, len(open_ids) // 2):  # a sudden mass disappearance is an API glitch, not closures
            con.executemany("UPDATE jobs SET closed=1 WHERE key=? AND id=?", [(key, i) for i in gone])
    con.execute("""INSERT INTO sources(key, seeded, fails, last_ok) VALUES (?,1,0,?)
                   ON CONFLICT(key) DO UPDATE SET seeded=1, fails=0, last_ok=excluded.last_ok, last_error=NULL""",
                (key, now))


TRANSIENT = re.compile(r"HTTP Error (429|5\d\d)|timed out|Temporary failure|Connection reset|RemoteDisconnected")


def record_failure(con, key, name, err):
    with DB_WRITE:
        _record_failure(con, key, err)
    fails = con.execute("SELECT fails FROM sources WHERE key=?", (key,)).fetchone()[0]
    if TRANSIENT.search(err):  # rate limit / server hiccup: the feed is still there, so wait much longer before saying so
        if fails == 24:
            notify(f"job-watcher: {name} keeps refusing requests", f"Failed 24 runs in a row; every other company is "
                   f"still being scanned.\n{err[:300]}", priority=2, tags=["warning"])
    elif fails == 6:  # ~1.5 hours of failures: the feed probably moved
        notify(f"job-watcher: {name} feed broken", f"Failed 6 runs in a row; every other company is still being "
               f"scanned.\n{err[:300]}", priority=2, tags=["warning"])


def _record_failure(con, key, err):
    con.execute("""INSERT INTO sources(key, fails, last_error) VALUES (?,1,?)
                   ON CONFLICT(key) DO UPDATE SET fails=fails+1, last_error=excluded.last_error""", (key, err[:500]))
    con.commit()


def all_companies(con):
    """companies.json plus companies added by `discover` (kept in the database so the Lambda can add them)."""
    listed = json.load(open(os.path.join(HERE, "companies.json")))["companies"]
    have = {(c["ats"], c["id"].lower()) for c in listed}
    extra = [{"name": n, "ats": a, "id": i, "note": "auto-discovered"} for a, i, n in
             con.execute("SELECT ats, id, name FROM discovered WHERE added=1") if (a, i.lower()) not in have]
    return [c for c in listed + extra if c.get("enabled", True)]


def cmd_scan(dry=False):
    """Only one scan at a time: if the previous one is still running, skip this turn."""
    import fcntl
    lock = open(DB + ".scan.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("previous scan still running; skipping this one")
        return {"alerted": 0, "matched": 0, "seeded": False}
    try:
        return _scan(dry)
    finally:
        lock.close()


def _scan(dry=False):
    setup = db()
    companies = all_companies(setup)
    setup.close()
    max_years, max_age = int(ENV["MAX_YEARS"]), float(ENV["MAX_AGE_DAYS"])
    con = db()
    t0 = time.time()
    run_start = datetime.now().isoformat(timespec="seconds")
    last_ok = dict(con.execute("SELECT key, last_ok FROM sources WHERE last_ok IS NOT NULL"))
    def due(c):  # big/slow feeds can ask to be polled less often, e.g. "every_minutes": 60
        lo = last_ok.get(f'{c["ats"]}:{c["id"]}')
        return not (c.get("every_minutes") and lo) or \
            (datetime.now() - datetime.fromisoformat(lo)).total_seconds() >= c["every_minutes"] * 60 - 120
    companies = [c for c in companies if due(c)]
    # before this scan adds any matches, so a new profile's empty page is noticed
    seeded_any = any([seed_profile(con, p) for p in PROFILES]) if not dry else False
    recovered_any = False
    all_alerts, totals, failed = [], {"listed": 0, "new": 0, "matched": 0, "details": 0}, []
    with cf.ThreadPoolExecutor(int(ENV.get("THREADS", 16))) as ex:
        futs = {ex.submit(scan_company, c, DB, dry, max_years, max_age, float(ENV["SEED_DETAIL_DAYS"])): c for c in companies}
        for f in cf.as_completed(futs):
            c = futs[f]
            try:
                alerts, st = f.result()
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
                log(f"  FAIL {c['name']}: {err}")
                failed.append(c["name"])
                if not dry:
                    record_failure(con, f'{c["ats"]}:{c["id"]}', c["name"], err)
                continue
            for k in totals:
                totals[k] += st[k]
            if st["new"] or st["first_run"]:
                log(f"  {c['name']:<22} listed={st['listed']:<5} new={st['new']:<4} matched={st['matched']}"
                    + ("  (first run: saved silently)" if st["first_run"] else ""))
            all_alerts += alerts
            seeded_any = seeded_any or st["first_run"]
            recovered_any = recovered_any or st["recovered"]

    # a feed that failed and now works again must be saved, or the stored fail count only ever grows
    changed = recovered_any
    alerted = 0
    for p in PROFILES:
        sent, ch = _alert_profile(con, p, [j for pid, j in all_alerts if pid == p.id], run_start, dry)
        alerted, changed = alerted + sent, changed or ch
    if not dry:
        write_open_json(con)
    log(f"scan done in {time.time() - t0:.0f}s: {len(companies)} companies, {totals['listed']} listed, "
        f"{totals['new']} new, {totals['matched']} matched, {alerted} alerted, "
        f"{totals['details']} detail fetches, {len(failed)} failed {failed if failed else ''}")
    return {"alerted": alerted, "matched": totals["matched"], "seeded": seeded_any, "changed": changed}


def _set_match(con, p, j, **cols):
    sets = ", ".join(f"{k}=?" for k in cols)
    with DB_WRITE, con:
        con.execute(f"UPDATE matches SET {sets} WHERE profile=? AND key=? AND id=?", (*cols.values(), p.id, j["key"], j["id"]))


def _alert_profile(con, p, new_matches, run_start, dry):
    """One profile's new matches -> phone. Returns (alerts sent, whether held state changed)."""
    tag = "" if p is ME else f"[{p.id}] "
    # drop cross-feed duplicates: already matched earlier via another feed, or twice in this run
    unique, keys = [], set()
    for j in sorted(new_matches, key=lambda j: j["source"] == "simplify"):  # prefer the company's own link
        dk = dedup_key(j)
        earlier = con.execute("""SELECT 1 FROM matches m JOIN jobs j ON j.key=m.key AND j.id=m.id
                                 WHERE m.profile=? AND j.dedup=? AND j.first_seen < ? LIMIT 1""", (p.id, dk, run_start)).fetchone()
        if dk in keys or earlier:
            log(f"  {tag}dup skipped: {j['company']} | {j['title']}")
            if not dry:
                _set_match(con, p, j, alerted=0, reason="duplicate")
            continue
        keys.add(dk)
        unique.append(j)
    alerts = []
    unclear_ok = now_local().strftime("%Y-%m-%d") >= ENV["UNCLEAR_ALERTS_FROM"]
    for j in unique:
        j = dict(j, start=filters.start_verdict(j["title"], j.get("description")) if p.start_gate else "")
        if p.start_gate and (j["start"] == "now" or (j["start"] == "" and not unclear_ok)):
            log(f"  {tag}no alert ({'starts before May 2027' if j['start'] else 'start date not stated'}): {j['company']} | {j['title']}")
            continue
        if p.star_alerts_only and not j.get("entry"):
            continue  # on the web page and in the digest, no buzz
        alerts.append(j)
    alerts.sort(key=lambda j: (j.get("start") != "2027", not j.get("entry"), j["company"]))
    changed = False
    if in_quiet_hours():  # overnight: only ⭐ jobs buzz; the rest wait for one morning message
        held = [j for j in alerts if not j.get("entry") and j.get("start") != "2027"]
        alerts = [j for j in alerts if j.get("entry") or j.get("start") == "2027"]
        if held and not dry:
            for j in held:
                _set_match(con, p, j, held=1)
            changed = True
            log(f"  {tag}quiet hours: holding {len(held)} alerts")
    elif not dry:
        held = con.execute("""SELECT j.company, j.title FROM matches m JOIN jobs j ON j.key=m.key AND j.id=m.id
                              WHERE m.profile=? AND m.held=1 ORDER BY m.priority DESC""", (p.id,)).fetchall()
        if held:
            notify(f"Overnight: {len(held)} new jobs", "\n".join(f"• {t} — {c}" for c, t in held[:30])
                   + ("" if len(held) <= 30 else f"\n…and {len(held) - 30} more on the web page"), priority=4,
                   tags=["briefcase"], profile=p)
            with DB_WRITE, con:
                con.execute("UPDATE matches SET held=0 WHERE profile=? AND held=1", (p.id,))
            changed = True
    cap = int(ENV["MAX_ALERTS_PER_RUN"])
    for j in alerts[:cap]:
        log(f"  {tag}ALERT {'*' if j.get('entry') else ' '} {j['company']} | {j['title']} | {j['location'][:60]} | {j['url']}")
        if not dry:
            alert_job(j, p)
    if len(alerts) > cap and not dry:
        notify(f"+{len(alerts) - cap} more new jobs", "They're on your web page and in the next digest.", priority=3, profile=p)
    return len(alerts), changed


def seed_profile(con, p, days=45):
    """A newly added profile starts with an empty page (every posting was already 'seen' before it
    existed), so fill it once from postings of the last N days. Title + location only: older postings
    have no stored description, so the years-of-experience check can't run on them. Never alerts."""
    if p is ME or con.execute("SELECT 1 FROM matches WHERE profile=? LIMIT 1", (p.id,)).fetchone():
        return False
    rows = con.execute("""SELECT key, id, title, location FROM jobs WHERE closed=0
                          AND COALESCE(posted, substr(first_seen,1,10)) >= date('now','localtime', ?)""", (f"-{days} days",)).fetchall()
    picked = [(p.id, k, i, 2 if filters.ENTRY.search(t) else 1, 0) for k, i, t, loc in rows
              if p.title_verdict(t)[0] and filters.is_canada({"location": loc, "country": "CA" if k.startswith("jobbank") else None})]
    with DB_WRITE, con:
        con.executemany("INSERT OR IGNORE INTO matches (profile, key, id, priority, alerted) VALUES (?,?,?,?,?)", picked)
        con.executemany("UPDATE jobs SET matched=1 WHERE key=? AND id=?", [(k, i) for _, k, i, _, _ in picked])
    log(f"  [{p.id}] seeded {len(picked)} matches from the last {days} days")
    return True


def matches_since(con, hours, p=ME):
    """New matches the profile could actually take; for the start-gated profile, 2027 starts first and
    jobs wanting a start before May 2027 are left out."""
    rows = con.execute("""SELECT j.company, j.title, j.location, j.url, m.priority, j.min_years, j.first_seen, j.descr
                          FROM matches m JOIN jobs j ON j.key=m.key AND j.id=m.id
                          WHERE m.profile=? AND j.closed=0 AND m.reason != 'duplicate' AND j.first_seen >= datetime('now','localtime', ?)
                          ORDER BY m.priority DESC, j.first_seen DESC""", (p.id, f"-{hours} hours")).fetchall()
    out = []
    for r in rows:
        st = filters.start_verdict(r[1], r[7]) if p.start_gate else ""
        if st != "now":
            out.append(r[:4] + ((2 if st == "2027" else r[4]),) + r[5:7])
    return sorted(out, key=lambda r: -r[4])


def check_closed_workday(con, limit=150):
    """Workday lists are only paged partially, so closure is checked per job: a taken-down posting 404s."""
    import urllib.error
    rows = con.execute("""SELECT key, id, url FROM jobs WHERE matched=1 AND closed=0 AND key LIKE 'workday:%'
                          ORDER BY COALESCE(last_seen, first_seen) LIMIT ?""", (limit,)).fetchall()
    closed = 0
    for key, jid, url in rows:
        tenant, wd, site = key.split(":", 1)[1].split("/")
        path = url.split(f"/{site}", 1)[1] if f"/{site}" in url else None
        if not path:
            continue
        try:
            sources.http_json(f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}{path}")
            con.execute("UPDATE jobs SET last_seen=? WHERE key=? AND id=?", (datetime.now().isoformat(timespec="seconds"), key, jid))
        except urllib.error.HTTPError as e:
            if e.code in (404, 410):
                con.execute("UPDATE jobs SET closed=1 WHERE key=? AND id=?", (key, jid))
                closed += 1
        except Exception:
            pass
    con.commit()
    log(f"workday closure check: {len(rows)} checked, {closed} closed")
    return closed


FOLLOW_UP_DAYS = 14


def follow_ups(p=ME):
    """Applications with no update for 2 weeks, nudged at most once per stage."""
    st, due, today = load_status(p), [], now_local().date()
    for k, v in st.items():
        stage = v.get("stage") or ("applied" if v.get("state") == "applied" else None)
        if stage not in ("applied", "oa", "interview"):
            continue
        since = (v.get("stage_at") or v.get("at") or "")[:10]
        try:
            idle = (today - datetime.fromisoformat(since).date()).days
        except ValueError:
            continue
        if idle >= FOLLOW_UP_DAYS and v.get("nudged") != f"{stage}:{since}":
            due.append((idle, stage, v))
            v["nudged"] = f"{stage}:{since}"
    if due:
        save_status(st, p)
    return sorted(due, key=lambda x: -x[0])


def cmd_digest(hours=12):
    con = db()
    part = "Morning" if now_local().hour < 12 else "Evening"
    if part == "Morning":
        check_closed_workday(con)
    for p in PROFILES:
        _digest(con, p, hours, part)


def _digest(con, p, hours, part):
    rows = matches_since(con, hours, p)
    nudges = follow_ups(p) if part == "Morning" else []
    if not rows:
        msg = f"No new {p.label.lower()} in Canada in the last {hours}h."
    else:
        msg = "\n".join(f"{'⭐' if r[4] == 2 else '•'} {r[1]} — {r[0]} ({r[2][:40]})" for r in rows[:40])
        if len(rows) > 40:
            msg += f"\n…and {len(rows) - 40} more on your web page"
    if nudges:
        label = {"applied": "no reply", "oa": "OA, no news", "interview": "interviewed, no news"}
        msg += "\n\n📌 Follow up (" + str(len(nudges)) + "):\n" + "\n".join(
            f"• {v.get('title', '?')} — {v.get('company', '?')} ({label[stage]} for {idle}d)" for idle, stage, v in nudges[:10])
    if p is ME:  # feed health is the owner's problem only
        broken = con.execute("SELECT key FROM sources WHERE fails >= 6").fetchall()
        if broken:
            msg += f"\n\n⚠ feeds failing: {', '.join(b[0] for b in broken)}"
    notify(f"{part} job digest: {len(rows)} new", msg, priority=3, tags=["newspaper"], profile=p)
    log(f"digest sent to {p.id}: {len(rows)} matches in last {hours}h")


def open_matches(con, days=45, profile=ME):
    """A profile's matched jobs posted in the last N days (or first seen then, if the feed has no date), one per job.
    Where a job came through two feeds, the company's own board wins over the GitHub list."""
    rows = con.execute("""SELECT j.company, j.title, j.location, j.url, m.priority, j.min_years, COALESCE(j.posted, substr(j.first_seen,1,10)) p,
                                 j.dedup, j.key, j.id, j.first_seen, j.descr
                          FROM matches m JOIN jobs j ON j.key=m.key AND j.id=m.id
                          WHERE m.profile=? AND j.closed=0 AND m.reason != 'duplicate' AND p >= date('now','localtime', ?)
                          ORDER BY p DESC, m.priority DESC, j.key LIKE 'simplify:%'""", (profile.id, f"-{int(float(days))} days")).fetchall()
    seen = set()
    return [r for r in rows if not (r[7] in seen or seen.add(r[7]))]


def open_jobs_payload(con, profile=ME):
    """Everything the web UI shows, one dict per open match."""
    rows = open_matches(con, profile=profile)
    has_date = {r[0]: r[1] for r in con.execute("SELECT key, MAX(posted IS NOT NULL) FROM jobs GROUP BY key")}
    setup = dict(con.execute("SELECT key, MIN(first_seen) FROM jobs GROUP BY key"))
    out = [{"k": f"{r[8]}|{r[9]}", "company": r[0], "title": r[1], "location": r[2][:120], "url": r[3],
            **dict(zip(("fit", "skills", "flags"), fit.score(r[1], r[11], r[2], r[4] == 2, r[5]))),
            "start": filters.start_verdict(r[1], r[11]) if profile.start_gate else "",
            "star": r[4] == 2, "years": r[5], "first_seen": r[10], "source": r[8].split(":")[0],
            # feeds without dates: "posted" is unknown, and jobs found on the very first scan are of unknown age
            "posted": r[6] if has_date.get(r[8]) else None,
            "found_at_setup": not has_date.get(r[8]) and r[10][:13] == (setup.get(r[8]) or "")[:13]} for r in rows]
    for j in out:
        if not profile.fit:  # the resume score is the owner's resume; flags (French, clearance) still apply
            j["fit"], j["skills"] = None, []
        elif j["start"] == "2027":  # a confirmed 2027 start is worth more than any skill keyword
            j["fit"] = min(100, j["fit"] + 10)
    # newest first; jobs of unknown age (dateless feed, found at setup) go last
    out.sort(key=lambda j: (j["posted"] or ("" if j["found_at_setup"] else j["first_seen"][:10])), reverse=True)
    return out


def write_open_json(con):
    json.dump(open_jobs_payload(con), open(OPEN_JSON, "w"))


def cmd_serve(port=8765):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import urlparse, parse_qsl
    write_open_json(db())
    store = webui.FileStore(OPEN_JSON, STATUS_JSON)

    class H(BaseHTTPRequestHandler):
        def _go(self, method):
            u = urlparse(self.path)
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode() if method == "POST" else ""
            code, ctype, out = webui.handle(method, u.path, dict(parse_qsl(u.query)), body, store)
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.end_headers()
            self.wfile.write(out.encode())

        def do_GET(self):
            self._go("GET")

        def do_POST(self):
            self._go("POST")

        def log_message(self, *a):
            pass

    log(f"web UI on http://localhost:{port}  (Ctrl+C to stop)")
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()


def cmd_backfill():
    """Fetch descriptions for matches saved before descriptions were stored (needed for fit scores)."""
    con = db()
    comps = {f'{c["ats"]}:{c["id"]}': c for c in all_companies(con)}
    todo = {}
    for key, jid, title, loc, url in con.execute(
            "SELECT key, id, title, location, url FROM jobs WHERE matched=1 AND closed=0 AND COALESCE(descr,'')=''"):
        if key in comps:
            todo.setdefault(key, []).append({"id": jid, "title": title, "location": loc, "url": url, "company": comps[key]["name"]})

    def one(key):
        c, rows, got = comps[key], todo[key], {}
        ats = c["ats"]
        if ats in sources.DETAILERS:
            for j in rows:
                if ats == "workday":
                    site = c["id"].split("/")[2]
                    j["_path"] = j["url"].split(f"/{site}", 1)[1]
                elif ats == "oracle":
                    j["_host"], j["_site"] = c["id"].split("|")
                elif ats == "eightfold":
                    j["_host"], j["_domain"] = c["id"].split("|")
                try:
                    sources.DETAILERS[ats](c, j)
                    got[j["id"]] = j.get("description") or ""
                except Exception:
                    pass
        else:  # description comes with the listing: list once and copy it over
            listing = {x["id"]: x for x in sources.LISTERS[ats](c)}
            for j in rows:
                if j["id"] in listing:
                    got[j["id"]] = listing[j["id"]].get("description") or ""
        return key, got

    filled = 0
    with cf.ThreadPoolExecutor(12) as ex:
        for f in cf.as_completed([ex.submit(one, k) for k in todo]):
            try:
                key, got = f.result()
            except Exception as e:
                log("  backfill failed:", e)
                continue
            con.executemany("UPDATE jobs SET descr=? WHERE key=? AND id=?", [(d[:6000], key, i) for i, d in got.items() if d])
            filled += sum(1 for d in got.values() if d)
    con.commit()
    log(f"backfill: {sum(len(v) for v in todo.values())} matches without a description, filled {filled}")


def cmd_recent(days=30):
    rows = open_matches(db(), days)
    for r in rows:
        yrs = "" if r[5] is None else f" [{r[5]}+y]"
        print(f"{'*' if r[4] == 2 else ' '} {r[6]}  {r[0][:20]:<20} {r[1][:70]:<70} {r[2][:35]:<35}{yrs}\n      {r[3]}")
    print(f"\n{len(rows)} open matches posted in the last {days} day(s)   (* = explicitly entry-level / new grad)")


if __name__ == "__main__":
    args = sys.argv[1:]
    cmd = args[0] if args and not args[0].startswith("-") else "scan"
    try:
        if cmd == "scan":
            cmd_scan(dry="--dry-run" in args)
        elif cmd == "digest":
            cmd_digest(int(args[1]) if len(args) > 1 else 12)
        elif cmd == "recent":
            cmd_recent(args[1] if len(args) > 1 else 30)
        elif cmd == "backfill":
            cmd_backfill()
        elif cmd == "serve":
            cmd_serve(int(args[1]) if len(args) > 1 else 8765)
        elif cmd == "test-notify":
            notify("job-watcher test", "If you see this, job alerts will reach your phone.", priority=3, tags=["tada"])
            log("test notification sent")
        else:
            print(__doc__)
    except Exception:
        log("CRASH\n" + traceback.format_exc())
        raise
