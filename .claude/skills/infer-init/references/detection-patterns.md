# Code Detection Patterns

Reference for recognizing inference-related patterns in user code. Read this when analyzing code in Step 1.

## Inference entry point patterns

- Standalone inference scripts: `infer.py`, `inference.py`, `predict.py`, `detect.py`, `demo.py`, `run.py`
- Inference flags/modes: `--infer`, `--predict`, `--mode infer`, `--mode predict`, `--demo`
- Inference functions: `def infer(`, `def predict(`, `def detect(`, `def forward(`
- Pipeline classes: `class Pipeline`, `class Predictor`, `class Detector`, `class Inferencer`
- Main guard patterns: `if __name__ == "__main__":` with model loading + data processing loop

When inference code is mixed with training/eval code, focus only on the inference path — what arguments control inference mode, what data it reads, what outputs it produces.

## Input detection patterns

To fill `input.json -> items`:

- **Image inputs**: `cv2.imread`, `Image.open`, `--image`, `--input`, `--source`, glob patterns like `*.jpg`, `*.png`
- **Video inputs**: `cv2.VideoCapture`, `--video`, `--source` with `.mp4`/`.avi`, streaming URLs
- **Directory inputs**: `os.listdir`, `glob.glob`, `--input-dir`, `--data-dir`, `Path(...).iterdir()`
- **Batch inputs**: `DataLoader`, `Dataset`, `--batch-size` combined with file loading
- **Text/JSON inputs**: `json.load`, `open(...).readlines()`, `--input-file`, `--manifest`

## Output detection patterns

To fill `output.json -> items`:

- **File outputs**: `cv2.imwrite`, `json.dump`, `to_csv()`, `save()`, `--output`, `--output-dir`, `--save-path`
- **Visualization**: `cv2.rectangle`, `draw_bbox`, `plt.savefig`, overlay/annotation code
- **Structured results**: detection dicts with `boxes`/`scores`/`labels`, classification logits, segmentation masks
- **Streaming output**: `print()` to stdout in structured format, MQTT publish, API response

## Metrics detection patterns

Scan code for these to populate `output.json -> metrics.definitions`:

- **Performance metrics**: `time.time()`, `time.perf_counter()`, FPS calculation, latency measurement, `torch.cuda.Event` for GPU timing
- **Throughput metrics**: frames processed, images per second, batch throughput, total inference time
- **Model metrics**: confidence scores (mean/min/max), detection counts, number of objects per frame
- **Resource metrics**: GPU memory usage (`torch.cuda.memory_allocated`), CPU usage, peak memory
- **Print/logging patterns**: `print(f"FPS: {fps}")`, `logger.info(f"latency: {lat}ms")`, `print(f"Processed {n} images")`
- **Result file writes**: `json.dump(results, ...)`, `to_csv()`, `save_results()`

Each metric gets an entry in `output.json -> metrics.definitions`:
```json
{
  "type": "float|int",
  "source": "stdout|file",
  "pattern": "regex to extract from stdout (if source=stdout)",
  "path": "relative path to result file (if source=file)",
  "key": "JSON key in result file (if source=file)"
}
```

Note: inference metrics are typically performance-oriented (FPS, latency, throughput) rather than accuracy-oriented. Accuracy metrics belong in the evaluation stage.
