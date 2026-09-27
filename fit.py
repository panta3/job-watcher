"""How well a job fits Aarav's resume (Resume/Aaravpant_resume.tex), 0-100, plus the reasons.

Pure keyword scoring, no AI service, so it's free and explainable: the UI shows exactly which of
your skills a posting mentions. Edit SKILLS when the resume changes.
"""
import re

# (label shown in the UI, regex, weight). Weight ~ how central it is to the resume.
SKILLS = [
    # languages
    ("Python", r"\bpython\b", 3), ("Java", r"\bjava\b(?!script)", 2), ("C++", r"c\+\+", 2), ("C#", r"c#|\.net\b", 1.5),
    ("C", r"\bC\b(?![+#])", 1), ("JavaScript", r"javascript|\bjs\b|node\.?js", 2.5), ("TypeScript", r"typescript", 2.5),
    ("Kotlin", r"kotlin", 1), ("SQL", r"\bsql\b|postgres|mysql|sqlite", 2.5), ("Haskell", r"haskell", 0.5),
    # web / frameworks
    ("React", r"\breact\b", 2.5), ("React Native", r"react native", 1.5), ("Next.js", r"next\.?js", 1.5),
    ("Django", r"django", 1), ("FastAPI", r"fastapi", 1.5), ("Flask", r"flask", 1.5), ("REST APIs", r"\brest(ful)?\b|\bapis?\b", 1.5),
    ("Tailwind", r"tailwind", 0.5), ("MongoDB", r"mongo", 1), ("Firebase", r"firebase|firestore", 0.5),
    # cloud / devops
    ("AWS", r"\baws\b|amazon web services|lambda|\bec2\b|\bs3\b|dynamodb|cloudwatch|eventbridge", 3),
    ("GCP", r"\bgcp\b|google cloud|cloud run", 1.5), ("Cloud", r"\bcloud\b", 1.5), ("Terraform", r"terraform|infrastructure as code|\biac\b", 2),
    ("Docker", r"docker|container", 2), ("Kubernetes", r"kubernetes|\bk8s\b|\beks\b|\bgke\b", 2),
    ("CI/CD", r"ci/cd|\bci\b|continuous integration|github actions|jenkins|pipelines?", 2), ("Linux", r"linux|unix|bash|shell script", 1.5),
    ("Git", r"\bgit\b|github|version control", 1),
    # security (target specialization)
    ("Security", r"cyber|information security|application security|cloud security|secure coding|security (engineer|analyst|controls|testing)", 2.5), ("IAM", r"\biam\b|identity and access|least privilege", 1.5),
    ("Vulnerability mgmt", r"vulnerab|cve|patch", 1.5), ("Compliance / CIS", r"\bcis\b|compliance|nist|iso ?27001|soc ?2", 1.5),
    ("SOC / SIEM", r"\bsoc\b|siem|splunk|incident response|threat", 1.5), ("Networking", r"networking|tcp/ip|\bdns\b|firewall", 1),
    ("Cryptography", r"cryptograph|encryption|\bpki\b|\btls\b", 1),
    # AI / ML (target specialization)
    ("Machine learning", r"machine learning|\bml\b|deep learning", 3), ("PyTorch", r"pytorch", 2), ("TensorFlow", r"tensorflow|keras", 1.5),
    ("scikit-learn", r"scikit|sklearn", 1), ("LLMs / RAG", r"\bllms?\b|large language|\brag\b|retrieval.augmented|generative ai|genai|langchain|hugging ?face", 2.5),
    ("NLP", r"\bnlp\b|natural language", 1), ("Pandas / NumPy", r"pandas|numpy", 1), ("Data pipelines", r"etl|data pipeline", 1),
    # testing (Evertz experience)
    ("Testing / QA", r"test automation|automated test|unit test|\bqa\b|quality assurance|testing", 2), ("JUnit", r"junit|pytest|selenium|playwright|cypress", 1),
    ("Automation scripts", r"automation|scripting", 1.5),
    # fundamentals / ways of working
    ("Data structures & algorithms", r"data structures|algorithms", 1.5), ("OOP", r"object.oriented|\boop\b", 1),
    ("Concurrency", r"concurren|multithread|distributed systems", 1), ("Agile", r"agile|scrum|jira", 0.5),
    ("Client-facing", r"client.facing|customer.facing|stakeholders?", 0.5),
]
SKILLS = [(label, re.compile(rx, re.I if label != "C" else 0), w) for label, rx, w in SKILLS]
TOTAL = sum(w for _, _, w in SKILLS)

# title families that match what the resume targets (SWE, AI/ML, cloud security)
TITLE_BONUS = [
    (re.compile(r"software|developer|swe\b|sde\b|full.?stack|back.?end|front.?end|programmer|développeu", re.I), 18),
    (re.compile(r"machine learning|\bml\b|\bai\b|data scien|mlops|llm", re.I), 16),
    (re.compile(r"cloud|devops|devsecops|site reliability|\bsre\b|platform|infrastructure", re.I), 14),
    (re.compile(r"security|cyber|soc\b|iam\b", re.I), 14),
    (re.compile(r"\bqa\b|test|sdet|quality", re.I), 10),
    (re.compile(r"data (engineer|analyst)|analytics", re.I), 8),
]
# Hamilton-based: commutable places first, then remote Canada, then the rest of Canada
NEAR = re.compile(r"hamilton|burlington|oakville|mississauga|toronto|brampton|milton|guelph|waterloo|kitchener|cambridge|markham|vaughan|"
                  r"richmond hill|north york|etobicoke|scarborough|\bgta\b|greater toronto", re.I)
REMOTE = re.compile(r"remote|hybrid|anywhere|télétravail", re.I)

FLAGS = [
    ("French required", re.compile(r"bilingual|bilingue|french (is )?(required|mandatory|essential)|fluency in french|français", re.I)),
    ("Clearance", re.compile(r"security clearance|secret clearance|reliability status|habilitation de sécurité", re.I)),
    ("PhD", re.compile(r"\bph\.?d\b", re.I)),
    ("Citizenship", re.compile(r"canadian citizen(ship)? (is )?required|must be a canadian citizen|u\.?s\.? citizen", re.I)),
]


def score(title, description, location, star=False, years=None):
    """Returns (0-100, [matched skill labels, strongest first], [flags])."""
    text = f"{title}\n{description or ''}"
    hits = [(w, label) for label, rx, w in SKILLS if rx.search(text)]
    hits.sort(reverse=True)
    skill_pts = sum(w for w, _ in hits)
    # skills: up to 50 pts. Saturates: ~15 weight points of overlap is already a strong match.
    s = min(50, 50 * skill_pts / 15) if description else min(30, 30 * skill_pts / 6)  # title-only feeds get less credit
    s += max((b for rx, b in TITLE_BONUS if rx.search(title)), default=0)           # up to 18
    s += 16 if star else 0                                                          # explicit new grad / entry
    s += {None: 4, 0: 10, 1: 9, 2: 5}.get(years, 0)                                  # stated experience ask
    loc = location or ""
    s += 6 if NEAR.search(loc) else 4 if REMOTE.search(loc) else 1
    flags = [name for name, rx in FLAGS if rx.search(text)]
    if "Clearance" in flags or "Citizenship" in flags or "PhD" in flags:
        s -= 8
    return max(0, min(100, round(s))), [label for _, label in hits][:8], flags
