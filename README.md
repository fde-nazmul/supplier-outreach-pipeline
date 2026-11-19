# supplier-outreach-pipeline

**An AI lead-generation pipeline for B2B agencies: audit supplier websites, score the opportunity, draft personalized outreach with Claude, and stop at a human review queue.**

This is the open-source core of an outreach system I run at [ZeekWeb](https://zeekweb.com). It finds exporters on B2B marketplaces whose own websites are weak and offers them a free audit. The production version adds HubSpot sync, email warm-up and WhatsApp follow-up. This repo keeps the parts that are useful to anyone.

```text
CSV of suppliers ─▶ SEO + GEO audit ─▶ opportunity score ─▶ Claude email draft ─▶ review queue ─▶ CRM export
                    (geo_audit.py)      (weak site = hot lead)   (template fallback)   (human approves)
```

## Why "assisted" outreach

Fully automatic cold email burns domains and trust. The pipeline does the slow work (research, audit, first draft) and leaves the send decision to a person:

- Every draft lands in `out/review_queue.csv` with status `pending_review`.
- `approve` flips selected rows to `approved`.
- `export` writes only approved leads to `out/crm_import.csv`, ready for HubSpot or any CRM.

## Quick start

Python 3.9+, no dependencies.

```bash
git clone https://github.com/fde-nazmul/supplier-outreach-pipeline
cd supplier-outreach-pipeline

python pipeline.py run examples/suppliers.csv --offline   # demo, no network
python pipeline.py run examples/suppliers.csv             # live audits
export ANTHROPIC_API_KEY=sk-ant-...                       # optional: Claude-written drafts

python pipeline.py approve 1 3
python pipeline.py export
```

Sample output:

```text
[1/3] Acme Lighting                tier B  opportunity 30
[2/3] Delta Hardware               tier B  opportunity 30
[3/3] Orion Textiles               tier B  opportunity 30

Review queue -> out/review_queue.csv  (3 drafts awaiting approval)
```

## Input format

`examples/suppliers.csv`:

| company | website | email | product |
|---|---|---|---|
| Acme Lighting | https://acme-lighting.test/ | sales@acme-lighting.test | table lamp |

## Scoring

`opportunity = 100 − average(SEO score, GEO score)`

| Tier | Opportunity | Meaning |
|---|---|---|
| A | ≥ 50 | Site is costing them buyers — prioritise |
| B | 30–49 | Clear quick wins |
| C | < 30 | Site is in decent shape — low priority |

The three highest-weight failed checks become the talking points in the email, so every message is specific to that supplier.

## Design notes (FDE lens)

- **The model never blocks the pipeline.** If the Claude call fails or no key is set, a deterministic template is used.
- **Cheap by default.** One model call per lead; the audit itself is pure Python. In production the whole system runs on a ~$50/month API budget.
- **Data stays in plain CSV**, so a non-technical client can open, edit and approve rows in Excel.
- **The audit is vendored** from [`geo-audit-agent`](https://github.com/fde-nazmul/geo-audit-agent) so this repo runs on its own.

## License

MIT
