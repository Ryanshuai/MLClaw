#!/usr/bin/env python3
"""Render the data lifecycle as one page: where every dataset is, and how it got there.

Airflow's grid view, with time on x. Rows are datasets, columns are censuses,
and a cell is where that dataset stood when that scan ran. Clicking one opens
that moment: its phase, its blockers, what was still out with a vendor.

A board, not a dashboard. Nothing here is live, nothing polls, nothing is served
— it is a self-contained HTML file generated on demand from records that already
exist, the same shape as `/eval-report` and `/train-tune-report`. README's "no
live dashboard" is about a running service, not about rendering what is on disk.

WHY THERE IS NO AUTO-REFRESH, and why the grid is better without one. A board
does not go out of date because the page is old; it goes out of date because the
CENSUS is old. Re-rendering every thirty seconds would redraw an eleven-day-old
scan with a fresh timestamp on it, which is the most convincing possible way to
lie about data nobody has looked at. So staleness is drawn instead: a column
where a dataset was not scanned carries its last known state FORWARD, faded, with
the age on it. The gaps in the grid are the point — they are where nobody looked.

**Pure renderer.** Every number comes from `phase.py`, which is the one place a
dataset's position is computed — `phase` for now, `history` for the timeline.
This file must never work it out again: a second implementation of the phase
rules is a second set of answers, and the one on the wall is the one people will
believe. Carry-forward is selection, not computation: it picks which precomputed
column to show and says how old it is.

Unlike the other report skills, this is a script rather than agent-written HTML.
The board's value is spotting a change at a glance across weeks, and a layout
that is redrawn differently each time cannot be compared with the last one. The
fallback rule still applies — if this breaks, render it by hand.
"""

import argparse
import html
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "shared"))
from _records import (broke)  # noqa: E402

PHASES = [
    ("collect", "Collect"),
    ("label", "Label"),
    ("curate", "Curate"),
    ("freeze", "Freeze"),
    ("ready", "Ready"),
]

# Verdicts come from census.py and keep its vocabulary and its ordering. The
# last two are the ones a plain sync report would not have; they are listed
# last so they read as the additions they are.
VERDICTS = [
    ("gap", "GAP", "needs compute"),
    ("drift", "DRIFT", "needs sync"),
    ("incomplete", "INCOMPLETE", "nothing claims it finished"),
    ("unreplicated", "UNREPLICATED", "one disk from total loss"),
    ("unarchived", "UNARCHIVED", "never left the machine that made it"),
]

HERE = os.path.dirname(os.path.abspath(__file__))
PHASE_PY = os.path.join(HERE, "..", "data", "phase.py")


def e(x):
    return html.escape("" if x is None else str(x))


def gather(verb, project, stale_days, *extra):
    """Shell out rather than import: one computation of phase, in one file.

    `-X utf8` on the child because this end decodes utf-8 and the child's stdout
    otherwise follows the console codepage — cp1252 on Windows, where `phase.py`
    prints an em-dash and the decode blows up in subprocess's reader thread. The
    flag is per-process and does NOT inherit, so it has to be passed at every
    spawn; there are eight of them across `lifecycle/scripts/`.
    """
    if not os.path.isfile(PHASE_PY):
        broke(f"phase.py not found at {PHASE_PY}")
    p = subprocess.run([sys.executable, "-X", "utf8", PHASE_PY, verb, "--project", project,
                        "--stale-days", str(stale_days), *extra],
                       capture_output=True, text=True, encoding="utf-8")
    if p.returncode != 0 or not p.stdout.strip():
        broke(f"phase.py {verb} failed (exit {p.returncode}): "
              f"{(p.stderr or p.stdout).strip()[:400]}")
    try:
        return json.loads(p.stdout)
    except json.JSONDecodeError as exc:
        broke(f"phase.py {verb} did not return JSON: {exc}")


# Position on the line, as a digit, so a reader sees movement rather than having
# to decode five similar glyphs. Shape carries the state and colour carries the
# health — never the other way round, and never colour alone.
STEP = {"collect": "1", "label": "2", "curate": "3", "freeze": "4", "ready": "✓"}


def col_labels(axis):
    """`MM-DD` per column, disambiguated to `MM-DD HH:MM` only where a date
    appears twice. Two scans in one day is normal during a push and a grid that
    labels both `07-31` is a grid you cannot point at."""
    days = [t[5:10] for t in axis]
    return [t[5:10] if days.count(t[5:10]) == 1 else f"{t[5:10]} {t[11:16]}"
            for t in axis]


