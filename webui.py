"""Web UI: every open match with fit score, Apply button, referral/salary links, and a pipeline tracker.

Served two ways with the same code:
  - locally:  python3 watcher.py serve          -> http://localhost:8765
  - on AWS:   short Vercel address -> Lambda function URL; lambda_function.py asks for the password

Storage is pluggable so the same handler works with local files or S3:
  store.load_jobs() -> list[dict]      (open matches, built from jobs.db)
  store.load_status() -> dict          (status.json: {job_key: {...}}, see update_status)
  store.save_status(dict)
  store.updated() -> str               (when the data was last refreshed)
"""
import json
import os
from datetime import datetime

STAGES = ["applied", "oa", "interview", "offer", "rejected", "ghosted"]


def update_status(status, req):
    """Merge one change from the page into status.json.

    Entry: {"state": "applied"|"hidden", "at", "stage", "stage_at", "notes", "title", "company", "url", "location"}.
    Title/company/url are copied in so the pipeline still shows a job after it closes.
    """
    k = req.get("k")
    if not k:
        return
    if "state" in req and not req["state"]:
        status.pop(k, None)
        return
    now = datetime.now().isoformat(timespec="seconds")
    e = status.setdefault(k, {})
    for f in ("title", "company", "url", "location"):
        if req.get(f):
            e[f] = req[f]
    if req.get("state") in ("applied", "hidden"):
        e["state"], e["at"] = req["state"], now
        if req["state"] == "applied":
            e.setdefault("stage", "applied")
            e.setdefault("stage_at", now)
    if req.get("stage") in STAGES and req["stage"] != e.get("stage"):
        e["stage"], e["stage_at"] = req["stage"], now
    if "notes" in req:
        e["notes"] = str(req["notes"])[:2000]


def handle(method, path, query, body, store, token=None):
    """Returns (status_code, content_type, body_str)."""
    if token and query.get("k") != token:
        return 403, "text/plain", "forbidden: add ?k=<your token> to the URL"
    if method == "GET" and path in ("/", ""):
        return 200, "text/html; charset=utf-8", render(store.load_jobs(), store.load_status(), query.get("k", ""), store.updated())
    if method == "GET" and path == "/api/jobs":
        return 200, "application/json", json.dumps({"jobs": store.load_jobs(), "status": store.load_status()})
    if method == "POST" and path == "/api/status":
        status = store.load_status()
        update_status(status, json.loads(body or "{}"))
        store.save_status(status)
        return 200, "application/json", json.dumps({"ok": True})
    return 404, "text/plain", "not found"


class FileStore:
    def __init__(self, jobs_path, status_path):
        self.jobs_path, self.status_path = jobs_path, status_path

    def load_jobs(self):
        try:
            return json.load(open(self.jobs_path))
        except FileNotFoundError:
            return []

    def load_status(self):
        try:
            return json.load(open(self.status_path))
        except FileNotFoundError:
            return {}

    def save_status(self, status):
        json.dump(status, open(self.status_path, "w"), indent=1)

    def updated(self):
        try:
            return datetime.fromtimestamp(os.path.getmtime(self.jobs_path)).strftime("%b %d, %H:%M")
        except OSError:
            return ""


def render(jobs, status, token, updated=""):
    data = json.dumps({"jobs": jobs, "status": status, "token": token, "updated": updated}).replace("</", "<\\/")
    return PAGE.replace("/*DATA*/null", data)


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22%3E%3Ctext y=%22.9em%22 font-size=%2290%22%3E%F0%9F%92%BC%3C/text%3E%3C/svg%3E">
<title>Job Watcher</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--ink:#16181d;--muted:#5d6472;--line:#e3e6eb;--accent:#1d5fd1;--accent-ink:#fff;
      --star:#9a6412;--star-bg:#fdf3e1;--ok:#1f7a45;--ok-bg:#e5f4ea;--warn:#a1361b;--warn-bg:#fbe9e4;--new:#c2410c;--chip:#eef1f5;
      --fit-hi:#1f7a45;--fit-mid:#9a6412;--fit-lo:#6b7280}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#0f1115;--card:#171a21;--ink:#e8eaee;--muted:#9aa2b1;
      --line:#262b35;--accent:#5b93f0;--accent-ink:#0b1020;--star:#f0b54a;--star-bg:#2a2212;--ok:#5fcf8b;--ok-bg:#132a1d;
      --warn:#f39a80;--warn-bg:#2d1812;--new:#fb8f5a;--chip:#1f2430;--fit-hi:#5fcf8b;--fit-mid:#f0b54a;--fit-lo:#8b93a3}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
