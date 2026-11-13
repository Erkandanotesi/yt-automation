import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from modelscope import snapshot_download

import sys as _sys

_automation_root = Path(__file__).resolve().parents[1]
if str(_automation_root / "src") not in _sys.path:
    _sys.path.insert(0, str(_automation_root / "src"))
from file_locations import MODELS_ROOT

target_dir = MODELS_ROOT / "Qwen3-TTS-12Hz-1.7B-CustomVoice"
target_dir.parent.mkdir(parents=True, exist_ok=True)

print(f"Starting download of Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice to: {target_dir}")
model_dir = snapshot_download(
    "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice",
    local_dir=str(target_dir),
)
print(f"Model downloaded successfully to: {model_dir}")