def carry_forward(timeline, axis):
    """For each column, the most recent replay at or before it.

    -> [(cell | None, age_days, is_this_column's_own_scan)]. This is selection,
    not computation: every cell was worked out by `phase.py history`. What is
    added here is the honesty about gaps — a dataset that was not scanned on a
    given day has its last known state shown FADED with its age, because "nobody
    has looked in eleven days" is the most useful thing this grid can say and a
    blank would read as nothing having happened.

    "Own scan" is an identity test on the timestamp, never an age threshold. The
    axis is the union across datasets, so a column often belongs to some other
    dataset's scan: boxes carried forward five hours reads as fresh under any
    tolerance, and drawing it solid would claim a scan that never ran.
    """
    out, i, cur = [], 0, None
    for t in axis:
        while i < len(timeline) and (timeline[i]["scanned_at"] or "") <= t:
            cur, i = timeline[i], i + 1
        if cur is None:
            out.append((None, None, False))
            continue
        try:
            gap = round((datetime.fromisoformat(t)
                         - datetime.fromisoformat(cur["scanned_at"])).total_seconds()
                        / 86400, 1)
        except (TypeError, ValueError):
            gap = None
        out.append((cur, gap, cur["scanned_at"] == t))
    return out


CSS = """
/* Status palette: the dataviz reference instance, used verbatim and unthemed.
   Both modes are SELECTED, not flipped — the dark steps are the same four hexes,
   which clear 3:1 on the dark surface. On the light surface `warning` and
   `serious` are sub-3:1 by design; the mitigation shipped with the palette is
   the icon+label pairing, which is why every cell in this grid carries a glyph
   and every legend entry a word. A status colour never carries meaning alone. */
.viz-root{color-scheme:light;
--surface-1:#fcfcfb;--surface-2:#f2f1ee;--text-primary:#0b0b0b;--text-secondary:#52514e;
--text-muted:#8a8880;--line:#e0ded8;
--good:#0ca30c;--warning:#fab219;--serious:#ec835a;--critical:#d03b3b}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])) .viz-root{
color-scheme:dark;--surface-1:#1a1a19;--surface-2:#232320;--text-primary:#fff;
--text-secondary:#c3c2b7;--text-muted:#8d8b82;--line:#33332f}}
:root[data-theme="dark"] .viz-root{color-scheme:dark;
--surface-1:#1a1a19;--surface-2:#232320;--text-primary:#fff;--text-secondary:#c3c2b7;
--text-muted:#8d8b82;--line:#33332f}
*{box-sizing:border-box}
body{margin:0;background:var(--surface-1);color:var(--text-primary);
font:14px/1.5 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif}
.viz-root{padding:2rem 1.25rem 4rem}
.wrap{max-width:1180px;margin:0 auto}
h1{font-size:1.35rem;margin:0 0 .2rem;font-weight:650;letter-spacing:-.01em}
h2{font-size:.78rem;margin:2.3rem 0 .6rem;font-weight:650;letter-spacing:.08em;
text-transform:uppercase;color:var(--text-secondary)}
.sub{color:var(--text-secondary);font-size:.83rem;margin-bottom:1.4rem}
.banner{border-left:3px solid var(--critical);background:var(--surface-2);
padding:.65rem .85rem;border-radius:0 4px 4px 0;margin:0 0 1.4rem;font-size:.85rem;
color:var(--text-primary)}
/* the grid — rows are datasets, columns are censuses. Airflow's grid view on
   real time, because "where is it" and "how did it get there" are the same
   question asked at two zoom levels. */
.scroll{overflow-x:auto}
table.grid{border-collapse:separate;border-spacing:2px;width:100%;min-width:760px}
table.grid th{font-size:.68rem;text-transform:uppercase;letter-spacing:.07em;
color:var(--text-muted);font-weight:600;padding:0 .3rem .35rem;text-align:center;
white-space:nowrap}
table.grid th.l,table.grid td.l{text-align:left}
td.ds{font-weight:600;font-size:.84rem;white-space:nowrap;padding-right:.7rem;
max-width:230px;overflow:hidden;text-overflow:ellipsis}
td.cell{width:66px;height:30px;text-align:center;border-radius:4px;
background:var(--surface-2);font-size:.8rem;font-weight:650;cursor:default;
font-variant-numeric:tabular-nums}
td.cell.done{color:var(--text-muted)}
td.cell.pending{color:var(--line)}
td.cell.here{background:color-mix(in srgb,var(--good) 20%,var(--surface-1));
color:var(--good);box-shadow:inset 0 0 0 1.5px var(--good)}
td.cell.warn{background:color-mix(in srgb,var(--warning) 22%,var(--surface-1));
color:var(--text-primary);box-shadow:inset 0 0 0 1.5px var(--warning)}
td.cell.serious{background:color-mix(in srgb,var(--serious) 24%,var(--surface-1));
color:var(--text-primary);box-shadow:inset 0 0 0 1.5px var(--serious)}
td.cell.stop{background:color-mix(in srgb,var(--critical) 22%,var(--surface-1));
color:var(--critical);box-shadow:inset 0 0 0 1.5px var(--critical)}
td.meta{font-size:.76rem;color:var(--text-secondary);white-space:nowrap;
font-variant-numeric:tabular-nums;padding-left:.5rem}
/* A carried-forward cell is the same state seen at a distance, so it is drawn
   as the same state seen at a distance. The age badge is the whole reason this
   board needs no auto-refresh: staleness is a thing you can look at. */
td.cell[data-k]{cursor:pointer}
td.cell[data-k]:hover,td.cell[data-k]:focus{outline:2px solid var(--text-secondary);
outline-offset:1px}
td.cell.carried{opacity:.42;box-shadow:none;background:var(--surface-2)}
td.cell.none{color:var(--line);background:transparent}
i.age{font-style:normal;font-size:.6rem;font-weight:500;margin-left:2px;
vertical-align:super;color:var(--text-muted)}
/* drill-down. Server-rendered grid, client-rendered panel: the page is still a
   readable board with JS off, which matters because it gets emailed. */
#panel{margin:.9rem 0 0;padding:.85rem 1rem;background:var(--surface-2);
border-radius:6px;font-size:.83rem;display:none}
#panel.on{display:block}
#panel h3{margin:0 0 .15rem;font-size:.95rem;font-weight:650}
#panel .when{color:var(--text-secondary);font-size:.78rem;margin-bottom:.6rem}
#panel dl{display:grid;grid-template-columns:auto 1fr;gap:.2rem .9rem;margin:0}
#panel dt{color:var(--text-muted);font-size:.72rem;text-transform:uppercase;
letter-spacing:.06em;white-space:nowrap;padding-top:.12rem}
#panel dd{margin:0}
#panel ul{margin:.15rem 0 0;padding-left:1.1rem}
#panel .warnline{margin-top:.6rem;padding:.4rem .6rem;border-left:3px solid var(--warning);
background:var(--surface-1);border-radius:0 3px 3px 0;font-size:.79rem}
.legend{display:flex;flex-wrap:wrap;gap:.15rem 1.1rem;margin:.8rem 0 0;
font-size:.76rem;color:var(--text-secondary)}
.legend span{display:inline-flex;align-items:center;gap:.35rem}
.sw{display:inline-flex;align-items:center;justify-content:center;width:19px;
height:19px;border-radius:4px;font-size:.72rem;font-weight:650;background:var(--surface-2)}
.sw.here{color:var(--good);box-shadow:inset 0 0 0 1.5px var(--good)}
.sw.warn{color:var(--text-primary);box-shadow:inset 0 0 0 1.5px var(--warning)}
.sw.serious{color:var(--text-primary);box-shadow:inset 0 0 0 1.5px var(--serious)}
.sw.stop{color:var(--critical);box-shadow:inset 0 0 0 1.5px var(--critical)}
.sw.done{color:var(--text-muted)}
.sw.pending{color:var(--line)}
table.det{width:100%;border-collapse:collapse;font-size:.82rem}
table.det th,table.det td{text-align:left;padding:.4rem .6rem;
border-bottom:1px solid var(--line);vertical-align:top;white-space:nowrap}
table.det th{font-size:.68rem;text-transform:uppercase;letter-spacing:.07em;
color:var(--text-muted);font-weight:600}
table.det td.num{text-align:right;font-variant-numeric:tabular-nums}
.z{color:var(--text-muted)}
.i-stop{color:var(--critical);font-weight:650}
.i-serious{color:var(--serious);font-weight:650}
.i-warn{color:var(--text-secondary)}
.wrapc{white-space:normal}
.foot{margin-top:2.4rem;padding-top:.85rem;border-top:1px solid var(--line);
color:var(--text-muted);font-size:.75rem;max-width:78ch}
code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.92em}
"""