header{position:sticky;top:0;z-index:5;background:var(--bg);border-bottom:1px solid var(--line);padding:12px 16px}
.wrap{max-width:960px;margin:0 auto}
h1{font-size:18px;margin:0 0 2px}
.sub{color:var(--muted);font-size:13px}
.controls{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px}
input[type=search]{flex:1 1 220px;min-width:0;padding:9px 11px;border:1px solid var(--line);border-radius:9px;background:var(--card);color:var(--ink);font:inherit}
select,.toggle{padding:8px 10px;border:1px solid var(--line);border-radius:9px;background:var(--card);color:var(--ink);font:inherit;font-size:14px}
.toggle{cursor:pointer;user-select:none}
.toggle.on{background:var(--accent);color:var(--accent-ink);border-color:var(--accent)}
main{padding:12px 16px 60px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px;margin-bottom:10px;display:flex;gap:12px;align-items:flex-start}
.fit{flex:0 0 46px;height:46px;border-radius:10px;display:flex;flex-direction:column;align-items:center;justify-content:center;border:2px solid currentColor;font-weight:700;font-size:17px;line-height:1}
.fit small{font-size:9.5px;font-weight:600;letter-spacing:.04em;margin-top:2px;opacity:.85}
.fit.hi{color:var(--fit-hi)}.fit.mid{color:var(--fit-mid)}.fit.lo{color:var(--fit-lo)}
.info{flex:1;min-width:0}
.title{font-weight:600;font-size:15.5px;overflow-wrap:anywhere}
.title a{color:inherit;text-decoration:none}
.title a:hover{text-decoration:underline}
.meta{color:var(--muted);font-size:13.5px;margin-top:2px;overflow-wrap:anywhere}
.badges{display:flex;flex-wrap:wrap;gap:6px;margin-top:7px}
.b{font-size:12px;padding:2px 8px;border-radius:99px;background:var(--chip);color:var(--muted)}
.b.star{background:var(--star-bg);color:var(--star);font-weight:600}
.b.ok{background:var(--ok-bg);color:var(--ok);font-weight:600}
.b.warn{background:var(--warn-bg);color:var(--warn);font-weight:600}
.b.skill{background:transparent;border:1px solid var(--line);color:var(--ink)}
.b.new{color:var(--new);font-weight:700;background:transparent;border:1px solid currentColor}
.links{margin-top:8px;font-size:13px;display:flex;flex-wrap:wrap;gap:4px 14px}
.links a{color:var(--accent);text-decoration:none}
.links a:hover{text-decoration:underline}
.actions{display:flex;flex-direction:column;gap:6px;flex:0 0 auto}
.btn{display:inline-block;text-align:center;padding:8px 14px;border-radius:9px;font:inherit;font-size:14px;font-weight:600;cursor:pointer;border:1px solid var(--line);background:var(--card);color:var(--ink);text-decoration:none;white-space:nowrap}
.btn.primary{background:var(--accent);color:var(--accent-ink);border-color:var(--accent)}
.btn.small{padding:5px 10px;font-size:13px;font-weight:500}
.pipe{margin-top:9px;display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.pipe select{padding:5px 8px;font-size:13px}
.pipe textarea{flex:1 1 100%;min-height:38px;padding:7px 9px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--ink);font:inherit;font-size:13px;resize:vertical}
.empty{color:var(--muted);text-align:center;padding:40px 0}
.count{color:var(--muted);font-size:13px;margin:0 0 10px}
@media (max-width:560px){.card{flex-wrap:wrap}.fit{flex-basis:40px;height:40px;font-size:15px}.info{flex-basis:calc(100% - 56px)}.actions{flex-direction:row;width:100%}.actions .btn{flex:1}}
</style>
</head>
<body>
<header><div class="wrap">
  <h1>Job Watcher</h1>
  <div class="sub" id="sub"></div>
  <div class="controls">
    <input type="search" id="q" placeholder="Search title, company, city, skill…">
    <span class="toggle" id="starOnly">⭐ Entry level only</span>
    <select id="sort"><option value="fit" selected>Best fit</option><option value="new">Newest</option></select>
    <select id="start"><option value="ok" selected>Can start May 2027+</option><option value="2027">Only confirmed 2027 starts</option><option value="all">Include "starts now"</option></select>
    <select id="days"><option value="3">Posted ≤ 3 days</option><option value="7">≤ 7 days</option><option value="14">≤ 14 days</option><option value="30">≤ 30 days</option><option value="999" selected>Any time</option></select>
    <select id="view"><option value="todo" selected>To apply</option><option value="pipeline">My applications</option><option value="hidden">Hidden</option><option value="all">Everything</option></select>
  </div>
