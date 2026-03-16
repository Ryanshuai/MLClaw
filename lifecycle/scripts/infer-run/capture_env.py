"""Capture ML environment snapshot. Outputs JSON to stdout.

Usage:
    python capture_env.py                    # use default ML package list
    python capture_env.py pkg1,pkg2,pkg3     # use custom package list
"""
import subprocess
import json
import os
import sys
import platform

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
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else None
    except FileNotFoundError:
        return None
    except Exception:
        return None


def get_pip_packages(package_list):
    output = run_cmd([sys.executable, "-m", "pip", "freeze"])
    if not output:
        return {pkg: None for pkg in package_list}
    installed = {}
    for line in output.splitlines():
        if "==" in line:
            name, ver = line.split("==", 1)
            installed[name.lower().strip()] = ver.strip()
    return {pkg: installed.get(pkg.lower()) for pkg in package_list}


def get_gpu_info():
    info = {"nvidia_driver": None, "gpu": None, "gpu_count": 0}
    output = run_cmd(["nvidia-smi", "--query-gpu=name,driver_version,count", "--format=csv,noheader,nounits"])
    if output:
        parts = output.splitlines()[0].split(", ")
        if len(parts) >= 2:
            info["gpu"] = parts[0].strip()
            info["nvidia_driver"] = parts[1].strip()
        info["gpu_count"] = len(output.splitlines())
    return info


def get_cuda_version():
    output = run_cmd(["nvcc", "--version"])
    if output:
        for line in output.splitlines():
            if "release" in line.lower():
                return line.split("release")[-1].strip().split(",")[0]
    return None


def get_cudnn_version():
    try:
        import torch
        if hasattr(torch.backends, 'cudnn') and torch.backends.cudnn.is_available():
            v = torch.backends.cudnn.version()
            return f"{v // 1000}.{(v % 1000) // 100}.{v % 100}"
    except Exception:
        pass
    for path in ["/usr/include/cudnn_version.h", "/usr/local/cuda/include/cudnn_version.h"]:
        try:
            import re
            with open(path) as f:
                content = f.read()
            major = re.search(r'CUDNN_MAJOR\s+(\d+)', content)
            minor = re.search(r'CUDNN_MINOR\s+(\d+)', content)
            patch = re.search(r'CUDNN_PATCHLEVEL\s+(\d+)', content)
            if major and minor and patch:
                return f"{major.group(1)}.{minor.group(1)}.{patch.group(1)}"
        except Exception:
            pass
    return None


def main():
    package_list = DEFAULT_ML_PACKAGES
    if len(sys.argv) > 1:
        package_list = [p.strip() for p in sys.argv[1].split(",") if p.strip()]

    gpu = get_gpu_info()
    env = {
        "python": platform.python_version(),
        "nvidia_driver": gpu["nvidia_driver"],
        "cuda": get_cuda_version(),
        "cudnn": get_cudnn_version(),
        "gpu": gpu["gpu"],
        "gpu_count": gpu["gpu_count"],
        "os": f"{platform.system()} {platform.release()}",
        "packages": get_pip_packages(package_list),
    }
    json.dump(env, sys.stdout, indent=2)


if __name__ == "__main__":
    main()
