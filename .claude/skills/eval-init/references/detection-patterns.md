# Code Detection Patterns

Reference for recognizing evaluation-related patterns in user code. Read this when analyzing code in Step 1.

## Evaluation entry point patterns

- Standalone eval scripts: `eval.py`, `evaluate.py`, `test.py`, `validate.py`, `benchmark.py`
- Eval flags/modes in training scripts: `--eval`, `--evaluate`, `--test`, `--val-only`, `--mode eval`
- Eval functions: `def evaluate(`, `def validate(`, `def test(`
- Conditional blocks: `if args.eval:`, `if mode == "eval":`

When eval code is mixed with training code, focus only on the evaluation path — what arguments control eval mode, what data it reads, what metrics it computes.

## Dataset detection patterns

To fill `config.json → dataset`:

- **name**: `CocoDetection`, `ImageFolder`, `load_dataset("imagenet")`, path patterns like `coco/val2017`, `VOC2012/test`
- **split**: `--split`, `val`, `test`, `val2017`, `testdev` in args or paths
- **num_samples**: dataset length prints, `len(dataset)`, known benchmark sizes
- **classes**: class lists, label maps, `num_classes` args

## Ground truth detection patterns

To fill `input.json → ground_truth`:

| Format | Patterns |
|--------|----------|
| COCO-style | `COCO(annotation_file)`, `pycocotools`, `--ann-file`, `instances_val2017.json` |
| YOLO-style | label `.txt` files in parallel directory structure |
| VOC-style | XML annotation files |
| Generic | `--gt`, `--ground-truth`, `--annotations`, `--labels`, `--target` |
| Embedded | HDF5 datasets, TFRecord with labels, CSV with target column |

**Pairing mode** for `input.json → pairing`:
- `"single_file"` — one annotation file covers all inputs (COCO JSON, CSV manifest)
- `"directory"` — per-input annotation files in parallel directory (YOLO .txt, VOC .xml)
- `"embedded"` — GT embedded in input files (HDF5, TFRecord)
- `"index"` — separate index/manifest maps inputs to GT entries

## Metrics detection patterns

Scan code for these to populate `output.json → metrics.definitions`:

- **Evaluation APIs**: `COCOeval`, `evaluate()`, `compute_metrics()`, `classification_report`, `confusion_matrix`, `MeanAveragePrecision`, `MultiClassMetrics`
- **Common metric names**: accuracy, precision, recall, f1, f1_score, mAP, AP, AP50, AP75, mAP_small, mAP_medium, mAP_large, BLEU, ROUGE, FID, IS, PSNR, SSIM, WER, CER, IoU, mIoU, dice, AUC, top1, top5, perplexity, loss
- **Per-class output**: `per_category_ap`, `class_results`, loops printing per-class metrics, `ap_per_class`, `map_per_class`
- **Print/logging patterns**: `print(f"mAP: {mAP}")`, `logger.info(f"accuracy: {acc}")`, `results["mAP"]`
- **PL logger patterns**: `self.log_dict(metrics, ...)`, `self.logger.add_figure(...)` — metrics logged to TensorBoard/MLflow
- **Result file writes**: `json.dump(results, ...)`, `to_csv()`, `save_results()`, `pickle.dump(...)`

Each metric gets an entry in `output.json → metrics.definitions`:
```json
{
  "type": "float|int",
  "source": "stdout|file|tensorboard",
  "pattern": "regex to extract from stdout (if source=stdout)",
  "path": "relative path to result file (if source=file)",
  "key": "JSON key in result file (if source=file)"
}
```

Note: `source: "tensorboard"` is for metrics logged via PyTorch Lightning `self.log_dict()`. These appear in TensorBoard event files and also in PL's stdout summary table.