</div></header>
<main><div class="wrap"><p class="count" id="count"></p><div id="list"></div></div></main>
<script>
const DATA = /*DATA*/null;
const status = DATA.status;
const STAGES = {applied:"Applied", oa:"Online assessment", interview:"Interviewing", offer:"Offer 🎉", rejected:"Rejected", ghosted:"No response"};
const $ = s => document.querySelector(s);
const ls = {get(k,d){try{return JSON.parse(localStorage.getItem(k))??d}catch(e){return d}}, set(k,v){try{localStorage.setItem(k,JSON.stringify(v))}catch(e){}}};
const prefs = Object.assign({starOnly:false, days:"999", view:"todo", sort:"fit", start:"ok"}, ls.get("jw-prefs", {}));
const lastVisit = ls.get("jw-last-visit", null);
ls.set("jw-last-visit", new Date().toISOString().slice(0,19));
$("#starOnly").classList.toggle("on", prefs.starOnly); $("#days").value=prefs.days; $("#view").value=prefs.view; $("#sort").value=prefs.sort; $("#start").value=prefs.start;

// jobs = open matches + anything you applied to that has since closed (so the pipeline keeps it)
const byKey = Object.fromEntries(DATA.jobs.map(j=>[j.k,j]));
for (const [k,s] of Object.entries(status)) if(!byKey[k] && s.state==="applied")
  byKey[k] = {k, title:s.title||"(closed posting)", company:s.company||"", location:s.location||"", url:s.url||"#", closed:true, fit:null, skills:[], flags:[], source:"closed"};
const jobs = Object.values(byKey);

function ago(d){ if(!d) return ""; const n=Math.round((Date.now()-new Date(d.slice(0,10)+"T12:00:00"))/864e5);
  return n<=0?"today":n==1?"yesterday":n+" days ago"; }
function esc(s){return String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]))}
function co(j){ return (j.company||"").replace(/\s*\((Job Bank|new grad|early careers)\)/i,"").replace(/,?\s+(Inc|Corp|Corporation|Ltd|LLC)\.?$/i,"").trim(); }
function links(j){
  return `<div class="links">
    <a href="https://www.linkedin.com/search/results/people/?keywords=${encodeURIComponent('McMaster '+co(j))}" target="_blank" rel="noopener">👥 McMaster alumni there</a>
    <a href="https://www.linkedin.com/search/results/people/?keywords=${encodeURIComponent(co(j)+' recruiter Canada')}" target="_blank" rel="noopener">Recruiters</a>
    <a href="https://www.google.com/search?q=${encodeURIComponent('levels.fyi '+co(j)+' software engineer salary Canada')}" target="_blank" rel="noopener">💰 Salary</a>
    <a href="https://www.google.com/search?q=${encodeURIComponent(co(j)+' glassdoor reviews Canada')}" target="_blank" rel="noopener">Reviews</a>
  </div>`; }
