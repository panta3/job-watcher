"""AWS Lambda entry point: scheduled scans/digests, and the web UI via a function URL.

State lives in S3: jobs.db (the SQLite database) and status.json (your Applied/Hide marks).
To stay at $0, jobs.db is only written back once an hour, or right away when an alert went out.
Between uploads a warm container keeps its newer local copy; a cold one re-checks a few
already-seen jobs, which costs nothing but a little time.
"""
import base64
import json
import os
from datetime import datetime

os.environ.setdefault("JOBS_DB", "/tmp/jobs.db")

import boto3  # noqa: E402  (preinstalled in the Lambda Python runtime)
from botocore.exceptions import ClientError  # noqa: E402

import watcher  # noqa: E402
import webui  # noqa: E402

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
    """webui storage: open jobs come from jobs.db, marks from status.json."""

    def load_jobs(self):
        sync_down()
        return watcher.open_jobs_payload(watcher.db())

    def updated(self):
        try:
            from zoneinfo import ZoneInfo
            ts = s3.head_object(Bucket=BUCKET, Key="jobs.db")["LastModified"]
            return ts.astimezone(ZoneInfo("America/Toronto")).strftime("%b %d, %H:%M")
        except ClientError:
            return ""

    def load_status(self):
        try:
            return json.loads(s3.get_object(Bucket=BUCKET, Key="status.json")["Body"].read())
        except ClientError as e:
            if e.response["Error"]["Code"] in ("404", "NoSuchKey"):
                return {}
            raise

    def save_status(self, status):
        s3.put_object(Bucket=BUCKET, Key="status.json", Body=json.dumps(status).encode(), ContentType="application/json")


# follow-up reminders (digest) read and write the same status.json the web page uses
watcher.load_status = lambda: S3Store().load_status()
watcher.save_status = lambda st: S3Store().save_status(st)


def web(event):
    http = event["requestContext"]["http"]
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode()
    path = event.get("rawPath") or "/"
    code, ctype, out = webui.handle(http["method"], path, event.get("queryStringParameters") or {}, body,
                                    S3Store(), token=os.environ["JOBS_UI_TOKEN"])
    return {"statusCode": code, "headers": {"Content-Type": ctype, "Cache-Control": "no-store"}, "body": out}


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
