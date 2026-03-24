"""Build lineage DAG HTML from all runs in a project.

Usage:
    python build_dag.py <project_root> [--output <path>]

Scans stages/*/runs/*/run.json and stages/*/runs/*/manifest.json,
builds lineage graph, outputs a self-contained HTML visualization.
"""
import json
import os
import sys
import html
from glob import glob
from datetime import datetime


# ── Monokai palette ──────────────────────────────────────────────────────────
STAGE_COLORS = {
    "training":   {"accent": "#fd971f", "bg": "#2a2418", "border": "rgba(253,151,31,0.2)"},
    "evaluation": {"accent": "#a6e22e", "bg": "#1f2518", "border": "rgba(166,226,46,0.2)"},
    "inference":  {"accent": "#66d9ef", "bg": "#182225", "border": "rgba(102,217,239,0.2)"},
    "data":       {"accent": "#ae81ff", "bg": "#211e28", "border": "rgba(174,129,255,0.2)"},
    "deployment": {"accent": "#f92672", "bg": "#281a20", "border": "rgba(249,38,114,0.2)"},
}
DEFAULT_COLOR = {"accent": "#b0b0b0", "bg": "#252525", "border": "rgba(176,176,176,0.2)"}


def load_runs(project_root):
    """Scan for run.json and manifest.json, return list of run dicts."""
    runs = []
    stages_dir = os.path.join(project_root, "stages")
    if not os.path.isdir(stages_dir):
        return runs

    for stage_name in sorted(os.listdir(stages_dir)):
        runs_dir = os.path.join(stages_dir, stage_name, "runs")
        if not os.path.isdir(runs_dir):
            continue
        for run_name in sorted(os.listdir(runs_dir)):
            run_dir = os.path.join(runs_dir, run_name)
            if not os.path.isdir(run_dir):
                continue

            # Try run.json first, then manifest.json
            run_file = os.path.join(run_dir, "run.json")
            if not os.path.isfile(run_file):
                run_file = os.path.join(run_dir, "manifest.json")
            if not os.path.isfile(run_file):
                continue

            with open(run_file, encoding="utf-8") as f:
                run = json.load(f)

            run.setdefault("stage", stage_name)
            run.setdefault("run_id", run_name)
            runs.append(run)

    return runs


def build_graph(runs):
    """Build nodes and edges from run records."""
    nodes = {}
    cross_edges = []  # cross-stage lineage (parents)
    fork_edges = []   # same-stage fork

    for run in runs:
        run_id = run["run_id"]
        stage = run["stage"]
        full_id = f"{stage}/{run_id}"

        # Extract short date
        created = run.get("created_at", "")
        short_date = ""
        if created:
            try:
                dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                short_date = dt.strftime("%Y-%m-%d")
            except (ValueError, TypeError):
                short_date = created[:10] if len(created) >= 10 else created

        # Extract key metric
        metrics = run.get("metrics", {})
        key_metric = ""
        for k in ["map/map", "mAP", "accuracy", "f1"]:
            if k in metrics:
                key_metric = f"{k}={metrics[k]}"
                break
        if not key_metric and metrics:
            first_key = next(iter(metrics))
            key_metric = f"{first_key}={metrics[first_key]}"

        nodes[full_id] = {
            "run_id": run_id,
            "stage": stage,
            "full_id": full_id,
            "alias": run.get("alias", ""),
            "description": run.get("description", ""),
            "status": run.get("status", "unknown"),
            "date": short_date,
            "key_metric": key_metric,
            "local_tags": run.get("lineage", {}).get("local_tags", []),
        }

        # Cross-stage parents
        parents = run.get("lineage", {}).get("parents", [])
        for p in parents:
            if isinstance(p, str):
                cross_edges.append({"from": p, "to": full_id})
            elif isinstance(p, dict):
                pid = f"{p.get('stage', '')}/{p.get('run_id', '')}"
                cross_edges.append({"from": pid, "to": full_id})

        # Fork
        fork_of = run.get("lineage", {}).get("fork_of", "")
        if fork_of:
            fork_edges.append({"from": fork_of, "to": full_id})

    return nodes, cross_edges, fork_edges


