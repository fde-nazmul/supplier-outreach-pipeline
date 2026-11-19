#!/usr/bin/env python3
"""
supplier-outreach-pipeline — turn a list of B2B supplier websites into a
human-reviewed outreach queue.

    discover (CSV) -> audit site (SEO + GEO) -> score opportunity -> draft email (Claude)
                   -> review queue (human approves) -> export approved leads for CRM

Nothing is ever sent automatically. The pipeline stops at a review queue so a
person approves every message — the "assisted outreach" pattern.

Usage:
    python pipeline.py run examples/suppliers.csv            # build out/review_queue.csv
    python pipeline.py run examples/suppliers.csv --offline  # demo with no network
    python pipeline.py approve 1 3                           # approve rows by id
    python pipeline.py export                                # approved rows -> out/crm_import.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import pathlib
import urllib.request

from geo_audit import Report, audit_url  # vendored from github.com/fde-nazmul/geo-audit-agent

OUT = pathlib.Path("out")
QUEUE = OUT / "review_queue.csv"
FIELDS = ["id", "company", "website", "email", "product", "seo_score", "geo_score",
          "opportunity", "tier", "top_issues", "subject", "body", "status"]


# --------------------------------------------------------------------------- scoring
def opportunity(report: Report) -> tuple[int, str, list[str]]:
    """A weak website is a strong lead: invert the audit into an opportunity score."""
    avg = (report.seo_score + report.geo_score) / 2
    score = round(100 - avg)
    tier = "A" if score >= 50 else "B" if score >= 30 else "C"
    issues = [c.detail for c in sorted(report.checks, key=lambda c: -c.weight) if not c.passed][:3]
    return score, tier, issues


# --------------------------------------------------------------------------- drafting
TEMPLATE = """Hi {company} team,

I came across your {product} range and ran a quick audit of {website}.
A few things stood out that are likely costing you international buyers:

{issues}

Buyers increasingly ask ChatGPT, Claude and Google AI for suppliers, and these gaps
make your site hard for them to find and quote. I've put together a free, no-obligation
audit report with the exact fixes — happy to send it over.

Best regards,
ZeekWeb Team
https://zeekweb.com"""


def draft_email(row: dict, issues: list[str], model: str = "claude-sonnet-5-5") -> tuple[str, str]:
    subject = f"Free website audit for {row['company']}"
    bullet_list = "\n".join(f"- {i}" for i in issues) or "- (no major issues found)"
    fallback = TEMPLATE.format(issues=bullet_list, **row)
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return subject, fallback
    prompt = (
        "Rewrite this B2B cold email to a Chinese manufacturer so it is short (under 120 words), "
        "specific, polite and in plain English. Keep the offer (free audit report) and the "
        "signature. Return only the email body.\n\n" + fallback
    )
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps({"model": model, "max_tokens": 600,
                         "messages": [{"role": "user", "content": prompt}]}).encode(),
        headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read())
        return subject, "".join(b.get("text", "") for b in data["content"]).strip() or fallback
    except Exception:  # never let the model block the pipeline
        return subject, fallback


# --------------------------------------------------------------------------- commands
def offline_report(url: str) -> Report:
    sample = pathlib.Path(__file__).parent / "examples" / "sample.html"
    from geo_audit import audit_html
    return audit_html(url, sample.read_text(), robots_txt="User-agent: GPTBot\nDisallow: /", llms_txt_found=False)


def cmd_run(args) -> None:
    OUT.mkdir(exist_ok=True)
    with open(args.csv, newline="", encoding="utf-8") as f:
        leads = list(csv.DictReader(f))
    rows = []
    for i, lead in enumerate(leads, 1):
        report = offline_report(lead["website"]) if args.offline else audit_url(lead["website"])
        score, tier, issues = opportunity(report)
        subject, body = draft_email(lead, issues)
        rows.append({**lead, "id": i, "seo_score": report.seo_score, "geo_score": report.geo_score,
                     "opportunity": score, "tier": tier, "top_issues": " | ".join(issues),
                     "subject": subject, "body": body, "status": "pending_review"})
        print(f"[{i}/{len(leads)}] {lead['company']:<28} tier {tier}  opportunity {score}")
    rows.sort(key=lambda r: -r["opportunity"])
    write(rows)
    print(f"\nReview queue -> {QUEUE}  ({len(rows)} drafts awaiting approval)")


def cmd_approve(args) -> None:
    rows = read()
    for r in rows:
        if r["id"] in args.ids:
            r["status"] = "approved"
    write(rows)
    print(f"Approved: {', '.join(args.ids)}")


def cmd_export(_args) -> None:
    approved = [r for r in read() if r["status"] == "approved"]
    path = OUT / "crm_import.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["company", "email", "website", "tier", "opportunity",
                                          "top_issues", "subject", "body"], extrasaction="ignore")
        w.writeheader()
        w.writerows(approved)
    print(f"{len(approved)} approved lead(s) -> {path} (import into HubSpot or any CRM)")


def read() -> list[dict]:
    with open(QUEUE, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write(rows: list[dict]) -> None:
    with open(QUEUE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def main() -> None:
    ap = argparse.ArgumentParser(description="Assisted B2B supplier outreach pipeline")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="audit + score + draft into a review queue")
    r.add_argument("csv")
    r.add_argument("--offline", action="store_true", help="use the bundled sample page (no network)")
    r.set_defaults(func=cmd_run)
    a = sub.add_parser("approve", help="approve queue rows by id")
    a.add_argument("ids", nargs="+")
    a.set_defaults(func=cmd_approve)
    sub.add_parser("export", help="export approved rows for CRM import").set_defaults(func=cmd_export)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
