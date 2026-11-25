"""After the call: research shareable resources for the user's goal with Gemini +
Google Search, and keep only links that are real.

Spoken links are useless on a phone call, so resources go in the summary email.
The model's own URLs are never trusted on their own:
- a link is kept if it came from the search results (grounding sources or
  grounding redirect links, resolved to the real page);
- a YouTube link is also kept if YouTube confirms the video exists;
- a person is kept only if the search results support their name. If their exact
  profile URL isn't confirmed, they get a LinkedIn search link for name + company,
  which always resolves to the right person.
Tracking parameters (utm_* etc.) are stripped. All blocking.
"""
import json
import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, quote_plus, urlencode, urlsplit, urlunsplit

import requests

from agent import config

API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
REDIRECT_HOST = "vertexaisearch.cloud.google.com"

CLIENT = """You are a senior career coach preparing follow-up resources for a client after a coaching call.
Use Google Search. Only give URLs that appeared in your search results; never construct or guess a URL.

CLIENT
Current situation: {situation}
Goal agreed on the call: {goal}
Target role: {target_role}
Skill gaps: {gaps}
Obstacles: {obstacles}
Agreed next steps: {next_steps}
Resume excerpt: {resume}
"""

TASKS = {
    "watch": """FIND 2 to 4 YouTube videos (talks, tutorials or interviews by credible practitioners) and 0 to 2 practical articles
that directly help this client reach the goal or close a skill gap. Prefer recent ones.
Return ONLY JSON: {{"videos":[{{"title":"","url":"","why":""}}],"reading":[{{"title":"","url":"","why":""}}]}}
"why" is one sentence tying the item to THIS client's goal or gap.""",
    "learning": """FIND courses, certifications, executive programmes or degrees ONLY if a skill gap above clearly needs one (0 to 3 items).
Prefer universities, IIM/IIT/ISB executive programmes, Coursera, edX, or official vendor certifications, available to someone in India.
If nothing is clearly needed, return an empty list. Never suggest a course just to have one.
Return ONLY JSON: {{"learning":[{{"title":"","provider":"","url":"","why":""}}]}}
"why" names the exact gap it closes.""",
    "people": """FIND 3 to 5 real senior people who already hold the client's target role (or one level above), ideally in India and in the
client's industry or at companies the client would target. Search LinkedIn and company or news pages.
Return ONLY JSON: {{"people":[{{"name":"","headline":"","company":"","url":"","why":"","connect_note":""}}]}}
"url" is their public LinkedIn profile (linkedin.com/in/...) if you found it, else "".
"why" says what the client can learn from this person. "connect_note" is a LinkedIn connection note under 280
characters the client could send, specific to that person and the client's goal; it asks one thoughtful question, never for a job.""",
}

_TRACKING = re.compile(r"^(utm_|fbclid$|gclid$|mc_|ref$|ref_|trk|si$|feature$)")


def _clean(url: str) -> str:
    """Drop tracking parameters, keep functional ones (e.g. YouTube's v)."""
    parts = urlsplit(url.strip())
    query = {k: v for k, v in parse_qs(parts.query).items() if not _TRACKING.match(k)}
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query, doseq=True), ""))


def _norm(url: str) -> str:
    """Comparable form: https, no www/in./m., no query or fragment, no trailing slash."""
    parts = urlsplit(url.strip())
    host = parts.netloc.lower()
    for prefix in ("www.", "in.", "m."):
        host = host.removeprefix(prefix)
    return urlunsplit(("https", host, parts.path.rstrip("/"), "", "")).lower()


def _resolve(uri: str) -> str:
    """Real URL behind a grounding redirect link ("" if it can't be resolved)."""
    try:
        r = requests.head(uri, allow_redirects=False, timeout=8)
        return r.headers.get("Location", "")
    except requests.RequestException:
        return ""


def _youtube_exists(url: str) -> bool:
    m = re.search(r"(?:youtube\.com/watch\?(?:.*&)?v=|youtu\.be/|youtube\.com/shorts/)([\w-]{11})", url)
    if not m:
        return False
    r = requests.get("https://www.youtube.com/oembed", timeout=8,
                     params={"url": f"https://www.youtube.com/watch?v={m.group(1)}", "format": "json"})
    return r.status_code == 200


