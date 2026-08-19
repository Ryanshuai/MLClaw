# Config Schemas for Evaluation Stage

## Item schema

Each item in `artifacts.json → items`, `input.json → items`, `input.json → ground_truth → items`, `output.json → items`:

```json
{
  "type": "",
  "format": "",
  "description": "",
  "resource": ""
}
```

- `type`: one of `video|image|text|tabular|json|binary|model|checkpoint|config|log`
- `format`: file extension (e.g., `.onnx`, `.mp4`, `.json`)
- `description`: short text
- `resource`: key in `resources.json (workspace-level)` indicating where this item typically comes from (e.g., `"server_172_31_60_66"`, `"aws"`, or `""` for local/unknown)

## Source schema

Each entry in `artifacts.json → sources`, `input.json → sources`, `input.json → ground_truth → sources`:

```json
{
  "source": "local|s3|server|stage_output|handoff|registry",
  "path": "",
  "credentials": "",
  "origin": null
}
```

- `source`: where the asset is accessed at runtime
- `path`: concrete path on that source (empty = not yet filled)
- `credentials`: key in `resources.json (workspace-level) → servers` or `aws`, etc. Only needed when source is not `local`.
- `origin`: upstream authoritative source this asset was copied/synced from. Same structure (`source`, `path`, `credentials`). Null if this entry IS the authoritative source (e.g., S3 direct).

When filling sources during init, if the user provides a server path that was synced from S3, record the S3 location in `origin`.

### `source: "handoff"` — data that came from outside

`stage_output` is an asset another stage's run produced; `handoff` is its external sibling — an asset a party outside MLClaw produced, taken delivery of through `/data-label`. Two extra fields, and both exist because the facts they carry do not survive anywhere else:

```json
{
  "source": "handoff",
  "handoff_id": "handoff_20260731_025626",
  "path": "/home/shuai/data/coco_labels_batch3",
  "credentials": "",
  "spec_version": "v3",
  "coverage": 0.9424,
  "origin": null
}
```

- **`handoff_id`** — the record holding the party, manifest, rounds, and the reconciliation. Only ever accept a path from a handoff whose `status` is `accepted`; anything else means the data on disk is a partial download of work still in progress.
- **`spec_version`** — the guideline the labels were produced under. Two batches under different specs are different distributions, and merging them silently degrades the model. This is the `preprocessing` mismatch rule (`<mlclaw_root>/references/run-mechanics.md` "Preprocessing contract (cross-stage)") applied to labels instead of pixels: when a stage draws on more than one handoff, diff the spec versions and surface any difference **before** the run, not after a confusing metric three weeks later.
- **`coverage`** — `1.0` unless the batch was accepted partial. A 0.94 batch that reads as complete downstream produces a dataset whose recorded composition is not its real one.

The consuming run also cites `handoffs/<handoff_id>` in `run.json -> lineage.parents`. The source entry says where the data came from; the lineage edge is what turns "did the new batch make it worse" into a DAG walk.

## `input.json` / `artifacts.json` -> `candidates`

**The schema lives in `/train-init` `references/schemas.md` → `candidates`** — entry shape, the full
`location` enum, and the `match` enum including why `unreachable` is not `absent`. Read it there.
It is not restated here on purpose: a second copy of that table is a copy that drifts, which is the
same reasoning that moved the source sweep out of `/train-init` into `/discover` rather than
copying it into this skill.

Locating candidates is `/discover`'s job. What this skill owns is the **`match` judgment**, and
two parts of it have no training equivalent.

### `samples` is part of the metric, not a property of the copy

Mandatory on every candidate, and compared against `config.json -> dataset.num_samples`. **A count
that differs is `mismatch`, never `ok`.**

Training does not need this gate. Train on a subset and the damage is visible in the metrics;
*evaluate* on a subset and the metric is a different number wearing the same name. mAP over 500
images compared against a paper's 5000-image baseline produces a delta made of sampling noise, every
file validates, and nothing errors. This is the same rule as the baseline `scope` discipline in
SKILL.md Step 4, moved one file earlier where it can be prevented instead of caveated.

Consequence worth stating: `dataset:<id>@<snapshot>` is worth more in evaluation than in training,
because it pins the count against a dated census. "The val set" stops meaning whatever is in that
directory today.

### `run:<stage>/<run_id>` — the checkpoint location only evaluation needs

```json
{ "location": "run:training/run_20260801_101500", "path": "output/best.pt", "match": "ok",
  "samples": null, "notes": "best_by val_mAP=0.481; trained on datasets/boxes@v3",
  "evidence": "stages/training/runs/run_20260801_101500/run.json" }
```

Training produces checkpoints; evaluation consumes them, and usually from a run in this same
project. A `.pt` named by a bare path is an anonymous file. The same file cited as
`run:training/<run_id>` reaches that run's `run.json` — the data it trained on, its env, its own
reported metric — which is what makes an evaluation an *edge in the lineage graph* rather than a
number about an unknown model.

**Only `mode: "production"` runs qualify.** A debug run's checkpoint carries a debug run's data
scope, and evaluating it produces a number that is not comparable to anything — the identical rule
SKILL.md Step 4 applies to baselines, and it is the same failure both times.

### Ground truth must pair

Images present with annotations missing is `mismatch`, not `ok`. Check the winning candidate against
`pairing` before writing `ok`: a val directory that satisfies `items` and fails `ground_truth.items`
produces a run that starts, loads, and reports nothing.

## Type classification rules

**Artifacts** (static, per model version):
- model weights: .onnx, .pt, .pth, .engine, .safetensors, .trt, .tflite → type `model`
- checkpoints: .ckpt → type `checkpoint`
- static configs: .yaml, .yml, .toml, .ini → type `config`
- evaluator tools, label maps, decoders → type `json` or `binary`

**Inputs** (dynamic, per run):
- video: .mp4, .avi, .mov, .mkv → type `video`
- images: .jpg, .png, .bmp, .tiff → type `image`
- text: .txt, .jsonl → type `text`
- tabular: .csv, .tsv, .parquet → type `tabular`

**Ground truth**:
- COCO annotations: .json → type `json`
- YOLO labels: .txt → type `text`
- VOC annotations: .xml → type `text`
- CSV labels: .csv → type `tabular`
- Embedded: .hdf5, .tfrecord → type `binary`

## Variable reference syntax `${}`

See CLAUDE.md → Conventions → Variable Reference Syntax for the full reference table. Use `${}` to reference values across config files. Resolved at runtime by `/eval-run`.
