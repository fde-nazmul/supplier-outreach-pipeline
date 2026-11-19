#!/usr/bin/env python3
"""
geo-audit-agent — audit a web page for classic SEO *and* GEO (Generative Engine
Optimization) readiness, then optionally ask Claude for a prioritized fix plan.

Zero dependencies: Python 3.9+ standard library only.

Usage:
    python geo_audit.py https://example.com
    python geo_audit.py https://example.com --json report.json
    ANTHROPIC_API_KEY=sk-... python geo_audit.py https://example.com --ai
    python geo_audit.py --file page.html            # audit a saved page offline
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from html.parser import HTMLParser

USER_AGENT = "geo-audit-agent/1.0 (+https://github.com/fde-nazmul/geo-audit-agent)"
AI_CRAWLERS = ["GPTBot", "OAI-SearchBot", "ChatGPT-User", "ClaudeBot", "Claude-SearchBot",
               "PerplexityBot", "Google-Extended", "Bingbot"]


# --------------------------------------------------------------------------- parsing
class PageParser(HTMLParser):
    """Collects the on-page signals we score."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.meta: dict[str, str] = {}
        self.canonical = ""
        self.lang = ""
        self.headings: dict[str, list[str]] = {f"h{i}": [] for i in range(1, 7)}
        self.images = 0
        self.images_missing_alt = 0
        self.links_internal = 0
        self.links_external = 0
        self.jsonld: list[str] = []
        self.text_chunks: list[str] = []
        self._stack: list[str] = []
        self._buf = ""
        self._base_host = ""

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "html":
            self.lang = a.get("lang", "")
        elif tag == "meta":
            key = (a.get("name") or a.get("property") or "").lower()
            if key:
                self.meta[key] = a.get("content", "")
        elif tag == "link" and "canonical" in a.get("rel", "").lower():
            self.canonical = a.get("href", "")
        elif tag == "img":
            self.images += 1
            if not a.get("alt", "").strip():
                self.images_missing_alt += 1
        elif tag == "a" and a.get("href"):
            host = urllib.parse.urlparse(a["href"]).netloc
            if host and self._base_host and host != self._base_host:
                self.links_external += 1
            else:
                self.links_internal += 1
        if tag in ("title", "script", "style") or tag in self.headings:
            self._stack.append(tag if tag != "script" else f"script:{a.get('type', '')}")
            self._buf = ""

    def handle_endtag(self, tag):
        if not self._stack:
            return
        top = self._stack[-1]
        if top == tag or top.startswith(tag + ":"):
            self._stack.pop()
            text = " ".join(self._buf.split())
            if tag == "title":
                self.title = text
            elif tag in self.headings:
                self.headings[tag].append(text)
            elif top == "script:application/ld+json":
                self.jsonld.append(self._buf.strip())
            self._buf = ""

    def handle_data(self, data):
        if self._stack:
            self._buf += data
        if not self._stack or self._stack[-1] in self.headings:
            if data.strip():
                self.text_chunks.append(data.strip())


# --------------------------------------------------------------------------- checks
@dataclass
class Check:
    id: str
    area: str          # "SEO" or "GEO"
    passed: bool
    weight: int
    detail: str


@dataclass
class Report:
    url: str
    seo_score: int = 0
    geo_score: int = 0
    checks: list[Check] = field(default_factory=list)
    ai_plan: str = ""