# Four severities, four status roles. `blocks` stops every transition; the
# `blocks_<x>` family stops one, which is a materially smaller thing and gets its
# own step rather than being flattened into the same red.
def severity(d):
    sev = {b["severity"] for b in d["blockers"]}
    if "blocks" in sev:
        return "stop", "\u25b2", "blocked"
    if sev & {"blocks_freeze", "blocks_curate", "blocks_consume"}:
        return "serious", "\u25c6", "gated"
    if "warns" in sev:
        return "warn", "\u25cf", "warning"
    return "here", "\u25cf", "on track"


def cell_severity(cell):
    """Same four status roles as `severity`, off a replayed column's blockers."""
    sev = {b["severity"] for b in cell.get("blockers") or []}
    if "blocks" in sev:
        return "stop", "blocked"
    if sev & {"blocks_freeze", "blocks_curate", "blocks_consume"}:
        return "serious", "one transition gated"
    if "warns" in sev:
        return "warn", "warning"
    return "here", "on track"


def trim_axis(hist, last):
    """Keep only the last N columns — of the AXIS, not of each dataset's history.

    Those are different caps and only one of them is right here. Capping each
    dataset's timeline drops the entries carry-forward reads from, so a dataset
    scanned rarely would render as "did not exist yet" across columns where it
    plainly did. The axis is what a grid reader means by "the last 20".
    """
    axis = hist.get("axis") or []
    hist["axis"] = axis[-last:] if last and last > 0 else axis
    return hist


