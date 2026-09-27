"""Decide whether a job is worth a phone buzz: tech + in Canada + entry level.

Everything is regex on purpose — easy to read, easy to tweak. Tune here.
"""
import re

# ---------------------------------------------------------------- tech titles (English + French)
TECH = re.compile(r"""(
    software|\bsde\b|\bswe\b|logiciel|developer|développeu|developpeu|programm(er|eur|euse|ing|ation)|full[\s-]?stack|front[\s-]?end|back[\s-]?end|
    \bweb\b|\bmobile\b|\bios\b|android|\bapi\b|
    data\s+(engineer|scien|analy|platform|developer)|donn[ée]es|analytics|business\s+intelligence|\bbi\b|
    machine\s+learning|\bml\b|\bai\b|\bia\b|artificial\s+intelligence|deep\s+learning|\bnlp\b|\bllm|computer\s+vision|
    cloud|devops|devsecops|\bsre\b|site\s+reliability|reliability\s+engineer|platform\s+engineer|infrastructure\s+(engineer|developer|analyst|specialist|operations|automation|as\s+code)|(cloud|it|ti|tech|compute|data|network)\s+infrastructure|
    systems?\s+(engineer|developer|analyst|administrator)|network|réseau|
    security|cyber|cybers[ée]curit[ée]|s[ée]curit[ée]\s+(informatique|de\s+l.information|ti\b|des\s+ti\b|applicative|r[ée]seau|infonuagique|des\s+jeux|offensive)|\bsoc\b|threat|penetration|\biam\b|identity|
    infrastructure\s+support|outils\s+et\s+infrastructures|quality\s+assurance\s+testing|\bqa\b|quality\s+assurance\s+(engineer|analyst|developer|automation|tester)|software\s+(quality|test|qa)|assurance\s+qualit[ée]\s+ti|\btest(ing)?\s+(engineer|automation|analyst|developer|lead|specialist)|developer\s+in\s+test|\bsdet\b|automation\s+(engineer|developer|analyst|tester)|test\s+automation|\brpa\b|automatisation|
    firmware|embedded|embarqu|\bfpga\b|\basic\b|\bgpu\b|cuda|compiler|kernel|linux|
    database|\bdba\b|\bsql\b|\bit\b|\bti\b|technology|informatique|information\s+(technology|security|systems)|technical\s+(support|analyst|consultant)|
    support\s+engineer|solutions?\s+(engineer|architect|developer)|sales\s+engineer|integration\s+(engineer|developer|specialist|analyst)|
    salesforce|\berp\b|\bsap\b|servicenow|workday\s+(analyst|developer)|
    product\s+analyst|digital\s+analyst|\bquant\b|quantitative|blockchain|robotics|gameplay|game\s+(programmer|developer)|
    forward[\s-]deployed|product\s+engineer|bioinformati|\bgis\b|geographic(al)?\s+information|g[ée]omati|analytique|guidewire|
    research\s+(engineer|scientist)|computer|tools\s+engineer|build\s+engineer|release\s+engineer|
    \bux\s+engineer|ui\s+developer|site\s+engineer|systems?\s+integrat
)""", re.I | re.X)

# titles that contain a tech word but aren't tech jobs
NOT_TECH = re.compile(r"""(
    sales\s+(representative|associate|manager|executive)|business\s+develop|account\s+(executive|manager)|recruit|talent|
    \bhr\b|payroll|accountant|accounting|tax\b|legal|counsel|paralegal|marketing\s+(manager|specialist|coordinator)|
    mechanical|civil|structural|chemical|electrical\s+(designer|technician)|welder|mainten|technician|driver|
    warehouse|nurse|pharmac|teller|branch|mortgage|financial\s+advisor|investment\s+advisor|insurance\s+advisor|
    representative|call\s+cent|guard\b|physical\s+security|life\s+safety|administrative|clerk\b|insurance|\bseller\b|renewals|sales\s+(program|specialist|supervisor)|building\s+automation|animator|\bclaims\b|training\s+developer|instructional|m[ée]canicien|mechanic|cashier|store|retail|\bevent\b|networking\s+event|information\s+session|
    loss\s+prevention|financial\s+security|security\s+(guard|advisor)|protection\s+rapproch|s[ée]curit[ée]\s+financi|
    training\s+specialist|coach\b|air\s+traffic|sant[ée]\s+et\s+s[ée]curit[ée]|health\s+(and|&)\s+safety|s[ée]curit[ée]\s+du\s+travail|safety\s+(officer|specialist|advisor)|\badvisor\b|conseill[eè]re?\s+(en\s+)?(s[ée]curit[ée]\s+financ|placement|financ)
)""", re.I | re.X)

