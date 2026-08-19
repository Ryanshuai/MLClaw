"""Capture ML environment snapshot. Outputs JSON to stdout.

Usage:
    python capture_env.py                    # use default ML package list
    python capture_env.py pkg1,pkg2,pkg3     # use custom package list

THE ARGUMENT IS A PACKAGE LIST, NOT A RUN DIR, and this file refuses one that
looks like a path rather than accepting it. `/train-run` Step 2 documented the
call as `capture_env.py <RUN_DIR>` for as long as that step has existed, next
to `code_snapshot.py <code_dir> <RUN_DIR>` and `check_deps.py <..> <RUN_DIR>`,
which do take one. Nothing raised: the run directory became the sole entry of
`package_list`, so the record read

    "packages": {"/…/stages/training/runs/run_007": null}

and every ML package version went unrecorded. Downstream that is not a visible
gap -- `/repro`'s env axis finds nothing to compare and returns `unverifiable`,
which is also what it returns for a run that predates env capture. A run whose
environment was silently not captured and one that never had it recorded are
the same fact to every reader.

This script writes to STDOUT and to nothing else, like `code_snapshot.py`; the
caller merges the JSON into `run.json -> env`.
"""
import subprocess
import json
import os
import sys
import platform

from _records import broke  # same directory; noqa: E402

DEFAULT_ML_PACKAGES = [
    "numpy", "pandas", "scipy", "scikit-learn", "pillow", "matplotlib",
    "torch", "torchvision", "torchaudio", "tensorflow", "keras", "jax", "jaxlib",
    "opencv-python", "albumentations", "ultralytics", "detectron2",
    "mmcv", "mmdet", "timm", "kornia",
    "transformers", "tokenizers", "datasets", "accelerate", "peft",
    "bitsandbytes", "vllm", "langchain", "openai", "anthropic", "sentencepiece",
    "librosa", "soundfile", "whisper",
    "onnx", "onnxruntime", "tensorrt", "openvino", "triton",
    "xgboost", "lightgbm", "catboost",
    "ray", "deepspeed", "horovod", "wandb", "mlflow", "tensorboard", "optuna",
    "huggingface-hub", "safetensors", "einops", "flash-attn",
]


def run_cmd(cmd):
    """-> (output, why_not). `why_not` is None when the command answered.

    CLAUDE.md "Never silently": *Never report data you could not look at.* A
    machine that did not answer, a path that is not there, and a directory that
    is genuinely empty are three facts, and only the last means the data is
    gone. This used to return a bare `None` for all of them, so `nvidia-smi`
    timing out and a box with no GPU produced the same record -- and the record
    is what `/repro` compares a re-run against.
    """
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=10)
    except FileNotFoundError:
        return None, "not installed"        # a fact: the tool is not on this box
    except subprocess.TimeoutExpired:
        return None, "timed out"            # NOT a fact about the machine
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if r.returncode != 0:
        return None, f"exit {r.returncode}"
    return r.stdout.strip(), None


def get_pip_packages(package_list):
    output, why = run_cmd([sys.executable, "-X", "utf8", "-m", "pip", "freeze"])
    if not output:
        return {pkg: None for pkg in package_list}, why or "pip freeze printed nothing"
    installed = {}
    for line in output.splitlines():
        if "==" in line:
            name, ver = line.split("==", 1)
            installed[name.lower().strip()] = ver.strip()
    return {pkg: installed.get(pkg.lower()) for pkg in package_list}, None


def get_gpu_info():
    gpu_info = {"nvidia_driver": None, "gpu": None, "gpu_count": 0}
    output, why = run_cmd(["nvidia-smi", "--query-gpu=name,driver_version,count", "--format=csv,noheader,nounits"])
    if output:
        parts = output.splitlines()[0].split(", ")
        if len(parts) >= 2:
            gpu_info["gpu"] = parts[0].strip()
            gpu_info["nvidia_driver"] = parts[1].strip()
        gpu_info["gpu_count"] = len(output.splitlines())
    # `not installed` is the honest read of "this box has no NVIDIA GPU"; a
    # timeout or a non-zero exit is not, and must not leave gpu_count 0 looking
    # like a measurement.
    return gpu_info, (None if why in (None, "not installed") else why)