def fetch(url: str, timeout: int = 20) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode(r.headers.get_content_charset() or "utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except (urllib.error.URLError, TimeoutError):
        return 0, ""


def robots_blocks(robots_txt: str, agent: str) -> bool:
    """True if robots.txt fully disallows `agent` (simple, conservative parser)."""
    groups, current, blocked = [], None, {}
    for raw in robots_txt.splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        k, v = (s.strip() for s in line.split(":", 1))
        k = k.lower()
        if k == "user-agent":
            if current is None or current["rules"]:
                current = {"agents": [], "rules": []}
                groups.append(current)
            current["agents"].append(v.lower())
        elif k in ("allow", "disallow") and current is not None:
            current["rules"].append((k, v))
    for g in groups:
        for a in g["agents"]:
            blocked[a] = any(k == "disallow" and v == "/" for k, v in g["rules"]) and \
                not any(k == "allow" and v == "/" for k, v in g["rules"])
    return blocked.get(agent.lower(), blocked.get("*", False))


def audit_html(url: str, html: str, robots_txt: str | None = None,
               llms_txt_found: bool | None = None) -> Report:
    p = PageParser()
    p._base_host = urllib.parse.urlparse(url).netloc
    p.feed(html)
    words = len(" ".join(p.text_chunks).split())
    schema_types: list[str] = []
    for blob in p.jsonld:
        try:
            data = json.loads(blob)
        except json.JSONDecodeError:
            continue
        items = data if isinstance(data, list) else data.get("@graph", [data])
        for it in items:
            t = it.get("@type") if isinstance(it, dict) else None
            schema_types += t if isinstance(t, list) else ([t] if t else [])

    desc = p.meta.get("description", "")
    h1s = p.headings["h1"]
    question_heads = [h for lvl in ("h2", "h3") for h in p.headings[lvl] if h.rstrip().endswith("?")]
    c: list[Check] = []
    add = lambda *a: c.append(Check(*a))  # noqa: E731

    # ---- classic SEO
    add("title", "SEO", 10 <= len(p.title) <= 65, 10, f"Title ({len(p.title)} chars): {p.title or '—'}")
    add("meta_description", "SEO", 50 <= len(desc) <= 160, 10, f"Meta description: {len(desc)} chars")
    add("single_h1", "SEO", len(h1s) == 1, 10, f"{len(h1s)} H1 tag(s)")
    add("canonical", "SEO", bool(p.canonical), 5, p.canonical or "No canonical link")
    add("lang", "SEO", bool(p.lang), 5, f"<html lang> = {p.lang or 'missing'}")
    add("viewport", "SEO", "viewport" in p.meta, 5, "Mobile viewport meta present" if "viewport" in p.meta else "No viewport meta")
    add("image_alt", "SEO", p.images_missing_alt == 0, 5, f"{p.images_missing_alt}/{p.images} images missing alt")
    add("open_graph", "SEO", "og:title" in p.meta and "og:image" in p.meta, 5, "Open Graph title+image")
    add("content_depth", "SEO", words >= 300, 10, f"~{words} words of visible text")
    add("internal_links", "SEO", p.links_internal >= 3, 5, f"{p.links_internal} internal / {p.links_external} external links")

    # ---- GEO: can AI engines read, trust and quote this page?
    add("structured_data", "GEO", bool(schema_types), 15, f"JSON-LD types: {', '.join(schema_types) or 'none'}")
    has_entity = any(t in schema_types for t in ("Organization", "LocalBusiness", "Person"))
    add("entity_schema", "GEO", has_entity, 10,
        "Organization / LocalBusiness / Person entity defined" if has_entity else "No Organization/LocalBusiness entity schema")
    add("faq_or_qa", "GEO", "FAQPage" in schema_types or len(question_heads) >= 2, 10,
        f"{len(question_heads)} question-style headings; FAQPage schema: {'yes' if 'FAQPage' in schema_types else 'no'}")
    answer_first = 40 <= len(desc) and words >= 150
    add("answer_first", "GEO", answer_first, 10,
        "Clear answer-first summary" if answer_first else "No answer-first summary (thin intro or short description)")
    add("heading_structure", "GEO", len(p.headings["h2"]) >= 3, 10, f"{len(p.headings['h2'])} H2 sections (chunkable for LLMs)")
    if robots_txt is not None:
        blocked = [a for a in AI_CRAWLERS if robots_blocks(robots_txt, a)]
        add("ai_crawlers_allowed", "GEO", not blocked, 15, "Blocked: " + ", ".join(blocked) if blocked else "All major AI crawlers allowed")
    if llms_txt_found is not None:
        add("llms_txt", "GEO", llms_txt_found, 5, "/llms.txt present" if llms_txt_found else "No /llms.txt")

    def score(area: str) -> int:
        items = [x for x in c if x.area == area]
        total = sum(x.weight for x in items) or 1
        return round(100 * sum(x.weight for x in items if x.passed) / total)

    return Report(url=url, seo_score=score("SEO"), geo_score=score("GEO"), checks=c)


def audit_url(url: str) -> Report:
    status, html = fetch(url)
    if not html:
        sys.exit(f"Could not fetch {url} (HTTP {status or 'error'})")
    root = "{0.scheme}://{0.netloc}".format(urllib.parse.urlparse(url))
    _, robots = fetch(root + "/robots.txt")
    llms_status, _ = fetch(root + "/llms.txt")
    return audit_html(url, html, robots_txt=robots, llms_txt_found=llms_status == 200)


# --------------------------------------------------------------------------- Claude
def ai_fix_plan(report: Report, model: str = "claude-sonnet-5-5") -> str:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return "(Set ANTHROPIC_API_KEY to get an AI fix plan.)"
    failed = [asdict(x) for x in report.checks if not x.passed]
    prompt = (
        "You are a senior SEO + GEO consultant. Given these failed checks for "
        f"{report.url} (SEO {report.seo_score}/100, GEO {report.geo_score}/100), write a "
        "prioritized fix plan: max 7 bullets, highest impact first, each with the concrete "
        "change to make and why it helps Google and AI answer engines.\n\n"
        + json.dumps(failed, indent=2)
    )
    body = json.dumps({"model": model, "max_tokens": 900,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, headers={
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read())
        return "".join(b.get("text", "") for b in data.get("content", []))
    except urllib.error.HTTPError as e:
        return f"(Claude API error {e.code}: {e.read().decode()[:200]})"


# --------------------------------------------------------------------------- CLI
def render(report: Report) -> str:
    out = [f"\nGEO Audit — {report.url}", f"SEO score: {report.seo_score}/100   GEO score: {report.geo_score}/100\n"]
    for area in ("SEO", "GEO"):
        out.append(f"[{area}]")
        for x in report.checks:
            if x.area == area:
                out.append(f"  {'PASS' if x.passed else 'FAIL'}  {x.id:<20} {x.detail}")
        out.append("")
    if report.ai_plan:
        out += ["[AI fix plan]", report.ai_plan]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="SEO + GEO audit for a web page")
    ap.add_argument("url", nargs="?", help="Page URL to audit")
    ap.add_argument("--file", help="Audit a local HTML file instead of fetching")
    ap.add_argument("--json", metavar="PATH", help="Also write the report as JSON")
    ap.add_argument("--ai", action="store_true", help="Ask Claude for a prioritized fix plan")
    args = ap.parse_args(argv)
    if not args.url and not args.file:
        ap.error("give a URL or --file")

    if args.file:
        with open(args.file, encoding="utf-8") as f:
            report = audit_html(args.url or "https://local.test/", f.read())
    else:
        report = audit_url(args.url)
    if args.ai:
        report.ai_plan = ai_fix_plan(report)
    print(render(report))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(asdict(report), f, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
