# Config Schemas for Inference Stage

## Item schema

Each item in `artifacts.json -> items`, `input.json -> items`, `output.json -> items`:

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

Each entry in `artifacts.json -> sources`, `input.json -> sources`:

```json
{
  "source": "local|s3|server|stage_output|registry",
  "path": "",
  "credentials": "",
  "origin": null
}
```

- `source`: where the asset is accessed at runtime
- `path`: concrete path on that source (empty = not yet filled)
- `credentials`: key in `resources.json (workspace-level) -> servers` or `aws`, etc. Only needed when source is not `local`.
- `origin`: upstream authoritative source this asset was copied/synced from. Same structure (`source`, `path`, `credentials`). Null if this entry IS the authoritative source (e.g., S3 direct).

When filling sources during init, if the user provides a server path that was synced from S3, record the S3 location in `origin`.

## Type classification rules

**Artifacts** (static, per model version):
- model weights: .onnx, .pt, .pth, .engine, .safetensors, .trt, .tflite -> type `model`
- checkpoints: .ckpt -> type `checkpoint`
- static configs: .yaml, .yml, .toml, .ini -> type `config`
- lookup tables, decoders, label maps -> type `json` or `binary`

**Inputs** (dynamic, per run):
- video: .mp4, .avi, .mov, .mkv -> type `video`
- images: .jpg, .png, .bmp, .tiff -> type `image`
- text: .txt, .jsonl -> type `text`
- tabular: .csv, .tsv, .parquet -> type `tabular`

## Variable reference syntax `${}`

See CLAUDE.md -> Conventions -> Variable Reference Syntax for the full reference table. Use `${}` to reference values across config files. Resolved at runtime by `/infer-run`.
