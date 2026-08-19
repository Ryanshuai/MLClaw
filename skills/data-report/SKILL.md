---
name: data-report
description: >
  Render the whole data lifecycle as one self-contained HTML board, Airflow-grid shaped: rows are
  datasets, columns are censuses over time, and clicking a cell opens that moment. Shows what blocks
  each dataset, census verdicts, open handoffs, snapshot staleness. Trigger for: wanting to see all
  datasets at once, how a dataset got where it is, a status page to share or put on a screen, a
  weekly data review, or checking what is stuck without asking dataset by dataset. Also trigger for
  Chinese requests like "数据看板", "所有数据集什么状态", "出个数据报告", "哪批数据卡住了",
  "看一眼全部数据", "这批数据这几周是怎么走的". Not for one dataset's position (use /data) or for
  scanning what exists (use /data-check).
---

# /data-report — the board

```bash
python <mlclaw_root>/scripts/data-report/board.py \
    --project <p> [--out <file.html>] [--stale-days N] [--last N]
```

Writes `<project>/data_board.html` by default and prints a summary. Self-contained: inline CSS and
JS, no CDN, no fonts, no network. Opens from a file:// URL, survives being emailed, renders in light
and dark.

## The grid

Airflow's grid view, with time on x — rows are datasets, columns are censuses, and the cell is where
that dataset stood when that scan ran. `1` Collect · `2` Label · `3` Curate · `4` Freeze · `✓` Ready,
coloured by health. Click any cell for that moment's blockers, verdicts, snapshots and open batches.

Three cell states that are not the same thing and are never drawn the same way:

| Cell | Means |
|---|---|
| solid | **this dataset's own scan** ran on this column |
| faded, with an age badge | nobody scanned it that day; this is its last known state, carried forward, and the badge is how far |
| `–` | it did not exist yet — distinct from "scanned and found nothing" |

The axis is the union across datasets, so most columns belong to *some other* dataset's scan. Solid
versus faded is an identity test on the timestamp, never a tolerance: five hours of carry-forward is
still carry-forward, and drawing it solid would claim a scan that never ran.

**Each column is a replay, not a re-reading.** `phase.py history` filters every record to what
existed at that scan, so a batch accepted last Friday still shows as *out* on the Tuesday before.
Records that carry no placeable timestamp are reported, and the column is marked a partial replay —
"not placed" is not "absent".

**The rightmost column is `now`, and it is not a scan.** It has to exist: a snapshot cut from census
N necessarily postdates N, so N's own replay correctly shows nothing frozen, and without a live
column you would freeze a dataset and watch the board not move. **`now` disagreeing with the last
census column is the finding** — it is everything that has happened since anyone looked.

## A board is not a dashboard

README's "no live dashboard" is about a **running service** — something that polls, streams, and
has to be up. This is the same thing `/eval-report`, `/refactor-report` and `/train-tune-report`
already are: an artifact rendered on demand from records that are already on disk. Nothing here
listens on a port.

**Do not make it auto-refresh, and do not put it behind a server.** The reason is not purity, it is
that refreshing would lie. See "Why not real-time" below.

## It computes nothing

Every number comes from `phase.py` — `phase` for the live column, `history` for the rest. The
renderer must never re-derive a phase, a blocker or a verdict, in Python or in the page's JS: a
second implementation is a second set of answers, and the one on the wall is the one people will act
on. That is why the replay lives in `phase.py` and not here, even though only this skill uses it.

Two things the renderer *does* do, and both are selection rather than computation: it picks which
precomputed column to show for a day nobody scanned, and it says how old that pick is.

Consequence worth knowing: **this skill is only ever as right as `/data` is.** If a phase looks
wrong on the board, the bug is in `phase.py`, and fixing it on the board would hide it.

## What it deviates on, and why

The other report skills have the agent write the HTML. This one is a script. A board's value is
noticing what changed since last week, and a layout redrawn differently each time cannot be
compared with the last one. Per CLAUDE.md "Script Integration" the fallback rule still holds: if
`board.py` breaks, render the same sections by hand and carry on.

## The one thing it must never do

**A partial census must never render as an inventory.** When any location did not answer, every
count is a lower bound, and the board says so in a banner *above* the numbers — CLAUDE.md "Never
report data you could not look at". A board is the single most dangerous place for this rule,
because a page of tidy counts is exactly what an inventory looks like. Datasets that have never
been scanned are named in the same banner.

If you render this by hand as a fallback, that banner is the part you cannot skip.

## Why not real-time

Nothing on this line moves fast enough for it to mean anything, and pretending otherwise makes the
page worse rather than better:

| What changes | How often |
|---|---|
| a capture session | hours, once a day at most |
| a handoff | days to weeks |
| a curate run | minutes to hours |
| a census scan | **it is the expensive operation** — ssh to every machine, walk terabytes |

**The board is never stale because it is not live. It is stale because the census is stale.** An
auto-refreshing page would re-render the same 10-day-old census every few seconds and look fresh
doing it — strictly worse than a page with a timestamp, because it removes the one cue that the
facts underneath are old.

**The grid draws that instead, which is the better answer to the same want.** A stretch of faded
cells with growing age badges *is* a picture of nobody looking, and it is visible from across a
room. The gaps in the grid are the point. A live-updating page would have had to hide them to keep
every cell looking current.

**What people actually want when they ask for live is a schedule.** Run `census.py scan` then
`board.py` on a cron — nightly is usually right, since it is the capture cadence — and the board is
never more than a day behind the world. That is real freshness. Polling is not.

## Requires / suggests

- **Requires**: `project.json` and at least one `datasets/<id>/dataset.json`. With no census yet it
  still renders — every dataset in `Collect`, named in the banner as never scanned, which is the
  correct picture of a project that has declared datasets and looked at none of them.
- **Suggests**: whatever the blocker table names — `/data` owns that routing (its "Composing the
  line"), and this page is a reading surface, not a router. When the user's question narrows from
  "what is the state of everything" to one dataset, hand over to `/data`.

Per `<mlclaw_root>/references/skill-graph.md` -> "Workflow State Protocol", push on entry and pop on exit — `stage: null`,
`execution: null`. Rendering is not an execution to resume.