# ---------------------------------------------------------------- seniority / student roles
TOO_SENIOR = re.compile(r"""(
    \bsenior\b|\bsr\b\.?|s[ée]nior|\bstaff\b|principal|\blead\b|\bleader\b|chef\b|\bhead\b|manager|gestionnaire|
    faculty|professor|tenure[\s-]track|professeur|\bavp\b|\bsvp\b|\bevp\b|director|directeur|directrice|\bvp\b|vice[\s-]president|chief|\bcto\b|architect|architecte|distinguished|fellow|
    expert|premi[eè]re?\b[^,]{0,12}conseill|\bmts\b|member\s+of\s+technical\s+staff|
    \b(iii|iv|v|vi)\b|\b(3|4|5)\b(?!\d)|level\s*[3-9]|\bl[4-9]\b|\bp[3-9]\b|\bsde\s*(ii|iii|2|3)\b
)""", re.I | re.X)

STUDENT = re.compile(r"""(
    intern\b|interns\b|internship|co[\s-]?op|\bcoop|stagiaire|stagaire|\bstage\b|student|étudiant|etudiant|
    summer\s+20|fall\s+20|winter\s+20|spring\s+20|work\s+term|summer\s+(analyst|associate|intern)|\bamplify\b|\(\s*\d{1,2}\s*(months?|mois)\s*\)|\bwinter\b.{0,40}\b20\d\d\b|\b20\d\d\b.{0,40}\bwinter\b|\bpey\b|placement|apprenti
)""", re.I | re.X)

# explicit entry-level wording -> high-priority alert
ENTRY = re.compile(r"""(
    new\s+grad|graduate|entry[\s-]?level|junior|\bjr\b|early[\s-]career|university|campus|\b2027\b|
    associate\b|\b(i|1)\b\s*$|\b(engineer|developer|analyst)\s+(i|1)\b|level\s*1\b|rotational|
    development\s+program|d[ée]butant|jeune\s+diplôm|finissant|nouveau\s+diplôm
)""", re.I | re.X)

# ---------------------------------------------------------------- Canada
PROVINCES = r"ontario|british\s+columbia|qu[ée]bec|alberta|manitoba|saskatchewan|nova\s+scotia|new\s+brunswick|newfoundland|prince\s+edward|yukon|nunavut|northwest\s+territories"
CITIES = r"toronto|vancouver|montr[ée]al|ottawa|waterloo|kitchener|calgary|edmonton|mississauga|markham|hamilton|burlington|oakville|brampton|vaughan|richmond\s+hill|halifax|winnipeg|victoria|burnaby|guelph|london,\s*on|laval|gatineau|saskatoon|regina|fredericton|moncton|st\.?\s+john'?s|kanata|sherbrooke|l[ée]vis|qu[ée]bec\s+city"
CANADA = re.compile(rf"(\bcanada\b|\bcan\b|\bcanadian\b|{PROVINCES}|{CITIES}|,\s*(ON|BC|QC|AB|MB|SK|NS|NB|NL|PE)\b|\b(ON|BC|QC|AB)\s*,\s*canada)", re.I)
# "CA" alone is ambiguous (California vs Canada) so it's only trusted from structured country fields.


US_STATE = re.compile(r",\s*(AL|AK|AZ|AR|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC)\b"
                      r"|,\s*CA\b|\busa\b|united\s+states|\bus\b", re.I)  # "Toronto, ON, CA" is caught by the province check first
UNAMBIGUOUS_CA = re.compile(rf"\bcanada\b|\bcanadian\b|\bcan\b|{PROVINCES}|,\s*(ON|BC|QC|AB|MB|SK|NS|NB|NL|PE)\b|\b(ON|BC|QC|AB)\s*,\s*canada", re.I)


def _part_is_canada(p):
    """One location ("Burlington, MA" / "Toronto, ON, CA" / "Remote - Canada"). City names alone are
    ambiguous (Burlington MA, Waterloo IA, London UK, Cambridge MA), so a US state/USA wins over a city."""
    if UNAMBIGUOUS_CA.search(p):
        return True
    if US_STATE.search(p):
        return False
    return bool(CANADA.search(p))


def is_canada(j):
    if (j.get("country") or "").strip().lower() in ("ca", "can", "canada"):
        return True
    return any(_part_is_canada(p) for p in re.split(r";|\|| / ", j.get("location") or "") if p.strip())


def location_needs_details(j):
    """Workday says '3 Locations' etc; Greenhouse sometimes says just 'Remote'."""
    loc = (j.get("location") or "").strip().lower()
    return (not loc) or bool(re.fullmatch(r"\d+\s+locations?|remote|hybrid|multiple\s+locations|flexible", loc))