function fitBox(j){ if(j.fit==null) return ""; const c=j.fit>=70?"hi":j.fit>=45?"mid":"lo";
  return `<div class="fit ${c}" title="How well this matches your resume">${j.fit}<small>FIT</small></div>`; }

let timer; async function save(k, patch){
  const j = byKey[k]||{};
  if(patch.state===null){ delete status[k]; }
  else { const e = status[k] = status[k]||{}; Object.assign(e, patch, {title:j.title, company:j.company, url:j.url, location:j.location});
         if(patch.state==="applied"){ e.at=e.at||new Date().toISOString().slice(0,19); e.stage=e.stage||"applied"; e.stage_at=e.stage_at||e.at; }
         if(patch.stage){ e.stage_at=new Date().toISOString().slice(0,19); } }
  render();
  try{ await fetch("api/status"+(DATA.token?"?k="+encodeURIComponent(DATA.token):""), {method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify(Object.assign({k, title:j.title, company:j.company, url:j.url, location:j.location}, patch))}); }
  catch(e){ alert("Couldn't save — check your connection."); }
}

function render(){
  prefs.starOnly=$("#starOnly").classList.contains("on"); prefs.days=$("#days").value; prefs.view=$("#view").value; prefs.sort=$("#sort").value; prefs.start=$("#start").value; ls.set("jw-prefs",prefs);
  const q=$("#q").value.trim().toLowerCase(), maxDays=+prefs.days, pipe=prefs.view==="pipeline";
  let rows = jobs.filter(j=>{
    const st=status[j.k]?.state;
    if(prefs.view==="todo" && (st || j.closed)) return false;
    if(pipe && st!=="applied") return false;
    if(prefs.view==="hidden" && st!=="hidden") return false;
    if(!pipe && prefs.starOnly && !j.star) return false;
    if(!pipe && prefs.start==="ok" && j.start==="now") return false;
    if(!pipe && prefs.start==="2027" && j.start!=="2027") return false;
    const d = j.posted || (j.found_at_setup ? null : (j.first_seen||"").slice(0,10));
    if(!pipe && maxDays<999 && (!d || (Date.now()-new Date(d+"T12:00:00"))/864e5 > maxDays)) return false;
    return !q || (j.title+" "+j.company+" "+j.location+" "+(j.skills||[]).join(" ")).toLowerCase().includes(q);
  });
  const date = j => j.posted || (j.found_at_setup ? "" : (j.first_seen||"").slice(0,10));
  if(pipe) rows.sort((a,b)=>(status[b.k].stage_at||"").localeCompare(status[a.k].stage_at||""));
  else if(prefs.sort==="fit") rows.sort((a,b)=>(b.fit??-1)-(a.fit??-1) || date(b).localeCompare(date(a)));
  else rows.sort((a,b)=>date(b).localeCompare(date(a)) || (b.fit??-1)-(a.fit??-1));

  const apps=Object.values(status).filter(s=>s.state==="applied");
  const n=s=>apps.filter(a=>a.stage===s).length;
  const n27 = DATA.jobs.filter(j=>j.start==="2027").length;
  $("#sub").textContent = `${DATA.jobs.length} open matches · ${n27} confirmed 2027 starts · ${apps.length} applied · ${n("oa")+n("interview")} in progress · ${n("offer")} offers · updated ${DATA.updated||""}`;
  $("#count").textContent = rows.length + (rows.length==1?" job":" jobs") + (pipe?" in your pipeline":"");
  $("#list").innerHTML = rows.length ? rows.map(j=>{
    const s=status[j.k]||{}, st=s.state, isNew=lastVisit && j.first_seen > lastVisit && !st;
    const idle = s.stage_at ? Math.round((Date.now()-new Date(s.stage_at))/864e5) : 0;
    return `<div class="card">
      ${fitBox(j)}
      <div class="info">
        <div class="title"><a href="${esc(j.url)}" target="_blank" rel="noopener">${esc(j.title)}</a></div>
        <div class="meta">${esc(j.company)} · ${esc(j.location)}</div>
        <div class="badges">
          ${isNew?'<span class="b new">NEW</span>':""}
          ${j.start==="2027"?'<span class="b ok">✓ 2027 start</span>':j.start==="now"?'<span class="b warn">wants a start before May 2027</span>':j.closed?"":'<span class="b">start date not stated</span>'}
          ${j.star?'<span class="b star">⭐ entry level</span>':""}
          ${j.closed?'<span class="b warn">posting closed</span>':""}
          ${(j.flags||[]).map(f=>`<span class="b warn">${esc(f)}</span>`).join("")}
          ${j.posted?`<span class="b">posted ${ago(j.posted)}</span>`:j.first_seen?`<span class="b" title="this site doesn't publish posting dates">found ${ago(j.first_seen)}</span>`:""}
          ${j.years!=null?`<span class="b">asks ${j.years}+ yrs</span>`:""}
          ${(j.skills||[]).slice(0,5).map(x=>`<span class="b skill">${esc(x)}</span>`).join("")}
        </div>
        ${links(j)}
        ${st==="applied"?`<div class="pipe">
          <select data-stage="${esc(j.k)}">${Object.entries(STAGES).map(([v,l])=>`<option value="${v}" ${s.stage===v?"selected":""}>${l}</option>`).join("")}</select>
          <span class="b ok">applied ${esc((s.at||"").slice(0,10))}</span>
          ${["applied","oa","interview"].includes(s.stage)&&idle>=14?`<span class="b warn">${idle} days, no update: follow up?</span>`:""}
          <textarea data-notes="${esc(j.k)}" placeholder="Notes: referral, recruiter, interview dates…">${esc(s.notes||"")}</textarea></div>`:""}
      </div>
      <div class="actions">
        <a class="btn primary" href="${esc(j.url)}" target="_blank" rel="noopener" data-apply="${esc(j.k)}">Apply ↗</a>
        ${st==="applied"?`<button class="btn small" data-set="${esc(j.k)}" data-state="">Undo</button>`
          :`<button class="btn small" data-set="${esc(j.k)}" data-state="applied">✓ Applied</button>`}
        ${st==="hidden"?`<button class="btn small" data-set="${esc(j.k)}" data-state="">Unhide</button>`
          :st?"":`<button class="btn small" data-set="${esc(j.k)}" data-state="hidden">Hide</button>`}
      </div></div>`}).join("") : `<div class="empty">${pipe?"Nothing here yet. Mark jobs ✓ Applied to track them.":"Nothing here. Try \"Any time\" or clear the search."}</div>`;
}
document.addEventListener("click", e=>{
  const s=e.target.closest("[data-set]"); if(s){ save(s.dataset.set, {state:s.dataset.state||null}); return; }
  const a=e.target.closest("[data-apply]");
  if(a) setTimeout(()=>{ if(!status[a.dataset.apply] && confirm("Did you apply? Mark it as applied?")) save(a.dataset.apply,{state:"applied"}); }, 1500);
});
document.addEventListener("change", e=>{ const s=e.target.closest("[data-stage]"); if(s) save(s.dataset.stage,{stage:s.value}); });
document.addEventListener("input", e=>{ const t=e.target.closest("[data-notes]"); if(!t) return;
  clearTimeout(timer); timer=setTimeout(()=>{ (status[t.dataset.notes]||{}).notes=t.value;
    fetch("api/status"+(DATA.token?"?k="+encodeURIComponent(DATA.token):""),{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({k:t.dataset.notes,notes:t.value})}); }, 700); });
$("#starOnly").onclick=()=>{ $("#starOnly").classList.toggle("on"); render(); };
["#q","#days","#view","#sort","#start"].forEach(s=>$(s).addEventListener("input", render));
render();
</script>
</body>
</html>
"""
