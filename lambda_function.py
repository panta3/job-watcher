"""AWS Lambda entry point: scheduled scans/digests, and the web UI (password sign-in) via a function URL.

State lives in S3: jobs.db (the SQLite database) and status.json (your Applied/Hide marks).
To stay at $0, jobs.db is only written back once an hour, or right away when an alert went out.
Between uploads a warm container keeps its newer local copy; a cold one re-checks a few
already-seen jobs, which costs nothing but a little time.
"""
import base64
import hashlib
import hmac
import json
import os
import time
from datetime import datetime
from urllib.parse import parse_qs

os.environ.setdefault("JOBS_DB", "/tmp/jobs.db")

import boto3  # noqa: E402  (preinstalled in the Lambda Python runtime)
from botocore.exceptions import ClientError  # noqa: E402

import watcher  # noqa: E402
import webui  # noqa: E402
from profiles import ME, PROFILES  # noqa: E402

s3 = boto3.client("s3")
BUCKET = os.environ["JOBS_BUCKET"]
DB = os.environ["JOBS_DB"]
ETAG_FILE = DB + ".etag"


def _etag():
    try:
        return s3.head_object(Bucket=BUCKET, Key="jobs.db")["ETag"]
    except ClientError as e:
        if e.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
            return None
        raise


def sync_down():
    """Download jobs.db unless this warm container already holds the latest (or a newer) copy."""
    remote = _etag()
    local = open(ETAG_FILE).read() if os.path.exists(ETAG_FILE) and os.path.exists(DB) else None
    if remote is None:
        for p in (DB, ETAG_FILE):
            if os.path.exists(p):
                os.remove(p)
    elif remote != local:
        for p in (DB + "-wal", DB + "-shm"):  # stale WAL from an older copy must not be replayed onto the new one
            if os.path.exists(p):
                os.remove(p)
        s3.download_file(BUCKET, "jobs.db", DB)
        open(ETAG_FILE, "w").write(remote)


def sync_up():
    # WAL mode keeps recent writes in jobs.db-wal; fold them into jobs.db before uploading it
    con = watcher.connect()
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.close()
    s3.upload_file(DB, BUCKET, "jobs.db")
    open(ETAG_FILE, "w").write(_etag() or "")


class S3Store:
    """webui storage for one profile: open jobs come from jobs.db, marks from that profile's status file."""

    def __init__(self, profile=ME):
        self.profile = profile

    def load_jobs(self):
        sync_down()
        return watcher.open_jobs_payload(watcher.db(), self.profile)

    def updated(self):
        try:
            from zoneinfo import ZoneInfo
            ts = s3.head_object(Bucket=BUCKET, Key="jobs.db")["LastModified"]
            return ts.astimezone(ZoneInfo("America/Toronto")).strftime("%b %d, %H:%M")
        except ClientError:
            return ""

    def load_status(self):
        try:
            return json.loads(s3.get_object(Bucket=BUCKET, Key=self.profile.status_file)["Body"].read())
        except ClientError as e:
            if e.response["Error"]["Code"] in ("404", "NoSuchKey"):
                return {}
            raise

    def save_status(self, status):
        s3.put_object(Bucket=BUCKET, Key=self.profile.status_file, Body=json.dumps(status).encode(), ContentType="application/json")


# follow-up reminders (digest) read and write the same status files the web pages use
watcher.load_status = lambda p=ME: S3Store(p).load_status()
watcher.save_status = lambda st, p=ME: S3Store(p).save_status(st)


# each profile has its own password; the password you type decides whose page you get.
# The cookie holds a hash, not the password; changing a password signs that person's devices out.
os.environ[ME.password_env]  # the owner's password is required
PASSWORDS = {p.id: os.environ.get(p.password_env, "") for p in PROFILES}
SESSIONS = {hashlib.sha256(f"job-watcher|{pid}|{pw}".encode()).hexdigest(): pid for pid, pw in PASSWORDS.items() if pw}
PROFILE_BY_ID = {p.id: p for p in PROFILES}
LOGIN_PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Job Watcher</title><body style="font:16px system-ui;display:grid;place-items:center;min-height:90vh;margin:0">
<form method=post action=/login style="display:grid;gap:10px;width:260px"><b>Job Watcher</b>%s
<input type=password name=p placeholder=Password autofocus required style="font:inherit;padding:8px">
<button style="font:inherit;padding:8px">Open</button></form>"""


def _page(code, ctype, out, **extra):
    return {"statusCode": code, "headers": {"Content-Type": ctype, "Cache-Control": "no-store"}, "body": out, **extra}


def web(event):
    http = event["requestContext"]["http"]
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode()
    path = event.get("rawPath") or "/"
    if path == "/logout":  # back to the password box, e.g. to open the other person's page
        return {"statusCode": 303, "headers": {"Location": "/", "Cache-Control": "no-store"},
                "cookies": ["jw=; Max-Age=0; Path=/; HttpOnly; Secure; SameSite=Lax"]}
    if http["method"] == "POST" and path == "/login":
        given = (parse_qs(body).get("p") or [""])[0].encode()
        for session, pid in SESSIONS.items():
            if hmac.compare_digest(given, PASSWORDS[pid].encode()):
                return {"statusCode": 303, "headers": {"Location": "/"},
                        "cookies": [f"jw={session}; Max-Age=31536000; Path=/; HttpOnly; Secure; SameSite=Lax"]}
        time.sleep(1)  # makes guessing slow
        return _page(401, "text/html; charset=utf-8", LOGIN_PAGE % "<span style=color:#c00>Wrong password</span>")
    profile = None
    for c in event.get("cookies") or []:
        name, _, value = c.strip().partition("=")
        for session, pid in SESSIONS.items():
            if name == "jw" and hmac.compare_digest(value.encode(), session.encode()):
                profile = PROFILE_BY_ID[pid]
    if not profile:
        if path.startswith("/api/"):
            return _page(401, "text/plain", "sign in first")
        return _page(200, "text/html; charset=utf-8", LOGIN_PAGE % "")
    code, ctype, out = webui.handle(http["method"], path, event.get("queryStringParameters") or {}, body,
                                    S3Store(profile), profile=profile)
    return _page(code, ctype, out)


def handler(event, context):
    event = event or {}
    if "requestContext" in event:  # function URL = someone opened the web UI
        return web(event)

    sync_down()
    action = event.get("action", "scan")
    if action == "digest":
        watcher.cmd_digest(int(event.get("hours", 12)))
        sync_up()  # the morning digest also marks closed Workday postings
    elif action == "test-notify":
        watcher.notify("job-watcher (AWS) test", "Lambda can reach your phone.", tags=["tada"])
    else:
        result = watcher.cmd_scan()
        if result["alerted"] or result["seeded"] or result.get("changed") or datetime.now().minute < 15:
            sync_up()
    return {"ok": True, "action": action}
