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


# Applied/Hide/pipeline marks. Local file by default; the Lambda swaps in S3 versions.
def _load_status_file():
    try:
        return json.load(open(STATUS_JSON))
    except FileNotFoundError:
        return {}


def _save_status_file(st):
    json.dump(st, open(STATUS_JSON, "w"), indent=1)


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
    return con


def dedup_key(j):
    """Same job reached via two feeds (e.g. the GitHub list + the company's own board)."""
    first_word = (re.findall(r"[a-z0-9]+", j["company"].lower()) or ["?"])[0]
    return first_word + ":" + re.sub(r"[^a-z0-9]", "", j["title"].lower())


# ---------------------------------------------------------------- notifications (ntfy.sh)
def notify(title, message, url=None, priority=3, tags=None):
    topic = ENV.get("JOBS_NTFY_TOPIC")
    if not topic:
        log("NOTIFY (no JOBS_NTFY_TOPIC set):", title, "|", message)
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


def alert_job(j):
    star = j.get("entry") or j.get("start") == "2027"
    yrs = j.get("min_years")
    lines = [j["company"], j["location"][:150]] + (["Starts 2027 ✓"] if j.get("start") == "2027" else [])
    if yrs is not None:
        lines.append(f"Asks for {yrs}+ yrs experience")
    notify(("⭐ " if star else "") + j["title"], "\n".join(lines), url=j["url"],
           priority=5 if star else 4, tags=["briefcase"])


# ---------------------------------------------------------------- scanning
def scan_company(c, con_path, dry, max_years, max_age, seed_detail_days):
    """Returns (matches_to_alert, stats). Runs in a thread; opens its own DB connection."""
    key = f'{c["ats"]}:{c["id"]}'
    con = connect()
    try:
        seeded = con.execute("SELECT seeded FROM sources WHERE key=?", (key,)).fetchone()
        seen = {r[0] for r in con.execute("SELECT id FROM jobs WHERE key=?", (key,))}
    finally:
        con.close()
    first_run = not (seeded and seeded[0])
    is_seen = lambda j: j["id"] in seen

    lister = sources.LISTERS[c["ats"]]
    jobs = lister(c, is_seen) if c["ats"] == "workday" else lister(c)
    new = [j for j in {j["id"]: j for j in jobs}.values() if not is_seen(j)]
    alerts, rows, detail_calls = [], [], 0
    now = datetime.now().isoformat(timespec="seconds")

    for j in new:
        keep, reason = filters.title_verdict(j["title"])
        prio = 0
        age = j.get("age_days")
        too_old = age is not None and age > (seed_detail_days if first_run else max_age)
        if keep and too_old:
            keep, reason = False, f"old ({age:.0f}d)"
        if keep and not filters.is_canada(j) and not filters.location_needs_details(j) \
                and c["ats"] not in sources.DETAILERS:
            keep, reason = False, "not Canada"
        if keep and c["ats"] in sources.DETAILERS:
            # cheap location reject before spending a request, unless the listing is vague
            if c["ats"] == "greenhouse" and not filters.is_canada(j) and not filters.location_needs_details(j) \
                    and "canada" not in j["location"].lower():
                keep, reason = False, "not Canada"
            else:
                try:
                    sources.DETAILERS[c["ats"]](c, j)
                    detail_calls += 1
                except Exception as e:
                    log(f"  details failed {c['name']} {j['title']}: {e}")
        if keep:
            keep, reason, prio = filters.full_verdict(j, max_years)
        posted = (datetime.now() - timedelta(days=age)).strftime("%Y-%m-%d") if age is not None else None
        rows.append((key, j["id"], j["company"], j["title"], j["location"][:500], j["url"], now,
                     int(keep), reason, prio, j.get("min_years"), int(keep and not first_run), posted, dedup_key(j),
                     (j.get("description") or "")[:6000] if keep else None, now))
        if keep and not first_run:
            alerts.append(j)

    if not dry:
        with DB_WRITE:
            _save_company(key, jobs, rows, now, c)
    matched = sum(r[7] for r in rows)
    return alerts, dict(listed=len(jobs), new=len(new), matched=matched, details=detail_calls, first_run=first_run)