def _parse_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    try:
        return json.loads(m.group(0)) if m else {}
    except json.JSONDecodeError:
        return {}


def _search(task: str, context: str) -> tuple:
    """One grounded request → (parsed JSON, resolved source URLs, supported text)."""
    r = requests.post(API.format(model=config.RESEARCH_MODEL), timeout=150,
                      params={"key": config.GEMINI_API_KEY},
                      json={"contents": [{"role": "user", "parts": [{"text": context + "\n" + TASKS[task]}]}],
                            "tools": [{"google_search": {}}]})
    r.raise_for_status()
    candidate = r.json()["candidates"][0]
    text = "".join(p.get("text", "") for p in candidate.get("content", {}).get("parts", []))
    gm = candidate.get("groundingMetadata", {})
    sources = {_norm(u) for u in (_resolve(c.get("web", {}).get("uri", ""))
                                  for c in gm.get("groundingChunks", [])) if u}
    supported = " ".join(s.get("segment", {}).get("text", "") for s in gm.get("groundingSupports", []))
    supported += " " + " ".join(c.get("web", {}).get("title", "") for c in gm.get("groundingChunks", []))
    return _parse_json(text), sources, supported.lower()


# Plain-language question on purpose: asked for JSON only, the model tends to
# answer from memory without searching, which leaves nothing to verify against.
VERIFY_PERSON = """Search the web to find out: is {name} currently (or very recently) {headline} at {company}?
Answer in two or three plain sentences saying what the search results show, and where.
Then end with exactly three lines:
VERDICT: yes or no
TITLE: their current title according to the search results
LINKEDIN: their linkedin.com/in/ profile URL if it appeared in the search results, else none"""


def _verify_person(p: dict):
    """Confirm a suggested person with their own grounded search. The bulk people
    search sometimes answers from memory without search sources, so every person
    needs this check. Returns the person with a usable URL, or None."""
    name = str(p.get("name", "")).strip()
    if not name:
        return None
    r = requests.post(API.format(model=config.RESEARCH_MODEL), timeout=90,
                      params={"key": config.GEMINI_API_KEY},
                      json={"contents": [{"role": "user", "parts": [{"text": VERIFY_PERSON.format(
                          name=name, headline=p.get("headline", ""), company=p.get("company", ""))}]}],
                            "tools": [{"google_search": {}}]})
    r.raise_for_status()
    candidate = r.json()["candidates"][0]
    chunks = candidate.get("groundingMetadata", {}).get("groundingChunks", [])
    text = "".join(x.get("text", "") for x in candidate.get("content", {}).get("parts", []))
    field = lambda key: (re.findall(rf"^\W*{key}:\s*(.+?)\s*$", text, re.M | re.I) or [""])[-1]
    if not chunks or not field("VERDICT").lower().startswith("yes"):
        return None  # no search evidence: don't risk sending a made-up person
    sources = {_norm(u) for u in (_resolve(c.get("web", {}).get("uri", "")) for c in chunks) if u}
    linkedin = re.search(r"https?://\S*linkedin\.com/in/[\w%-]+", field("LINKEDIN"))
    url = ""
    for candidate_url in (linkedin.group(0) if linkedin else "", p.get("url")):
        url = _verified_url(candidate_url, sources)
        if "linkedin.com/in/" in url:
            break
        url = ""
    if not url:
        url = _people_search_link(name, p.get("company", ""))
    headline = field("TITLE").strip("*` ") or p.get("headline", "")
    return {**p, "headline": headline, "url": url}


def _verified_url(url: str, sources: set, youtube: bool = False) -> str:
    """The real, cleaned URL if it is backed by the search, else ""."""
    url = str(url or "").strip()
    if not url.startswith("http"):
        return ""
    if urlsplit(url).netloc == REDIRECT_HOST:
        url = _resolve(url)  # a grounding redirect: came from the search by definition
        return _clean(url) if url else ""
    if _norm(url) in sources or (youtube and _youtube_exists(url)):
        return _clean(url)
    return ""