def get_cuda_version():
    output, why = run_cmd(["nvcc", "--version"])
    if output:
        for line in output.splitlines():
            if "release" in line.lower():
                return line.split("release")[-1].strip().split(",")[0], None
        return None, "nvcc --version printed no release line"
    return None, (None if why == "not installed" else why)


def get_cudnn_version():
    """-> (version, why_not). Two probes, and only the second can say `absent`.

    torch failing to import is not evidence about cuDNN, so it does not become
    the answer -- it falls through to the headers, and only if THOSE are absent
    too is the answer a genuine None. `except Exception` stays deliberately
    broad on the torch branch: an installed-but-broken torch (a missing
    libcuda, a driver mismatch) raises OSError or RuntimeError as readily as
    ImportError, and none of them is a reason for the whole env capture to die.
    What changed is that the reason is now carried out rather than dropped.
    """
    torch_why = None
    try:
        import torch
        if hasattr(torch.backends, 'cudnn') and torch.backends.cudnn.is_available():
            v = torch.backends.cudnn.version()
            return f"{v // 1000}.{(v % 1000) // 100}.{v % 100}", None
    except ImportError:
        torch_why = None                     # a fact: torch is not installed here
    except Exception as exc:                 # noqa: BLE001 -- see the docstring
        torch_why = f"torch: {type(exc).__name__}: {exc}"
    header_why = []
    for path in ["/usr/include/cudnn_version.h", "/usr/local/cuda/include/cudnn_version.h"]:
        try:
            import re
            with open(path, encoding="utf-8") as f:
                content = f.read()
        except FileNotFoundError:
            continue                         # a fact: no cuDNN header at that path
        except (OSError, UnicodeDecodeError) as exc:
            header_why.append(f"{path}: {type(exc).__name__}")
            continue
        major = re.search(r'CUDNN_MAJOR\s+(\d+)', content)
        minor = re.search(r'CUDNN_MINOR\s+(\d+)', content)
        patch = re.search(r'CUDNN_PATCHLEVEL\s+(\d+)', content)
        if major and minor and patch:
            return f"{major.group(1)}.{minor.group(1)}.{patch.group(1)}", None
    why = [w for w in [torch_why, *header_why] if w]
    return None, "; ".join(why) or None


def main():
    package_list = DEFAULT_ML_PACKAGES
    if len(sys.argv) > 1:
        raw = sys.argv[1]
        seps = [c for c in (os.sep, os.altsep) if c]
        if any(c in raw for c in seps) or os.path.isdir(raw):
            broke(f"the argument is a comma-separated PACKAGE LIST, not a path: {raw!r}",
                  fix="drop it -- `python capture_env.py` captures the default ML package "
                      "set and prints JSON on stdout; merge that into `run.json -> env`. "
                      "Its neighbours in /train-run Step 2 (`code_snapshot.py`, "
                      "`check_deps.py`) do take a run dir; this one never has.")
        package_list = [p.strip() for p in raw.split(",") if p.strip()]

    gpu, gpu_why = get_gpu_info()
    cuda, cuda_why = get_cuda_version()
    cudnn, cudnn_why = get_cudnn_version()
    packages, pkg_why = get_pip_packages(package_list)

    # Which fields nothing could READ, as opposed to read as absent. Always
    # emitted, including empty: a record with no `unreadable` key at all is one
    # written before this existed, and that is a third state, not a clean one.
    unreadable = {}
    for field, why in (("nvidia_driver", gpu_why), ("gpu", gpu_why),
                       ("gpu_count", gpu_why), ("cuda", cuda_why),
                       ("cudnn", cudnn_why), ("packages", pkg_why)):
        if why:
            unreadable[field] = why

    env = {
        "python": platform.python_version(),
        "nvidia_driver": gpu["nvidia_driver"],
        "cuda": cuda,
        "cudnn": cudnn,
        "gpu": gpu["gpu"],
        "gpu_count": gpu["gpu_count"],
        "os": f"{platform.system()} {platform.release()}",
        "packages": packages,
        "unreadable": unreadable,
    }
    json.dump(env, sys.stdout, indent=2)


if __name__ == "__main__":
    main()