def _save_company(key, jobs, rows, now, c):
    """One short transaction per company. Always commits or rolls back, and always closes:
    a connection left mid-transaction holds the write lock and freezes every later scan."""
    con = connect()
    try:
        with con:  # commit on success, rollback on any error
            _write_company(con, key, jobs, rows, now, c)
    finally:
        con.close()


def _write_company(con, key, jobs, rows, now, c):
    con.executemany("""INSERT OR IGNORE INTO jobs (key, id, company, title, location, url, first_seen, matched, reason,
                       priority, min_years, alerted, posted, dedup, descr, last_seen) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", rows)
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


def record_failure(con, key, name, err):
    with DB_WRITE:
        _record_failure(con, key, err)
    fails = con.execute("SELECT fails FROM sources WHERE key=?", (key,)).fetchone()[0]
    if fails == 6:  # ~1.5 hours of failures: the feed probably moved
        notify(f"job-watcher: {name} feed broken", f"Failed 6 runs in a row.\n{err[:300]}", priority=2, tags=["warning"])


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
    seeded_any = False
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

    # drop cross-feed duplicates: already matched earlier via another feed, or twice in this run
    unique, keys = [], set()
    for j in sorted(all_alerts, key=lambda j: j["source"] == "simplify"):  # prefer the company's own link
        dk = dedup_key(j)
        earlier = con.execute("SELECT 1 FROM jobs WHERE dedup=? AND matched=1 AND first_seen < ? LIMIT 1", (dk, run_start)).fetchone()
        if dk in keys or earlier:
            log(f"  dup skipped: {j['company']} | {j['title']}")
            if not dry:
                with DB_WRITE, con:
                    con.execute("UPDATE jobs SET alerted=0, reason='duplicate' WHERE key=? AND id=?", (j["key"], j["id"]))
            continue
        keys.add(dk)
        unique.append(j)
    all_alerts = []
    unclear_ok = now_local().strftime("%Y-%m-%d") >= ENV["UNCLEAR_ALERTS_FROM"]
    for j in unique:
        j["start"] = filters.start_verdict(j["title"], j.get("description"))
        if j["start"] == "now" or (j["start"] == "" and not unclear_ok):
            log(f"  no alert ({'starts before May 2027' if j['start'] else 'start date not stated'}): {j['company']} | {j['title']}")
            continue
        all_alerts.append(j)
    all_alerts.sort(key=lambda j: (j.get("start") != "2027", not j.get("entry"), j["company"]))
    changed = False
    if in_quiet_hours():  # overnight: only ⭐ jobs buzz; the rest wait for one morning message
        held = [j for j in all_alerts if not j.get("entry") and j.get("start") != "2027"]
        all_alerts = [j for j in all_alerts if j.get("entry") or j.get("start") == "2027"]
        if held and not dry:
            with DB_WRITE, con:
                con.executemany("UPDATE jobs SET held=1 WHERE key=? AND id=?", [(j["key"], j["id"]) for j in held])
            changed = True
            log(f"  quiet hours: holding {len(held)} alerts")
    elif not dry:
        held = con.execute("SELECT company, title, url FROM jobs WHERE held=1 ORDER BY priority DESC").fetchall()
        if held:
            notify(f"Overnight: {len(held)} new jobs", "\n".join(f"• {t} — {c}" for c, t, u in held[:30])
                   + ("" if len(held) <= 30 else f"\n…and {len(held) - 30} more on the web page"), priority=4, tags=["briefcase"])
            with DB_WRITE, con:
                con.execute("UPDATE jobs SET held=0 WHERE held=1")
            changed = True
    cap = int(ENV["MAX_ALERTS_PER_RUN"])
    for j in all_alerts[:cap]:
        log(f"  ALERT {'*' if j.get('entry') else ' '} {j['company']} | {j['title']} | {j['location'][:60]} | {j['url']}")
        if not dry:
            alert_job(j)
    if len(all_alerts) > cap and not dry:
        notify(f"+{len(all_alerts) - cap} more new jobs", "Run `python3 watcher.py recent` or wait for the digest.", priority=3)
    if not dry:
        write_open_json(con)
    log(f"scan done in {time.time() - t0:.0f}s: {len(companies)} companies, {totals['listed']} listed, "
        f"{totals['new']} new, {totals['matched']} matched, {len(all_alerts)} alerted, "
        f"{totals['details']} detail fetches, {len(failed)} failed {failed if failed else ''}")
    return {"alerted": len(all_alerts), "matched": totals["matched"], "seeded": seeded_any, "changed": changed}


def matches_since(con, hours):
    """New matches you could actually take (not wanting a start before May 2027); 2027 starts first."""
    rows = con.execute("""SELECT company, title, location, url, priority, min_years, first_seen, descr FROM jobs
                          WHERE matched=1 AND closed=0 AND reason != 'duplicate' AND first_seen >= datetime('now','localtime', ?)
                          ORDER BY priority DESC, first_seen DESC""", (f"-{hours} hours",)).fetchall()
    out = []
    for r in rows:
        st = filters.start_verdict(r[1], r[7])
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


def follow_ups():
    """Applications with no update for 2 weeks, nudged at most once per stage."""
    st, due, today = load_status(), [], now_local().date()
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
        save_status(st)
    return sorted(due, key=lambda x: -x[0])


def cmd_digest(hours=12):
    con = db()
    rows = [r for r in matches_since(con, hours)]
    broken = con.execute("SELECT key FROM sources WHERE fails >= 3").fetchall()
    part = "Morning" if now_local().hour < 12 else "Evening"
    nudges = follow_ups() if part == "Morning" else []
    if part == "Morning":
        check_closed_workday(con)
    if not rows:
        msg = f"No new entry-level tech jobs in Canada in the last {hours}h."
    else:
        msg = "\n".join(f"{'⭐' if r[4] == 2 else '•'} {r[1]} — {r[0]} ({r[2][:40]})" for r in rows[:40])
        if len(rows) > 40:
            msg += f"\n…and {len(rows) - 40} more (python3 watcher.py recent)"
    if nudges:
        label = {"applied": "no reply", "oa": "OA, no news", "interview": "interviewed, no news"}
        msg += "\n\n📌 Follow up (" + str(len(nudges)) + "):\n" + "\n".join(
            f"• {v.get('title', '?')} — {v.get('company', '?')} ({label[stage]} for {idle}d)" for idle, stage, v in nudges[:10])
    if broken:
        msg += f"\n\n⚠ feeds failing: {', '.join(b[0] for b in broken)}"
    notify(f"{part} job digest: {len(rows)} new", msg, priority=3, tags=["newspaper"])
    log(f"digest sent: {len(rows)} matches in last {hours}h")


def open_matches(con, days=45):
    """Matched jobs posted in the last N days (or first seen then, if the feed has no date), one per job.
    Where a job came through two feeds, the company's own board wins over the GitHub list."""
    rows = con.execute("""SELECT company, title, location, url, priority, min_years, COALESCE(posted, substr(first_seen,1,10)) p,
                                 dedup, key, id, first_seen, descr
                          FROM jobs WHERE matched=1 AND closed=0 AND reason != 'duplicate' AND p >= date('now','localtime', ?)
                          ORDER BY p DESC, priority DESC, key LIKE 'simplify:%'""", (f"-{int(float(days))} days",)).fetchall()
    seen = set()
    return [r for r in rows if not (r[7] in seen or seen.add(r[7]))]


def open_jobs_payload(con):
    """Everything the web UI shows, one dict per open match."""
    rows = open_matches(con)
    has_date = {r[0]: r[1] for r in con.execute("SELECT key, MAX(posted IS NOT NULL) FROM jobs GROUP BY key")}
    setup = dict(con.execute("SELECT key, MIN(first_seen) FROM jobs GROUP BY key"))
    out = [{"k": f"{r[8]}|{r[9]}", "company": r[0], "title": r[1], "location": r[2][:120], "url": r[3],
            **dict(zip(("fit", "skills", "flags"), fit.score(r[1], r[11], r[2], r[4] == 2, r[5]))),
            "start": filters.start_verdict(r[1], r[11]),
            "star": r[4] == 2, "years": r[5], "first_seen": r[10], "source": r[8].split(":")[0],
            # feeds without dates: "posted" is unknown, and jobs found on the very first scan are of unknown age
            "posted": r[6] if has_date.get(r[8]) else None,
            "found_at_setup": not has_date.get(r[8]) and r[10][:13] == (setup.get(r[8]) or "")[:13]} for r in rows]
    for j in out:  # a confirmed 2027 start is worth more than any skill keyword
        if j["start"] == "2027":
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