def now_cell(d):
    """The live assessment, reshaped to look like a replayed column.

    Reshaping, not computing: every value is lifted straight out of
    `phase.py phase`. The rightmost cell has to exist because a census column
    can never show a freeze that came *from* it \u2014 a snapshot frozen from
    census N necessarily postdates N, so N's own replay correctly reports
    nothing frozen. Without a live column you would freeze a dataset and watch
    the board not move.
    """
    c = d.get("census") or {}
    return {
        "census_id": c.get("census_id"), "scanned_at": c.get("scanned_at"),
        "phase": d["phase"], "why": d["why"], "next": d["next"],
        "next_skill": d["next_skill"],
        "census_complete": c.get("complete"),
        "counts_are_lower_bound": c.get("counts_are_lower_bound"),
        "totals": c.get("totals") or {},
        "handoffs": d["handoffs"],
        "snapshots": [s["snapshot_id"] for s in d.get("snapshots") or []],
        "stale_against": len(d.get("stale_against") or []),
        "staleness_undetermined": len(d.get("staleness_undetermined") or []),
        "units_deleted": (d.get("retire") or {}).get("units_deleted", 0),
        "blockers": [{k: b.get(k) for k in ("blocker", "severity", "detail", "fix")}
                     for b in d["blockers"]],
        "replay": None,
    }