def group_by_stage(nodes):
    """Group nodes by stage, preserving order."""
    stages = {}
    for nid, node in nodes.items():
        stage = node["stage"]
        stages.setdefault(stage, [])
        stages[stage].append(node)
    return stages


def esc(s):
    return html.escape(str(s))


def generate_html(nodes, cross_edges, fork_edges, project_name):
    """Generate self-contained HTML DAG."""
    stages = group_by_stage(nodes)

    # Canonical stage order
    stage_order = ["training", "data", "evaluation", "inference", "deployment"]
    ordered_stages = []
    for s in stage_order:
        if s in stages:
            ordered_stages.append(s)
    for s in stages:
        if s not in ordered_stages:
            ordered_stages.append(s)

    # Build columns HTML
    columns_html = ""
    node_index = {}  # full_id -> (stage_idx, node_idx) for arrow drawing

    for si, stage_name in enumerate(ordered_stages):
        color = STAGE_COLORS.get(stage_name, DEFAULT_COLOR)
        stage_runs = stages[stage_name]

        nodes_html = ""
        for ni, node in enumerate(stage_runs):
            node_index[node["full_id"]] = (si, ni)

            # Check if this node has a fork parent
            fork_parent = None
            for fe in fork_edges:
                if fe["to"] == node["full_id"]:
                    fork_parent = fe["from"]
                    break

            # Fork arrow before this node
            fork_html = ""
            if fork_parent:
                fork_html = f'''
            <div class="fork-arrow" style="--accent: {color['accent']}">
              <span class="fork-label">fork_of</span>
            </div>'''

            # Tag pills
            tags_html = ""
            if node["local_tags"]:
                for tag in node["local_tags"]:
                    tags_html += f'<span class="run-tag" style="background: {color["accent"]}18; color: {color["accent"]}">{esc(tag)}</span> '
            elif node["alias"]:
                tags_html = f'<span class="run-tag" style="background: {color["accent"]}18; color: {color["accent"]}">{esc(node["alias"])}</span>'

            # Description - truncate
            desc = node["description"]
            if len(desc) > 60:
                desc = desc[:57] + "..."

            # Metric display
            metric_html = ""
            if node["key_metric"]:
                metric_html = f'<span class="metric">{esc(node["key_metric"])}</span>'

            # Status
            status = node["status"]
            status_cls = "status-ok" if status in ("completed", "released") else "status-other"

            nodes_html += f'''{fork_html}
            <div class="run-node" id="{esc(node['full_id'])}" style="border-left-color: {color['accent']}">
              <div class="run-id" style="color: {color['accent']}">{esc(node['run_id'])}</div>
              <div class="run-desc">{esc(desc)}</div>
              <div class="run-meta">
                <span>{metric_html or esc(node['date'])}</span>
                <span class="{status_cls}">{esc(status)}</span>
              </div>
              <div class="run-meta-secondary">
                {f'<span>{esc(node["date"])}</span>' if metric_html else ''}
              </div>
              <div class="run-tags">{tags_html}</div>
            </div>'''

        columns_html += f'''
      <div class="stage-column" style="border-top-color: {color['accent']}">
        <div class="stage-header" style="background: {color['bg']}; color: {color['accent']}; border-color: {color['border']}">{esc(stage_name)}</div>
        {nodes_html}
      </div>'''

    # Build connections JSON for JS — only cross-stage lineage arrows
    # Fork connections are already rendered as CSS dashed lines within the column
    connections_js = json.dumps([
        {"from": e["from"], "to": e["to"], "type": "parent"} for e in cross_edges
        if e["from"] in nodes and e["to"] in nodes
    ])

    return f'''<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<title>{esc(project_name)} — Run Lineage DAG</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ background: #1a1a1a; color: #f8f8f2; font-family: 'Consolas', 'Fira Code', 'SF Mono', monospace; min-height: 100vh; padding: 32px; }}
  h1 {{ text-align: center; font-size: 20px; font-weight: 500; color: #f8f8f2; margin-bottom: 6px; letter-spacing: 0.5px; }}
  .subtitle {{ text-align: center; font-size: 12px; color: #666; margin-bottom: 36px; }}
  .dag-container {{ display: flex; gap: 0; justify-content: center; align-items: flex-start; position: relative; }}

  .stage-column {{
    flex: 1; max-width: 270px; min-width: 240px;
    border-radius: 12px; padding: 20px 14px;
    display: flex; flex-direction: column; align-items: center; gap: 14px;
    background: #222; border: 1px solid #333; border-top: 2px solid;
    box-shadow: 0 1px 4px rgba(0,0,0,0.3);
  }}
  .stage-header {{
    font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 2px;
    padding: 7px 18px; border-radius: 4px; margin-bottom: 4px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.2); border: 1px solid;
  }}

  .run-node {{
    width: 216px; border-radius: 8px; padding: 14px 16px;
    cursor: default; transition: transform 0.15s, box-shadow 0.15s;
    background: #2d2d2d; border-left: 3px solid;
    box-shadow: 0 2px 8px rgba(0,0,0,0.4), inset 0 1px 0 rgba(255,255,255,0.04);
  }}
  .run-node:hover {{ transform: translateY(-2px); box-shadow: 0 6px 20px rgba(0,0,0,0.5), inset 0 1px 0 rgba(255,255,255,0.04); }}

  .run-id {{ font-size: 13px; font-weight: 700; margin-bottom: 5px; }}
  .run-desc {{ font-size: 11px; color: #b0b0b0; line-height: 1.5; }}
  .run-meta {{ font-size: 10px; color: #777; margin-top: 6px; display: flex; justify-content: space-between; align-items: center; }}
  .run-meta-secondary {{ font-size: 10px; color: #555; margin-top: 2px; }}
  .run-tags {{ margin-top: 6px; }}
  .run-tag {{
    font-size: 9px; padding: 2px 8px; border-radius: 3px; display: inline-block;
    font-weight: 600; letter-spacing: 0.5px;
  }}

  .metric {{ color: #e6db74; }}
  .status-ok {{ font-size: 9px; padding: 2px 8px; border-radius: 3px; background: #333; color: #a6e22e; }}
  .status-other {{ font-size: 9px; padding: 2px 8px; border-radius: 3px; background: #333; color: #b0b0b0; }}

  .fork-arrow {{
    width: 216px; display: flex; align-items: center; justify-content: center;
    position: relative; height: 28px;
  }}
  .fork-arrow::before {{
    content: ''; position: absolute; left: 50%; top: 0; bottom: 0;
    border-left: 2px dashed; opacity: 0.25;
    border-color: var(--accent);
  }}
  .fork-label {{
    font-size: 9px; color: #666; background: #222; padding: 0 8px;
    position: relative; z-index: 1; letter-spacing: 0.5px;
  }}

  svg.arrows {{
    position: absolute; top: 0; left: 0; width: 100%; height: 100%;
    pointer-events: none; overflow: visible;
  }}
  .arrow-line {{ stroke-width: 1.5; fill: none; opacity: 0.3; }}

  .legend {{
    display: flex; gap: 20px; justify-content: center; margin-top: 28px; font-size: 11px; color: #666;
  }}
  .legend-item {{ display: flex; align-items: center; gap: 5px; }}
  .legend-dot {{ width: 8px; height: 8px; border-radius: 2px; }}
  .generated {{ text-align: center; font-size: 10px; color: #444; margin-top: 16px; }}
</style>
</head>
<body>

<h1>{esc(project_name)} — run lineage</h1>
<p class="subtitle">stages left → right · dag top → bottom · dashed = fork</p>

<div class="dag-container" id="dag">
  {columns_html}
  <svg class="arrows" id="arrowsSvg"><defs></defs></svg>
</div>

<div class="legend">
  {"".join(f'<div class="legend-item"><div class="legend-dot" style="background:{STAGE_COLORS.get(s, DEFAULT_COLOR)["accent"]}"></div> {s}</div>' for s in ordered_stages)}
  <div class="legend-item"><span style="border-bottom:2px dashed #66d9ef; width:16px; display:inline-block; opacity:0.4"></span> fork</div>
  <div class="legend-item"><span style="border-bottom:1.5px solid #888; width:16px; display:inline-block; opacity:0.4"></span> lineage</div>
</div>
<p class="generated">generated {datetime.now().strftime("%Y-%m-%d %H:%M")}</p>

<script>
const CONNECTIONS = {connections_js};
const STAGE_COLORS = {json.dumps(STAGE_COLORS)};

function drawArrows() {{
  const svg = document.getElementById('arrowsSvg');
  const container = document.getElementById('dag');
  const rect = container.getBoundingClientRect();

  // Clear old
  svg.querySelectorAll('path.arrow-line').forEach(e => e.remove());
  svg.querySelector('defs').innerHTML = '';

  // Create markers per stage color
  const defs = svg.querySelector('defs');
  for (const [stage, colors] of Object.entries(STAGE_COLORS)) {{
    const marker = document.createElementNS('http://www.w3.org/2000/svg', 'marker');
    marker.setAttribute('id', 'ah-' + stage);
    marker.setAttribute('markerWidth', '6');
    marker.setAttribute('markerHeight', '5');
    marker.setAttribute('refX', '6');
    marker.setAttribute('refY', '2.5');
    marker.setAttribute('orient', 'auto');
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', 'M0,0 L6,2.5 L0,5');
    path.setAttribute('fill', colors.accent + '66');
    marker.appendChild(path);
    defs.appendChild(marker);
  }}

  CONNECTIONS.forEach(({{ from: fromId, to: toId, type }}) => {{
    const fromEl = document.getElementById(fromId);
    const toEl = document.getElementById(toId);
    if (!fromEl || !toEl) return;

    const fr = fromEl.getBoundingClientRect();
    const tr = toEl.getBoundingClientRect();

    const x1 = fr.right - rect.left;
    const y1 = fr.top + fr.height / 2 - rect.top;
    const x2 = tr.left - rect.left;
    const y2 = tr.top + tr.height / 2 - rect.top;
    const cx = (x1 + x2) / 2;

    // Get target stage for color
    const targetStage = toId.split('/')[0];
    const color = (STAGE_COLORS[targetStage] || {{ accent: '#b0b0b0' }}).accent;

    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', `M${{x1}},${{y1}} C${{cx}},${{y1}} ${{cx}},${{y2}} ${{x2}},${{y2}}`);
    path.setAttribute('class', 'arrow-line');
    path.setAttribute('stroke', color);
    path.setAttribute('marker-end', `url(#ah-${{targetStage}})`);
    svg.appendChild(path);
  }});
}}

window.addEventListener('load', () => setTimeout(drawArrows, 100));
window.addEventListener('resize', drawArrows);
</script>

</body></html>'''


def main():
    if len(sys.argv) < 2:
        print("Usage: python build_dag.py <project_root> [--output <path>]")
        sys.exit(1)

    project_root = sys.argv[1]

    # Parse --output
    output_path = os.path.join(project_root, "lineage_dag.html")
    if "--output" in sys.argv:
        idx = sys.argv.index("--output")
        if idx + 1 < len(sys.argv):
            output_path = sys.argv[idx + 1]

    # Load project name
    project_json = os.path.join(project_root, "project.json")
    project_name = os.path.basename(project_root)
    if os.path.isfile(project_json):
        with open(project_json, encoding="utf-8") as f:
            project_name = json.load(f).get("name", project_name)

    # Build
    runs = load_runs(project_root)
    if not runs:
        print(f"No runs found in {project_root}/stages/*/runs/")
        sys.exit(1)

    nodes, cross_edges, fork_edges = build_graph(runs)
    html_content = generate_html(nodes, cross_edges, fork_edges, project_name)

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html_content)

    print(f"DAG written to {output_path} ({len(nodes)} nodes, {len(cross_edges)} lineage edges, {len(fork_edges)} forks)")


if __name__ == "__main__":
    main()
