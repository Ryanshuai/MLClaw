---
name: data-audit-report
description: >
  Render a completed data audit as one self-contained HTML page — the findings a person has to look
  at rather than read: the flagged samples themselves with their annotations drawn on, distributions
  with the outliers marked, the schema diff against the prior version. Trigger for: turning audit
  findings into something shareable, seeing the flagged images, showing a class imbalance or a
  long-tail to somebody, sending data problems to whoever owns the labels. Also trigger for Chinese
  requests like "生成数据报告", "把坏样本挑出来看看", "类别分布画一下", "这批数据的问题发给标注",
  "数据质量报告". Requires /data-audit to have run. Not for the data lifecycle board across all
  datasets (use /data-report) and not for a model's results (use /eval-report).
---

# /data-audit-report — the looking surface

```
datasets/<id>/audits/<audit_id>/audit.json   →   .../audit_report.html
```

An audit's findings split cleanly in two, and only one half is worth a page. "Class list has 81
entries, the model config says 80" is a sentence — rendering it costs more than reading it. "Here
are the 23 bounding boxes that fall outside their image" is not a sentence in any useful sense; it
is 23 pictures, and whether they are a broken export or a legitimate edge case is a judgement
nobody can make from the number.

**Render the second half. Say the first half in text and move on.**

## It computes nothing

Every number, verdict and locator comes from `audit.json`. The renderer must never re-derive a
threshold, recount a category, or decide a severity — in Python or in the page's JS. A second
implementation is a second set of answers, and the one on the screen is the one people act on. Same
rule as `/data-report`, and the consequence is the same: **if a number looks wrong on the page, the
bug is in the audit**, and fixing it here would hide it.

What the renderer does do is selection: which findings have enough samples to be worth a gallery,
which fields have enough range to be worth a histogram.

## Self-contained, which rules out the obvious implementation

Inline CSS and JS, images as base64, **no CDN, no fonts, no network**. Opens from a `file://` URL,
survives being emailed to whoever owns the labels — which is the whole delivery path for this
particular report, since its audience is usually not the person who ran the audit.

That rules out reaching for Plotly or any charting library off a CDN. The charts here are
histograms, bar charts and a diff table; hand-written SVG covers all three and stays readable in
light and dark. If a chart genuinely needs a library, render it to a PNG locally and embed the
bytes — a page that renders only for people with internet is not a page you can send to a vendor.

## The three things it must never do

| Never | Why |
|---|---|
| draw an advisory finding with a fatal's weight | A 3σ outlier is a sample worth a look; a category id the dataloader clamps is a model that will be wrong. A page that colours them alike teaches its reader that red means nothing. Severity is `audit.json`'s, and it maps to visual weight, not to whatever looks balanced on the page. |
| omit a check the audit **skipped** | An absent section reads as a passed one. Render skipped checks explicitly, with what was skipped and why — the field is in `audit.json` precisely so this page cannot lose it. |
| show a gallery without its denominator | Twelve thumbnails under "empty annotations" says nothing about whether that is 12 of 5 240 or 12 of 14. Every gallery states how many were found, how many are shown, and how the shown ones were picked. |

The third one is CLAUDE.md "Never report data you could not look at" in its most tempting form: a
grid of thumbnails is exactly what a complete picture looks like, and a truncated gallery is the
easiest place in MLClaw to imply one by accident.

## What earns a section

- **flagged-sample gallery** — the annotation drawn on the actual image, one tile per finding,
  grouped by finding type. This is the section that justifies the report existing.
- **distributions** — per numeric field, with mean/σ marked and the outliers picked out in the
  distribution rather than listed beside it.
- **class balance** — sorted horizontal bars, long-tail classes marked at the threshold the audit
  used. Not a pie chart, at any class count.
- **schema diff** — the prior version beside the current one, changed rows marked, and **renumbered
  categories called out separately from added ones**. Same names with shifted ids is the change that
  reads as harmless and is not.
- **the fatal list** — plain text at the top, above every chart. Whoever opens this page needs to
  know in one screen whether the data is usable.

Sections with no data are omitted, and the omission is stated in the header rather than left to be
inferred from a gap.

## Size

Base64 images dominate. Cap the total page, downscale thumbnails, cap tiles per gallery — and when
anything is dropped for size, **say so at the gallery, not in a footnote**. A silent cap is the
denominator rule broken by a different route.

## Requires / suggests

- **Requires**: `datasets/<id>/audits/<audit_id>/audit.json`. If there is none, offer `/data-audit`.
  A report of an audit that stopped at a Step 1 fatal is legitimate and often the useful one — render
  it, with the unrun steps shown as skipped.
- **Suggests**: whatever the findings own — a `/data-label` rework, `/data-curate` for a format
  defect, `/data-freeze` for a corrected snapshot. Rendering changes nothing about who owns what, so
  this skill routes exactly where `/data-audit` did and never softens it.

Per `lifecycle/references/skill-graph.md` → "Workflow State Protocol", push on entry and pop on exit
— `stage: null`, `execution: null`. Rendering is not an execution to resume.