# ---------------------------------------------------------------- years of experience
YEARS = re.compile(
    r"(?:minimum\s+(?:of\s+)?|at\s+least\s+)?(\d{1,2})\s*(?:\+|plus)?\s*(?:(?:-|–|to|à)\s*\d{1,2}\s*\+?\s*)?"
    r"(?:years?|yrs?|ans|années)\b(?:\s+of)?[^.;\n]{0,60}?(?:experience|expérience|exp[ée]rience)",
    re.I)


def min_years_required(desc):
    """Smallest 'N years of experience' mentioned, or None. Smallest = most forgiving,
    so 'BS + 2 years or MS + 0 years' counts as 2, not 5."""
    ys = [int(m.group(1)) for m in YEARS.finditer(desc or "") if int(m.group(1)) <= 20]
    return min(ys) if ys else None


def title_verdict(title):
    """Cheap first pass on title only. Returns (keep, reason)."""
    if STUDENT.search(title):
        return False, "student/intern"
    if TOO_SENIOR.search(title) and not re.search(r"new\s+grad|graduate|entry", title, re.I):
        return False, "senior"
    if NOT_TECH.search(title) or not TECH.search(title):
        return False, "not tech"
    return True, ""


def full_verdict(j, max_years):
    """After details are loaded. Returns (keep, reason, priority)."""
    if not is_canada(j):
        return False, "not Canada", 0
    yrs = min_years_required(j.get("description"))
    j["min_years"] = yrs
    if yrs is not None and yrs > max_years:
        return False, f"{yrs}+ yrs", 0
    entry = bool(ENTRY.search(j["title"])) or bool(re.search(
        r"new\s+grad|recent\s+graduate|entry[\s-]level|early\s+career|class\s+of\s+2027|graduating\s+in\s+2027", j.get("description") or "", re.I))
    j["entry"] = entry
    return True, "", (2 if entry else 1)


# ---------------------------------------------------------------- start date (can't start before May 2027)
MONTHS = r"(january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)"
START_2027 = re.compile(r"""(
    \b2027\b.{0,40}(start|graduat|new\s+grad|cohort|class|intake|program)|(start|graduat|new\s+grad|cohort|class|intake|program).{0,40}\b2027\b|
    (may|june|july|august|september|summer|fall|autumn)\s+2027|class\s+of\s+2027|graduating\s+(in|by|between)\b.{0,40}2027|
    new[\s-]grad|early[\s-]career\s+program|campus\s+(hire|recruit)|nouveaux?\s+diplôm|finissant
)""", re.I | re.X | re.S)
START_NOW = re.compile(rf"""(
    immediate(ly)?\s+(start|available|availability)|start(ing)?\s+immediately|available\s+immediately|\basap\b|as\s+soon\s+as\s+possible|
    start\s+date\s*:?\s*{MONTHS}\s+(\d{{1,2}},?\s+)?2026|start\s+date\s*:?\s*(january|february|march|april|jan|feb|mar|apr)\s+(\d{{1,2}},?\s+)?2027|
    (starting|beginning|commencing)\s+(in\s+)?(october|november|december|january|february|march)\b|
    (fall|winter)\s+2026\s+start|(january|winter)\s+2027\s+(start|cohort|intake)|
    \b\d{{1,2}}[\s-]*(month|mois)\b[^.]{{0,20}}(contract|term|contrat)|(contract|term|contrat)\s*[(\-–]\s*\d{{1,2}}\s*(month|mois)|fixed[\s-]term|temporary\s+(full|part)|
    must\s+have\s+(already\s+)?(completed|graduated)|(have|has)\s+completed\s+(a|your|their)\s+(bachelor|degree)|degree\s+completed\s+by\s+(2025|2026)|
    graduated\s+(in|between)\b.{{0,30}}(2023|2024|2025)|entrée\s+en\s+fonction\s+immédiate|disponibilité\s+immédiate
)""", re.I | re.X | re.S)


def start_verdict(title, description):
    """'2027' = clearly a 2027 start (new grad program etc.), 'now' = wants someone before May 2027,
    '' = the posting doesn't say. Title wins over description; 'now' signals beat vague new-grad wording."""
    t, d = title or "", description or ""
    if re.search(r"(winter|january|jan|february|feb|march|mar|hiver|janvier)\s*2027", t, re.I) or \
            re.search(r"(january|february|march|winter)\s*2027", d[:3000], re.I) and not re.search(r"(may|june|july|august|september|summer|fall)\s+2027", d, re.I):
        return "now"
    if re.search(r"\b2027\b", t):
        return "2027"
    if START_NOW.search(t) or START_NOW.search(d):
        return "now"
    if START_2027.search(t) or START_2027.search(d):
        return "2027"
    return ""
