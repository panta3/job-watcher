"""Who the watcher hunts for. Every scan lists each company once; each profile filters the same
listings its own way, gets its own phone alerts, password, web page and Applied/Hide marks.
"""
from dataclasses import dataclass
from typing import Callable

import filters


@dataclass(frozen=True)
class Profile:
    id: str
    label: str                      # shown on the web page
    title_verdict: Callable         # cheap title-only filter: title -> (keep, reason)
    topic_env: str                  # env var with this person's ntfy topic (no topic = no alerts)
    password_env: str               # env var with this person's web page password
    status_file: str                # Applied/Hide marks (local file name, and S3 key on AWS)
    max_years: int = 2              # drop postings that ask for more experience than this
    start_gate: bool = False        # can't start before May 2027: hold jobs that don't say when they start
    fit: bool = False               # score jobs against the resume in fit.py
    star_alerts_only: bool = False  # buzz only for explicit entry-level/new-grad jobs; the rest wait for the digest
    school: str = ""                # for the "alumni there" LinkedIn link


PROFILES = [
    Profile("me", "Tech roles", filters.title_verdict, "JOBS_NTFY_TOPIC", "JOBS_UI_PASSWORD", "status.json",
            start_gate=True, fit=True, school="McMaster"),
    # a business grad who can start now. Business titles are ~10x more common than tech ones, so
    # only explicit entry-level jobs buzz the phone; everything else is on the page and in the digest.
    Profile("biz", "Business roles", filters.business_title_verdict, "JOBS_NTFY_TOPIC_BIZ", "JOBS_UI_PASSWORD_BIZ",
            "status-biz.json", star_alerts_only=True),
]
BY_ID = {p.id: p for p in PROFILES}
ME = BY_ID["me"]
