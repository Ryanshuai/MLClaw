"""Create project directory structure, copy templates, git init."""
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime

GITIGNORE = """\
# Large files — model weights, checkpoints
*.onnx
*.pt
*.pth
*.engine
*.trt
*.tflite
*.safetensors
*.ckpt
*.bin

# Data files
*.mp4
*.avi
*.mov
*.mkv
*.jpg
*.jpeg
*.png
*.bmp
*.tiff
*.csv
*.parquet

# Run outputs, artifacts, and data
stages/*/runs/
stages/*/artifacts/
stages/*/data/

# Secrets — NEVER commit
secrets.json

# OS / IDE
.DS_Store
Thumbs.db
__pycache__/
*.pyc
.vscode/
.idea/
"""


def main():
    if len(sys.argv) < 3:
        print("Usage: python init_project.py <project_json_str> <mlclaw_root>")
        print("  project_json_str: JSON string with project config (name, root, stages, etc.)")
        print("  mlclaw_root: path to MLClaw repo (for templates)")
        sys.exit(1)

    project = json.loads(sys.argv[1])
    mlclaw_root = sys.argv[2]
    lifecycle = os.path.join(mlclaw_root, "lifecycle")

    root = project["root"]
    os.makedirs(root, exist_ok=True)

    # Copy project-level templates
    for fname in ["secrets.json", "history.json", "runs_index.json"]:
        src = os.path.join(lifecycle, fname)
        dst = os.path.join(root, fname)
        if os.path.isfile(src) and not os.path.isfile(dst):
            shutil.copy2(src, dst)

    # Write project.json
    project["created"] = datetime.now().isoformat()
    with open(os.path.join(root, "project.json"), "w") as f:
        json.dump(project, f, indent=2)

    # Create stage directories and copy templates
    for stage, cfg in project.get("stages", {}).items():
        if not cfg.get("enabled"):
            continue

        stage_dir = os.path.join(root, "stages", stage)
        os.makedirs(os.path.join(stage_dir, "code"), exist_ok=True)
        os.makedirs(os.path.join(stage_dir, "runs"), exist_ok=True)
        os.makedirs(os.path.join(stage_dir, "artifacts"), exist_ok=True)
        os.makedirs(os.path.join(stage_dir, "data"), exist_ok=True)

        # Copy 4 JSON templates (use stage-specific if exists, else inference fallback)
        template_dir = os.path.join(lifecycle, stage)
        fallback_dir = os.path.join(lifecycle, "inference")
        if not os.path.isdir(template_dir):
            if os.path.isdir(fallback_dir):
                template_dir = fallback_dir
            else:
                print(json.dumps({"warning": f"No templates found for stage '{stage}', skipping template copy"}), file=sys.stderr)
                continue

        for jf in ["artifacts.json", "config.json", "input.json", "output.json"]:
            src = os.path.join(template_dir, jf)
            dst = os.path.join(stage_dir, jf)
            if os.path.isfile(src) and not os.path.isfile(dst):
                shutil.copy2(src, dst)

    # Write .gitignore
    with open(os.path.join(root, ".gitignore"), "w") as f:
        f.write(GITIGNORE)

    # Git init + initial commit (skip if git not available)
    try:
        subprocess.run(["git", "init"], cwd=root, capture_output=True, check=True)
        subprocess.run(["git", "add", "project.json", ".gitignore", "history.json", "runs_index.json"], cwd=root, capture_output=True)
        for stage, cfg in project.get("stages", {}).items():
            if cfg.get("enabled"):
                for jf in ["artifacts.json", "config.json", "input.json", "output.json"]:
                    path = os.path.join("stages", stage, jf)
                    subprocess.run(["git", "add", path], cwd=root, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Initial project setup"], cwd=root, capture_output=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        print(json.dumps({"warning": "git not available, skipping git init"}), file=sys.stderr)

    print(json.dumps({"status": "ok", "root": root}))


if __name__ == "__main__":
    main()