def _safe(fn, *args):
    try:
        return fn(*args)
    except Exception as e:
        print(f"[research] {fn.__name__} failed: {e!r}", flush=True)
        return None


def _people_search_link(*terms: str) -> str:
    return "https://www.linkedin.com/search/results/people/?keywords=" + quote_plus(" ".join(t for t in terms if t)[:120])


def find_resources(summary: dict, resume: str) -> dict:
    """Return {"videos","learning","people","reading","people_search"} with verified links only."""
    target = summary.get("target_role") or summary.get("goal", "")
    out = {"videos": [], "learning": [], "people": [], "reading": [],
           "people_search": _people_search_link(target)}
    if not (config.GEMINI_API_KEY and config.RESEARCH_ENABLED and target):
        return out
    context = CLIENT.format(
        situation=summary.get("current_situation", ""), goal=summary.get("goal", ""),
        target_role=target, gaps=summary.get("skill_gaps", "") or "not stated",
        obstacles=summary.get("obstacles", ""), next_steps=summary.get("next_steps", ""),
        resume=re.sub(r"\s+", " ", resume)[:3000])

    with ThreadPoolExecutor(max_workers=3) as pool:
        jobs = {task: pool.submit(_search, task, context) for task in TASKS}
    results = {}
    for task, job in jobs.items():
        try:
            results[task] = job.result()
        except Exception as e:  # one failed search shouldn't lose the others
            print(f"[research] {task} failed: {e!r}", flush=True)

    if "watch" in results:
        found, sources, _ = results["watch"]
        for v in found.get("videos", []) or []:
            if (url := _verified_url(v.get("url"), sources, youtube=True)):
                out["videos"].append({**v, "url": url})
        for a in found.get("reading", []) or []:
            if (url := _verified_url(a.get("url"), sources)):
                out["reading"].append({**a, "url": url})
    if "learning" in results:
        found, sources, _ = results["learning"]
        for c in found.get("learning", []) or []:
            if (url := _verified_url(c.get("url"), sources)):
                out["learning"].append({**c, "url": url})
    if "people" in results:
        candidates = (results["people"][0].get("people", []) or [])[:5]
        with ThreadPoolExecutor(max_workers=5) as pool:
            checks = list(pool.map(lambda p: _safe(_verify_person, p), candidates))
        out["people"] = [p for p in checks if p]
    print("[research] kept " + ", ".join(f"{k}={len(v)}" for k, v in out.items() if isinstance(v, list)), flush=True)
    return out


def email_section(res: dict) -> str:
    """Plain-text block for the summary email."""
    lines = ["RESOURCES PICKED FOR YOU",
             "These links are only to guide you. They are not promotions, sponsored or affiliate links,",
             "and Disha has no connection with any provider or person listed.", ""]
    if res.get("videos"):
        lines.append("Watch")
        for v in res["videos"]:
            lines += [f"- {v.get('title', '')}", f"  {v['url']}", f"  Why: {v.get('why', '')}"]
        lines.append("")
    if res.get("learning"):
        lines.append("Learn (only where it fills a real gap)")
        for c in res["learning"]:
            provider = f" ({c['provider']})" if c.get("provider") else ""
            lines += [f"- {c.get('title', '')}{provider}", f"  {c['url']}", f"  Why: {c.get('why', '')}"]
        lines.append("")
    if res.get("reading"):
        lines.append("Read")
        for a in res["reading"]:
            lines += [f"- {a.get('title', '')}", f"  {a['url']}", f"  Why: {a.get('why', '')}"]
        lines.append("")
    lines += ["People to connect with on LinkedIn",
              "They are already where you want to be. Send a short, specific note and ask one",
              "thoughtful question; never ask for a job in the first message."]
    for p in res.get("people", []):
        lines += [f"- {p.get('name', '')}: {p.get('headline', '')}", f"  {p['url']}",
                  f"  Why: {p.get('why', '')}"]
        if p.get("connect_note"):
            lines.append(f"  Suggested note: \"{p['connect_note']}\"")
    lines += [f"- Find more people in this role: {res['people_search']}", ""]
    return "\n".join(lines)