def timeline_grid(hist, live):
    """Rows are datasets, columns are censuses, rightmost column is now.

    Airflow's grid on real time, including its rightmost-cell-is-live habit. The
    live column is what makes the gap since the last scan visible as a step in
    the row rather than as a discrepancy between this grid and the table under
    it: a batch accepted after the last census shows the row moving without any
    new scan behind it, which is the finding.

    Every cell carries a digit or a dash as well as a colour. That is not
    decoration: the status palette's own note says two of its four steps are
    sub-3:1 on the light surface by design, and the icon+label pairing is the
    mitigation shipped with it. Colour alone would also lose the whole grid for
    a CVD reader.
    """
    axis = hist.get("axis") or []
    rows = hist.get("datasets") or []
    by_name = {d["dataset"]: d for d in live.get("datasets") or []}
    if not rows:
        return ('<p class="z">no datasets declared.</p>'), {}

    labels = col_labels(axis) + ["now"]
    head = "".join(f'<th title="{e(t)}">{e(lab)}</th>'
                   for t, lab in zip(axis + ["the live assessment, not a scan"], labels))

    detail, out = {}, []
    for d in rows:
        cells = []
        seq = carry_forward(d["timeline"], axis)
        if d["dataset"] in by_name:
            seq = seq + [(now_cell(by_name[d["dataset"]]), 0, True)]
        for j, (cell, gap, own) in enumerate(seq):
            if cell is None:
                cells.append('<td class="cell none" title="this dataset did not '
                             'exist yet">\u2013</td>')
                continue
            cls, word = cell_severity(cell)
            key = f'{d["dataset"]}|{j}'
            detail[key] = dict(cell, _carried=not own, _gap_days=gap,
                               _column=axis[j] if j < len(axis) else "now",
                               _live=j >= len(axis), _dataset=d["dataset"])
            stamp = "" if own else (
                f'<i class="age">{gap:g}d</i>' if gap is not None else
                '<i class="age">?</i>')
            tip = (f'{cell["phase"]}: {word} \u2014 {cell["why"]}' if own else
                   f'not scanned on this date \u2014 carried forward from '
                   f'{cell["scanned_at"]}, {gap}d earlier')
            if j >= len(axis):
                tip = f'now (no new scan): {cell["phase"]} \u2014 {cell["why"]}'
            cells.append(
                f'<td class="cell {cls}{"" if own else " carried"}" tabindex="0" '
                f'role="button" data-k="{e(key)}" title="{e(tip)}">'
                f'{STEP.get(cell["phase"], "?")}{stamp}</td>')
        last = d["timeline"][-1] if d["timeline"] else None
        meta = "no census" if last is None else (
            f'{(last.get("totals") or {}).get("units", "\u2014")} units \u00b7 '
            f'{len(d["timeline"])} scan' + ("s" if len(d["timeline"]) != 1 else ""))
        out.append(f'<tr><td class="ds l" title="{e(d["dataset"])}">{e(d["dataset"])}</td>'
                   + "".join(cells) + f'<td class="meta">{e(meta)}</td></tr>')

    return (f'<table class="grid"><thead><tr><th class="l">Dataset</th>{head}'
            f'<th class="l">&nbsp;</th></tr></thead><tbody>{"".join(out)}</tbody>'
            f'</table>'), detail


LEGEND = [("here", "\u2713", "on track"), ("warn", "\u2713", "warning"),
          ("serious", "4", "one transition gated"),
          ("stop", "2", "blocked"), ("carried", "3", "carried forward \u2014 not scanned that day"),
          ("none", "\u2013", "did not exist yet")]

STEP_LEGEND = " \u00b7 ".join(f"<b>{v}</b> {k}" for k, v in STEP.items())


JS = r"""
// The panel only formats what phase.py already decided. It must not derive a
// phase, a blocker or a staleness verdict — see the module docstring.
(function(){
  var D = JSON.parse(document.getElementById("cells").textContent);
  var p = document.getElementById("panel");
  function esc(s){var d=document.createElement("div");d.textContent=s==null?"":s;return d.innerHTML;}
  function row(k,v){return "<dt>"+esc(k)+"</dt><dd>"+v+"</dd>";}
  function show(k){
    var c = D[k]; if(!c) return;
    var t = c.totals || {}, h = c.handoffs || {};
    var out = "<h3>"+esc(c._dataset)+" · "+esc(c.phase)+"</h3>";
    out += '<div class="when">'+
           (c._live ? "<b>now</b> — the live assessment, off census "+esc(c.census_id)+
                      " ("+esc(c.scanned_at)+"). No scan has run since."
                    : esc(c.scanned_at)+" · "+esc(c.census_id)+
                      (c._carried ? " · <b>not scanned on "+esc((c._column||"").slice(0,10))+
                                    "</b> — carried forward "+esc(c._gap_days)+" days" : ""))+
           "</div><dl>";
    out += row("why", esc(c.why));
    if(c.next_skill) out += row("next", "<code>"+esc(c.next_skill)+"</code> — "+esc(c.next));
    out += row("units", esc(t.units==null?"—":t.units) +
      ["gap","drift","unreplicated","unarchived","incomplete"]
        .filter(function(v){return t[v];})
        .map(function(v){return " · "+v.toUpperCase()+" "+t[v];}).join(""));
    out += row("handoffs", "open "+esc(h.open)+" · accepted "+esc(h.accepted));
    out += row("snapshots", (c.snapshots&&c.snapshots.length)
      ? c.snapshots.map(esc).join(", ")+(c.stale_against?" <b>· "+c.stale_against+" stale</b>":"")
      : "<span class=z>none frozen</span>");
    if(c.units_deleted) out += row("retired", esc(c.units_deleted)+" units deleted by then");
    if(c.blockers && c.blockers.length) out += row("blocking",
      "<ul>"+c.blockers.map(function(b){
        return "<li><b>"+esc(b.blocker)+"</b> — "+esc(b.detail)+"</li>";}).join("")+"</ul>");
    out += "</dl>";
    // Two different incompletenesses, never merged: one is machines that did
    // not answer, the other is records that could not be placed in time.
    if(c.counts_are_lower_bound) out += '<div class="warnline">Partial census — a location '+
      'did not answer, so every count above is a <b>lower bound</b>, not an inventory.</div>';
    if(c.replay && !c.replay.complete) out += '<div class="warnline">Partial replay — '+
      esc(c.replay.unplaceable.length)+' record(s) carry no placeable timestamp, so this '+
      'column is a reconstruction with holes in it.</div>';
    p.innerHTML = out; p.classList.add("on");
  }
  document.addEventListener("click", function(ev){
    var t = ev.target;
    var td = t && t.closest && t.closest("td.cell[data-k]"); if(td) show(td.dataset.k);});
  document.addEventListener("keydown", function(ev){
    if(ev.key!=="Enter" && ev.key!==" ") return;
    var td = ev.target.closest && ev.target.closest("td.cell[data-k]");
    if(td){ ev.preventDefault(); show(td.dataset.k); }});
})();
"""


