"""One fetcher per hiring system (ATS). Each returns a list of Job dicts.

list_jobs(company) -> cheap listing (1 request, or a few pages for Workday)
details(company, job) -> fills in description/locations; only called for new,
                         title-matched jobs so we don't hammer anyone.
"""
import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

UA = "Mozilla/5.0 (job-watcher; personal job alerts)"
TIMEOUT = 25


def http_json(url, body=None):
    headers = {"User-Agent": UA, "Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            # back off on rate limits / flaky servers; give up on real errors (404 etc.)
            if e.code not in (429, 500, 502, 503, 504) or attempt == 2:
                raise
            time.sleep(int(e.headers.get("Retry-After") or 0) or 10 * (attempt + 1))


def strip_html(s):
    s = html.unescape(s or "")
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def iso_age_days(ts):
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds() / 86400


def job(company, jid, title, location, url, age_days=None, country=None, description=""):
    return {
        "company": company["name"], "source": company["ats"], "key": f'{company["ats"]}:{company["id"]}',
        "id": str(jid), "title": (title or "").strip(), "location": location or "",
        "url": url, "age_days": age_days, "country": country, "description": description,
    }


# ---------------------------------------------------------------- Greenhouse
def greenhouse_list(c):
    d = http_json(f'https://boards-api.greenhouse.io/v1/boards/{c["id"]}/jobs')
    return [job(c, j["id"], j["title"], (j.get("location") or {}).get("name"), j["absolute_url"],
                iso_age_days(j.get("first_published") or j.get("updated_at")))
            for j in d["jobs"]]


def greenhouse_details(c, j):
    d = http_json(f'https://boards-api.greenhouse.io/v1/boards/{c["id"]}/jobs/{j["id"]}')
    j["description"] = strip_html(d.get("content"))
    offices = [o.get("location") or o.get("name") or "" for o in d.get("offices", [])]
    j["location"] = "; ".join(filter(None, [j["location"], *offices]))


# ---------------------------------------------------------------- Lever
def lever_list(c):
    d = http_json(f'https://api.lever.co/v0/postings/{c["id"]}?mode=json')
    out = []
    for p in d:
        cats = p.get("categories") or {}
        locs = cats.get("allLocations") or [cats.get("location") or ""]
        desc = " ".join([p.get("descriptionPlain") or "", p.get("additionalPlain") or ""] +
                        [strip_html(l.get("content")) for l in p.get("lists") or []])
        age = (datetime.now(timezone.utc).timestamp() * 1000 - p["createdAt"]) / 86400000 if p.get("createdAt") else None
        out.append(job(c, p["id"], p.get("text"), "; ".join(locs), p.get("hostedUrl"), age,
                       country=p.get("country"), description=desc))
    return out


# ---------------------------------------------------------------- Ashby
def ashby_list(c):
    d = http_json(f'https://api.ashbyhq.com/posting-api/job-board/{c["id"]}')
    out = []
    for p in d["jobs"]:
        if p.get("isListed") is False:
            continue
        addr = ((p.get("address") or {}).get("postalAddress") or {})
        locs = [p.get("location") or ""]
        for s in p.get("secondaryLocations") or []:
            locs.append(s.get("location") or "")
            sa = (s.get("address") or {}).get("postalAddress") or {}
            locs.append(sa.get("addressCountry") or "")
        locs.append(", ".join(filter(None, [addr.get("addressLocality"), addr.get("addressRegion"), addr.get("addressCountry")])))
        out.append(job(c, p["id"], p.get("title"), "; ".join(filter(None, locs)), p.get("jobUrl"),
                       iso_age_days(p.get("publishedAt")), country=addr.get("addressCountry"),
                       description=p.get("descriptionPlain") or ""))
    return out


# ---------------------------------------------------------------- SmartRecruiters
def smartrecruiters_list(c):
    out, offset = [], 0
    while True:
        d = http_json(f'https://api.smartrecruiters.com/v1/companies/{c["id"]}/postings?country=ca&limit=100&offset={offset}')
        for p in d["content"]:
            loc = p.get("location") or {}
            out.append(job(c, p["id"], p.get("name"), loc.get("fullLocation"),
                           f'https://jobs.smartrecruiters.com/{c["id"]}/{p["id"]}',
                           iso_age_days(p.get("releasedDate")), country=loc.get("country")))
        offset += 100
        if offset >= d["totalFound"] or offset >= 1000:
            return out


def smartrecruiters_details(c, j):
    d = http_json(f'https://api.smartrecruiters.com/v1/companies/{c["id"]}/postings/{j["id"]}')
    secs = ((d.get("jobAd") or {}).get("sections") or {})
    j["description"] = " ".join(strip_html(s.get("text")) for s in secs.values() if isinstance(s, dict))


# ---------------------------------------------------------------- Amazon
def amazon_list(c):
    out = []
    for offset in (0, 100):
        d = http_json(f"https://www.amazon.jobs/en/search.json?country=CAN&sort=recent&result_limit=100&offset={offset}")
        for p in d["jobs"]:
            try:
                age = (datetime.now() - datetime.strptime(p["posted_date"], "%B %d, %Y")).total_seconds() / 86400
            except (KeyError, ValueError):
                age = None
            # Amazon writes locations as "CA, ON, Toronto" (country first): reverse it
            loc = ", ".join(reversed([x.strip() for x in (p.get("location") or "").split(",")]))
            desc = strip_html((p.get("basic_qualifications") or "") + " " + (p.get("preferred_qualifications") or ""))
            out.append(job(c, p["id_icims"], p["title"], loc.replace(", CA", ", Canada"),
                           "https://www.amazon.jobs" + p["job_path"], age,
                           country="CA" if p.get("country_code") == "CAN" else p.get("country_code"),
                           description=desc))
    return out


# ---------------------------------------------------------------- Workday
_WD_POSTED = re.compile(r"(\d+)\+? Days? Ago", re.I)


def _wd_age(text):
    t = (text or "").lower()
    if "today" in t:
        return 0
    if "yesterday" in t:
        return 1
    m = _WD_POSTED.search(t)
    return int(m.group(1)) if m else None


def _wd_canada_facet(facets):
    """Find the facet value that means 'Canada' (country facets differ per company)."""
    hits = []

    def walk(nodes, param):
        for n in nodes or []:
            if "values" in n:
                walk(n["values"], n.get("facetParameter") or param)
            elif (n.get("descriptor") or "").strip().lower() == "canada" and param:
                hits.append((param, n["id"]))

    walk(facets, None)
    rank = lambda p: (0 if "ountry" in p[0] else 1 if "ierarchy" in p[0] else 2)
    hits = [h for h in hits if not re.search(r"state|province|city", h[0], re.I)]
    return sorted(hits, key=rank)[0] if hits else None


def workday_list(c, is_seen, max_pages=8):
    """Workday lists newest-first-ish; page until a full page is already seen."""
    tenant, wd, site = c["id"].split("/")
    base = f"https://{tenant}.{wd}.myworkdayjobs.com"
    api = f"{base}/wday/cxs/{tenant}/{site}/jobs"
    first = http_json(api, {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""})
    facet = _wd_canada_facet(first.get("facets"))
    applied = {facet[0]: [facet[1]]} if facet else {}
    # Many banks pin old "evergreen" postings above the date-sorted list (TD pins ~50, RBC ~9),
    # so an all-seen page only means "nothing new" once we've reached the sorted part: either we
    # saw ages drop from old (3+ days) back to fresh (0-1), or the list was sorted from the top.
    out, ages, dropped = [], [], False
    for page in range(max_pages):
        d = first if (page == 0 and not facet) else http_json(
            api, {"appliedFacets": applied, "limit": 20, "offset": page * 20, "searchText": ""})
        posts = [p for p in d.get("jobPostings") or [] if p.get("externalPath")]
        batch = [job(c, p["externalPath"].rsplit("_", 1)[-1] if "_" in p["externalPath"] else p["externalPath"],
                     p.get("title"), p.get("locationsText"), f'{base}/{site}{p["externalPath"]}',
                     _wd_age(p.get("postedOn")), country="CA" if facet else None) | {"_path": p["externalPath"]}
                 for p in posts]
        out += batch
        for a in (j["age_days"] for j in batch if j["age_days"] is not None):
            if ages and max(ages) >= 3 and a <= 1:
                dropped = True
            ages.append(a)
        sorted_from_top = bool(ages) and ages[0] <= 1 and all(x <= y for x, y in zip(ages, ages[1:]))
        if len(posts) < 20 or (all(is_seen(j) for j in batch) and (dropped or sorted_from_top)):
            break
    return out


def workday_details(c, j):
    tenant, wd, site = c["id"].split("/")
    d = http_json(f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}{j['_path']}")["jobPostingInfo"]
    j["description"] = strip_html(d.get("jobDescription"))
    j["location"] = "; ".join(filter(None, [d.get("location"), *(d.get("additionalLocations") or []),
                                             ((d.get("country") or {}).get("descriptor"))]))


# ---------------------------------------------------------------- Jibe / iCIMS career sites (AMD, ...)
def jibe_list(c):
    """c["id"] = career-site host, e.g. careers.amd.com"""
    out, page = [], 1
    while page <= 5:
        d = http_json(f'https://{c["id"]}/api/jobs?location=Canada&page={page}&limit=100&sortBy=posted_date&descending=true&internal=false')
        for p in (x["data"] for x in d["jobs"]):
            out.append(job(c, p["req_id"], p.get("title"), p.get("full_location") or p.get("location_name"),
                           p.get("canonical_url") or p.get("apply_url"), iso_age_days((p.get("posted_date") or "").replace("+0000", "+00:00")),
                           country=p.get("country_code"), description=strip_html(p.get("description"))))
        if page * 100 >= d.get("totalCount", 0):
            return out
        page += 1
    return out


# ---------------------------------------------------------------- Eightfold "pcsx" career sites (Microsoft)
def eightfold_list(c):
    """c["id"] = host|domain, e.g. apply.careers.microsoft.com|microsoft.com"""
    host, domain = c["id"].split("|")
    out, start = [], 0
    while start < 300:
        d = json.loads(http_text(f"https://{host}/api/pcsx/search?domain={domain}&location=Canada&start={start}&sort_by=timestamp",
                                 {"Accept": "application/json", "Referer": f"https://{host}/careers"}))["data"]
        for p in d["positions"]:
            age = (datetime.now(timezone.utc).timestamp() - p["postedTs"]) / 86400 if p.get("postedTs") else None
            out.append(job(c, p["id"], p.get("name"), "; ".join(p.get("locations") or []),
                           f"https://{host}{p.get('positionUrl') or ''}", age) | {"_domain": domain, "_host": host})
        start += len(d["positions"])
        if not d["positions"] or start >= d.get("count", 0):
            return out
    return out


def eightfold_details(c, j):
    d = json.loads(http_text(f"https://{j['_host']}/api/pcsx/position_details?position_id={j['id']}&domain={j['_domain']}&hl=en",
                             {"Accept": "application/json", "Referer": f"https://{j['_host']}/careers"}))["data"]
    j["description"] = strip_html(d.get("jobDescription"))
    if d.get("publicUrl"):
        j["url"] = d["publicUrl"]


# ---------------------------------------------------------------- SAP SuccessFactors career sites (Scotiabank, Rogers, TELUS)
def successfactors_list(c):
    """c["id"] = career-site host. Uses the site's built-in RSS feed, newest first, full description included."""
    import xml.etree.ElementTree as ET
    from email.utils import parsedate_to_datetime
    req = urllib.request.Request(f'https://{c["id"]}/services/rss/job/?locale=en_US&keywords=&sortColumn=referencedate&sortDirection=desc',
                                 headers={"User-Agent": BROWSER_UA, "Accept": "application/rss+xml,application/xml;q=0.9,*/*;q=0.8"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        root = ET.fromstring(r.read())
    out = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        m = re.match(r"(.*)\s+\(([^()]*)\)\s*$", title)   # "Job title (City, ON, CA, M1K5L1)"
        name, loc = (m.group(1), m.group(2)) if m else (title, "")
        link = (it.findtext("link") or "").split("?")[0]
        jid = re.search(r"/(\d+)/?$", link)
        try:
            age = (datetime.now(timezone.utc) - parsedate_to_datetime(it.findtext("pubDate"))).total_seconds() / 86400
        except Exception:
            age = None
        country = "CA" if re.search(r",\s*CA(,|$)", loc) else None   # SuccessFactors uses ISO country codes here
        out.append(job(c, jid.group(1) if jid else link, name, loc, link, age, country=country,
                       description=strip_html(it.findtext("description"))))
    return out


# ---------------------------------------------------------------- small-company hiring systems
def workable_list(c):
    d = http_json(f'https://apply.workable.com/api/v1/widget/accounts/{c["id"]}?details=true')
    out = []
    for p in d.get("jobs") or []:
        locs = p.get("locations") or [{"city": p.get("city"), "region": p.get("state"), "country": p.get("country"), "countryCode": None}]
        loc = "; ".join(", ".join(filter(None, [l.get("city"), l.get("region"), l.get("country")])) for l in locs)
        cc = next((l.get("countryCode") for l in locs if (l.get("countryCode") or "").upper() == "CA"), None) or \
             ("CA" if any((l.get("country") or "").lower() == "canada" for l in locs) else None)
        out.append(job(c, p["shortcode"], p.get("title"), loc, p.get("url"), iso_age_days(p.get("published_on")),
                       country=cc, description=strip_html(p.get("description"))))
    return out


def rippling_list(c):
    out, page = [], 0
    while page < 10:
        d = http_json(f'https://ats.rippling.com/api/v2/board/{c["id"]}/jobs?page={page}')
        for p in d.get("items") or []:
            locs = p.get("locations") or []
            loc = "; ".join(", ".join(filter(None, [l.get("city"), l.get("state"), l.get("country")])) or l.get("name", "") for l in locs)
            cc = "CA" if any(l.get("countryCode") == "CA" for l in locs) else None
            out.append(job(c, p["id"], p.get("name"), loc, p.get("url"), None, country=cc))
        page += 1
        if page >= (d.get("totalPages") or 1):
            return out
    return out


def rippling_details(c, j):
    d = http_json(f'https://ats.rippling.com/api/v2/board/{c["id"]}/jobs/{j["id"]}')
    desc = d.get("description") or {}
    j["description"] = strip_html(" ".join(v for v in desc.values() if isinstance(v, str)) if isinstance(desc, dict) else str(desc))


def bamboohr_list(c):
    d = http_json(f'https://{c["id"]}.bamboohr.com/careers/list')
    out = []
    for p in d.get("result") or []:
        a = p.get("atsLocation") or {}
        loc = ", ".join(filter(None, [a.get("city"), a.get("state") or a.get("province"), a.get("country")]))
        out.append(job(c, p["id"], p.get("jobOpeningName"), loc, f'https://{c["id"]}.bamboohr.com/careers/{p["id"]}',
                       None, country="CA" if (a.get("country") or "").lower() == "canada" else None))
    return out


def bamboohr_details(c, j):
    d = http_json(f'https://{c["id"]}.bamboohr.com/careers/{j["id"]}/detail')["result"]["jobOpening"]
    j["description"] = strip_html(d.get("description"))
    if d.get("datePosted"):
        j["age_days"] = iso_age_days(d["datePosted"])


def recruitee_list(c):
    d = http_json(f'https://{c["id"]}.recruitee.com/api/offers/')
    out = []
    for p in d.get("offers") or []:
        loc = "; ".join(", ".join(filter(None, [l.get("city"), l.get("state"), l.get("country")])) for l in p.get("locations") or []) \
            or ", ".join(filter(None, [p.get("city"), p.get("country")]))
        cc = "CA" if (p.get("country_code") or "").upper() == "CA" or "canada" in loc.lower() else None
        out.append(job(c, p["id"], p.get("title"), loc, p.get("careers_url"), iso_age_days((p.get("published_at") or "").replace(" UTC", "+00:00").replace(" ", "T")),
                       country=cc, description=strip_html((p.get("description") or "") + " " + (p.get("requirements") or ""))))
    return out


# ---------------------------------------------------------------- community new-grad list (SimplifyJobs on GitHub)
def simplify_list(c):
    """Crowd-maintained new-grad list; catches small companies we don't watch directly.
    c["id"] = GitHub repo, e.g. SimplifyJobs/New-Grad-Positions"""
    d = http_json(f'https://raw.githubusercontent.com/{c["id"]}/dev/.github/scripts/listings.json')
    now = datetime.now(timezone.utc).timestamp()
    out = []
    for p in d:
        if not p.get("active") or p.get("is_visible") is False:
            continue
        age = (now - p["date_posted"]) / 86400 if p.get("date_posted") else None
        if age is not None and age > 30:
            continue
        j = job(c, p["id"], p.get("title"), "; ".join(p.get("locations") or []), p.get("url"), age)
        j["company"] = p.get("company_name") or "?"
        out.append(j)
    return out


# ---------------------------------------------------------------- Job Bank (Government of Canada) — lots of small employers
JOBBANK_SEARCHES = ["software developer", "software engineer", "programmer", "web developer", "data analyst",
                    "data scientist", "data engineer", "machine learning", "cloud", "devops", "cybersecurity",
                    "security analyst", "QA analyst", "network", "database", "IT support", "computer systems"]


def jobbank_list(c):
    import xml.etree.ElementTree as ET
    ns = {"a": "http://www.w3.org/2005/Atom"}
    out = {}
    for q in JOBBANK_SEARCHES:
        req = urllib.request.Request(
            "https://www.jobbank.gc.ca/jobsearch/feed/jobSearchRSSfeed?sort=D&searchstring=" + urllib.parse.quote_plus(q),
            headers={"User-Agent": UA, "Accept": "application/rss+xml,application/xml,text/xml,*/*"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            root = ET.fromstring(r.read())
        for e in root.findall("a:entry", ns):
            link = e.find("a:link", ns).get("href")
            summ = strip_html(e.findtext("a:summary", "", ns))
            loc = re.search(r"Location:\s*(.*?)\s*(Employer:|Salary:|$)", summ)
            emp = re.search(r"Employer:\s*(.*?)\s*(Salary:|$)", summ)
            jid = link.rstrip("/").rsplit("/", 1)[-1]
            j = job(c, jid, (e.findtext("a:title", "", ns) or "").strip().capitalize(),
                    (loc.group(1) if loc else "") + ", Canada", link, iso_age_days(e.findtext("a:updated", "", ns)), country="CA")
            j["company"] = (emp.group(1) if emp else "Unknown employer") + " (Job Bank)"
            out[jid] = j
    return list(out.values())


def jobbank_details(c, j):
    req = urllib.request.Request(j["url"], headers={"User-Agent": UA, "Accept": "text/html"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        j["description"] = strip_html(r.read().decode("utf-8", "replace"))[:20000]


# ---------------------------------------------------------------- Oracle Recruiting Cloud (JPMorgan, Nokia, Amex, Oracle, ...)
def oracle_list(c):
    """c["id"] = host|siteNumber, e.g. jpmc.fa.oraclecloud.com|CX_1001. Server-side Canada filter."""
    host, site = c["id"].split("|")
    out = []
    for offset in (0, 100, 200):
        finder = f"findReqs;siteNumber={site},location=Canada,limit=100,offset={offset},sortBy=POSTING_DATES_DESC"
        d = http_json(f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions?onlyData=true"
                      f"&expand=requisitionList.secondaryLocations&finder=" + urllib.parse.quote(finder))
        item = (d.get("items") or [{}])[0]
        reqs = item.get("requisitionList") or []
        for r in reqs:
            locs = [r.get("PrimaryLocation") or ""] + [l.get("Name") or "" for l in r.get("secondaryLocations") or []]
            cc = "CA" if r.get("PrimaryLocationCountry") == "CA" or any(l.get("CountryCode") == "CA" for l in r.get("secondaryLocations") or []) else None
            out.append(job(c, r["Id"], r.get("Title"), "; ".join(filter(None, locs)),
                           f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{r['Id']}",
                           iso_age_days(r.get("PostedDate")), country=cc) | {"_host": host, "_site": site})
        if len(reqs) < 100:
            return out
    return out


def oracle_details(c, j):
    finder = f'ById;Id="{j["id"]}",siteNumber={j["_site"]}'
    d = http_json(f"https://{j['_host']}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails?expand=all&onlyData=true&finder="
                  + urllib.parse.quote(finder))["items"][0]
    j["description"] = strip_html(" ".join(d.get(k) or "" for k in ("ExternalDescriptionStr", "ExternalResponsibilitiesStr", "ExternalQualificationsStr")))


# ---------------------------------------------------------------- big companies with their own career sites
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"


def http_text(url, headers=None, body=None):
    h = {"User-Agent": BROWSER_UA, "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9"}
    h.update(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=h)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read().decode("utf-8", "replace")


def google_list(c):
    """Google careers search (server-rendered HTML), Canada filter, newest first, 3 pages."""
    out = []
    for page in (1, 2, 3):
        b = http_text(f"https://www.google.com/about/careers/applications/jobs/results?location=Canada&sort_by=date&page={page}")
        cards = b.split('<li class="lLd3Je"')[1:]
        for card in cards:
            jid = re.search(r"ssk='\d+:(\d+)'", card)
            title = re.search(r'<h3 class="QJPWVe">([^<]+)</h3>', card)
            if not (jid and title):
                continue
            locs = [html.unescape(x).lstrip("; ") for x in re.findall(r'<span class="r0wTof[^"]*">([^<]+)</span>', card)]
            level = re.search(r'aria-label="(Early|Mid|Advanced|Director)[^"]*experience', card)
            level = level.group(1) if level else ""
            # the experience level stands in for a description: Early -> entry, Advanced -> filtered out as 5+ yrs
            desc = {"Early": "early career role", "Advanced": "5 years of experience", "Director": "10 years of experience"}.get(level, "")
            out.append(job(c, jid.group(1), html.unescape(title.group(1)), "; ".join(locs),
                           f"https://www.google.com/about/careers/applications/jobs/results/{jid.group(1)}", None,
                           country="CA", description=desc))
        if len(cards) < 20:
            break
    return out


def ibm_list(c):
    d = json.loads(http_text("https://www-api.ibm.com/search/api/v2", {"Origin": "https://www.ibm.com"}, {
        "appId": "careers", "scopes": ["careers2"], "query": {"bool": {"must": []}},
        "post_filter": {"term": {"field_keyword_05": "Canada"}}, "size": 100, "sort": [{"dcdate": "desc"}],
        "_source": ["_id", "title", "url", "description", "field_keyword_19", "dcdate"]}))
    return [job(c, h["_id"], h["_source"].get("title"), (h["_source"].get("field_keyword_19") or "").replace(", CA", ", Canada"),
                h["_source"].get("url"), iso_age_days(h["_source"].get("dcdate")), country="CA",
                description=strip_html(h["_source"].get("description")))
            for h in d["hits"]["hits"]]


def atlassian_list(c):
    d = json.loads(http_text("https://www.atlassian.com/endpoint/careers/listings", {"Accept": "application/json"}))
    out = []
    for p in d:
        post = p.get("portalJobPost") or {}
        try:
            age = (datetime.now() - datetime.strptime(post.get("updatedDate", ""), "%Y-%m-%d %I:%M %p")).total_seconds() / 86400
        except ValueError:
            age = None
        out.append(job(c, p["id"], p.get("title"), "; ".join(p.get("locations") or []), post.get("portalUrl") or p.get("applyUrl"),
                       age, description=strip_html((p.get("qualifications") or "") + " " + (p.get("responsibilities") or ""))))
    return out


def shopify_list(c):
    """Shopify's careers page embeds its Ashby postings (title, date, link). Shopify is remote-first with
    Canada as home base, so these are treated as open to Canada."""
    b = http_text("https://www.shopify.com/careers")
    out = {}
    for m in re.finditer(r'\\"([^"\\]{3,160})\\",\\"(\d{4}-\d{2}-\d{2})\\",\\"(https://www\.shopify\.com/careers\?ashby_jid=([0-9a-f-]{36}))', b):
        title, date, url, jid = m.groups()
        out[jid] = job(c, jid, title, "Canada / Americas (remote-first)", url, iso_age_days(date), country="CA")
    return list(out.values())


def successfactors_search_list(c):
    """SuccessFactors sites whose RSS ignores location (EY): read the site's own Canada search page."""
    host, _, q = c["id"].partition("|")
    b = http_text(f"https://{host}/search/?q=&locationsearch={q or 'canada'}&sortColumn=referencedate&sortDirection=desc")
    out = {}
    for row in b.split('class="data-row')[1:]:
        a = re.search(r'href="([^"]+/job/[^"]+)"[^>]*class="jobTitle-link[^"]*"[^>]*>([^<]+)<', row) or \
            re.search(r'class="jobTitle-link[^"]*"[^>]*href="([^"]+)"[^>]*>([^<]+)<', row)
        if not a:
            continue
        loc = re.search(r'class="jobLocation[^"]*"[^>]*>\s*([^<]+?)\s*<', row)
        date = re.search(r'class="jobDate[^"]*"[^>]*>\s*([A-Z][a-z]{2} \d{1,2}, \d{4})\s*<', row)
        age = (datetime.now() - datetime.strptime(date.group(1), "%b %d, %Y")).total_seconds() / 86400 if date else None
        link = a.group(1) if a.group(1).startswith("http") else f"https://{host}{a.group(1)}"
        jid = re.search(r"/(\d+)/?$", link.split("?")[0])
        out[link] = job(c, jid.group(1) if jid else link, html.unescape(a.group(2)), html.unescape(loc.group(1)) if loc else "",
                        link, age, country="CA")
    return list(out.values())


# Feeds that return a company's whole board, so "no longer listed" means "taken down".
# Others return capped / newest-first slices; they count as complete only below their cap.
COMPLETE = {"greenhouse", "lever", "ashby", "workable", "rippling", "bamboohr", "recruitee", "atlassian", "shopify", "simplify"}
CAPS = {"oracle": 300, "jibe": 500, "eightfold": 300, "smartrecruiters": 1000, "ibm": 100, "google": 60}


def is_complete(ats, n):
    return ats in COMPLETE or (ats in CAPS and n < CAPS[ats])


LISTERS = {"greenhouse": greenhouse_list, "lever": lever_list, "ashby": ashby_list,
           "smartrecruiters": smartrecruiters_list, "amazon": amazon_list, "workday": workday_list,
           "jibe": jibe_list, "eightfold": eightfold_list, "successfactors": successfactors_list,
           "workable": workable_list, "rippling": rippling_list, "bamboohr": bamboohr_list,
           "recruitee": recruitee_list, "simplify": simplify_list, "jobbank": jobbank_list, "oracle": oracle_list,
           "google": google_list, "ibm": ibm_list, "atlassian": atlassian_list, "shopify": shopify_list,
           "successfactors_search": successfactors_search_list}
DETAILERS = {"greenhouse": greenhouse_details, "smartrecruiters": smartrecruiters_details,
             "workday": workday_details, "eightfold": eightfold_details,
             "rippling": rippling_details, "bamboohr": bamboohr_details, "jobbank": jobbank_details, "oracle": oracle_details}