def render(data, hist, project, stale_days):
    rows = data.get("datasets") or []
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # A partial census makes every count below a lower bound. Saying so at the
    # top, before any number, is CLAUDE.md "Never report data you could not look
    # at" — a board is exactly where a lower bound gets mistaken for an
    # inventory, because it looks so much like one.
    partial = [d["dataset"] for d in rows
               if (d.get("census") or {}).get("counts_are_lower_bound")]
    nocensus = [d["dataset"] for d in rows if d.get("census") is None]
    banner = ""
    if partial or nocensus:
        bits = []
        if partial:
            bits.append(f"<b>{len(partial)}</b> dataset(s) have a partial census "
                        f"({e(', '.join(partial))}) \u2014 a location did not answer, so "
                        f"<b>every count below is a lower bound, not an inventory</b>")
        if nocensus:
            bits.append(f"<b>{len(nocensus)}</b> dataset(s) have never been scanned "
                        f"({e(', '.join(nocensus))})")
        banner = f'<div class="banner">{" \u00b7 ".join(bits)}</div>'

    legend = "".join(f'<span><i class="sw {c}">{g}</i>{lab}</span>' for c, g, lab in LEGEND)
    tl, drill = timeline_grid(hist, data)

    # per-dataset detail
    trs = []
    for d in rows:
        c = d.get("census") or {}
        t_ = c.get("totals") or {}
        cells = "".join(
            f'<td class="num {"z" if not t_.get(k) else ""}">{t_.get(k) or 0}</td>'
            for k, _l, _n in VERDICTS)
        snaps = d.get("snapshots") or []
        newest = snaps[-1]["snapshot_id"] if snaps else None
        stale = any(b["blocker"] == "snapshot_stale" for b in d["blockers"])
        trs.append(
            f'<tr><td><b>{e(d["dataset"])}</b></td><td>{e(d["phase"])}</td>'
            + cells +
            f'<td class="num">{d["handoffs"]["open"]}</td>'
            f'<td>{e(newest) if newest else "<span class=z>none</span>"}'
            f'{" <span class=i-stop>stale</span>" if stale else ""}</td>'
            f'<td class="num">{len(d.get("consumers") or [])}</td></tr>')
    vhead = "".join(f'<th title="{e(n)}">{lab}</th>' for _k, lab, n in VERDICTS)

    bl = []
    for d in rows:
        for b in sorted(d["blockers"], key=lambda x: x["severity"] != "blocks"):
            cls = ("i-stop" if b["severity"] == "blocks"
                   else "i-warn" if b["severity"] == "warns" else "i-serious")
            bl.append(f'<tr><td><b>{e(d["dataset"])}</b></td>'
                      f'<td class="{cls}">{e(b["blocker"])}</td>'
                      f'<td class="z">{e(b["severity"])}</td>'
                      f'<td class="wrapc">{e(b["detail"])}</td>'
                      f'<td class="wrapc"><code>{e(b.get("fix", ""))}</code></td></tr>')
    blockers = "".join(bl) or '<tr><td colspan="5" class="z">nothing blocking</td></tr>'

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Data lifecycle \u2014 {e(os.path.basename(project.rstrip("/")))}</title>
<style>{CSS}</style></head><body><div class="viz-root"><div class="wrap">
<h1>Data lifecycle</h1>
<div class="sub">{e(project)} \u00b7 {len(rows)} dataset(s) \u00b7 rendered {e(now)}
 \u00b7 stale threshold {e(stale_days)}d</div>
{banner}
<div class="scroll">{tl}</div>
<div id="panel"></div>
<div class="legend">{legend}</div>
<div class="legend"><span>position: {STEP_LEGEND}</span></div>
<h2>Where each dataset is now</h2>
<div class="scroll"><table class="det">
<thead><tr><th>Dataset</th><th>Phase</th>{vhead}
<th>Open handoffs</th><th>Newest snapshot</th><th>Consumers</th></tr></thead>
<tbody>{"".join(trs) or '<tr><td colspan="10" class="z">no datasets declared</td></tr>'}</tbody>
</table></div>
<h2>What is blocking</h2>
<div class="scroll"><table class="det">
<thead><tr><th>Dataset</th><th>Blocker</th><th>Severity</th><th>Detail</th><th>Fix</th></tr></thead>
<tbody>{blockers}</tbody></table></div>
<div class="foot">
Rendered from <code>phase.py phase</code> and <code>phase.py history</code>, which is where a
dataset's position is computed; this page recomputes nothing. Each column is that census
<b>replayed</b> \u2014 only records that existed when it ran, so a batch accepted last Friday still
shows as out on the Tuesday before. <b>The last column can therefore disagree with the table
below it</b>, and that gap is a finding rather than an inconsistency: it is everything that has
happened since anyone last looked. <b>There is no auto-refresh on purpose:</b> a board goes
stale because the census is old, not because the page is, and a faded cell with an age on it
says that where a fresh timestamp would hide it. <code>Retire</code> never appears as a
position \u2014 it is an action on units, not a place a dataset arrives at. A snapshot is stale when
the census it froze from never saw inflow that has since been accepted; the citation still
resolves, which is why it needs saying. Colour never carries a state alone: every cell has a
glyph and the legend names every step.
</div></div></div>
<script id="cells" type="application/json">{json.dumps(drill, ensure_ascii=False).replace("</", "<\\/")}</script>
<script>{JS}</script>
</body></html>
"""


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--project", required=True)
    p.add_argument("--out", default=None, help="default: <project>/data_board.html")
    p.add_argument("--stale-days", type=float, default=14.0)
    p.add_argument("--last", type=int, default=20,
                   help="columns to draw, most recent first (default 20). A grid "
                        "wider than a screen stops being scannable, which is the "
                        "only thing it is for")
    a = p.parse_args()

    project = os.path.expanduser(a.project)
    if not os.path.isdir(project):
        broke(f"project not found: {project}")
    data = gather("phase", project, a.stale_days)
    # Deliberately NOT `--last`: phase.py's cap is per dataset, and carry-forward
    # needs the entries before the first drawn column to know what to carry in.
    hist = trim_axis(gather("history", project, a.stale_days), a.last)
    out = os.path.expanduser(a.out) if a.out else os.path.join(project, "data_board.html")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(render(data, hist, project, a.stale_days))
    rows = data.get("datasets") or []
    # A truncated axis is a silent cap, so it is reported rather than left to be
    # inferred from a grid that looks complete — CLAUDE.md "Never report data you could not look at".
    shown = len(hist.get("axis") or [])
    print(json.dumps({
        "board": out, "datasets": len(rows),
        "columns": shown, "column_limit": a.last,
        "by_phase": {k: sum(1 for d in rows if d["phase"] == k) for k, _ in PHASES},
        "blocked": data.get("blocked"),
        "partial_census": [d["dataset"] for d in rows
                           if (d.get("census") or {}).get("counts_are_lower_bound")],
        "partial_replay": sorted({d["dataset"] for d in hist.get("datasets") or []
                                  for t in d["timeline"]
                                  if t.get("replay") and not t["replay"]["complete"]}),
    }, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
