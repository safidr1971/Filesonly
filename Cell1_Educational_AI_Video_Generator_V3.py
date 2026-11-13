
# =====================================================================
# CELL 1 — UNIFIED AI VIDEO GENERATOR STARTUP V3
#
# GPU 0: Ollama / Gemma 4 E4B
# GPU 1: DreamShaper 8 FP16
#
# ONE persistent inline progress bar for the complete startup.
# Preserves existing assets, models and generated videos.
#
# Includes:
# - Minimal dependency installation
# - Click compatibility repair
# - Ollama startup/reuse
# - Ollama model pull with real download progress
# - Streamed Gemma inference validation
# - Hugging Face authenticated model download
# - DreamShaper loading and GPU validation
# =====================================================================

import os
import sys
import re
import json
import time
import shutil
import subprocess
import threading
import importlib.util
import importlib.metadata as metadata
import requests

from pathlib import Path
from datetime import datetime
from tqdm.auto import tqdm

START_TIME = time.time()
INSTALL_BAR = None

# ---------------------------------------------------------------------
# 1. LOGGING AND SINGLE PROGRESS BAR
# ---------------------------------------------------------------------

def log(message):
    elapsed = time.time() - START_TIME
    text = f"[{elapsed:7.1f}s] {message}"

    if INSTALL_BAR is not None:
        tqdm.write(text)
    else:
        print(text, flush=True)


def advance_progress(target, message):
    global INSTALL_BAR

    target = max(INSTALL_BAR.n, min(1000, int(target)))
    amount = target - INSTALL_BAR.n

    INSTALL_BAR.set_postfix_str(message, refresh=True)

    if amount > 0:
        INSTALL_BAR.update(amount)


def set_download_progress(start, end, fraction, message):
    fraction = max(0.0, min(1.0, float(fraction)))
    target = start + int((end - start) * fraction)
    advance_progress(target, message)


print("=" * 75)
print("UNIFIED AI VIDEO GENERATOR — STARTUP V3")
print("Started:", datetime.now().astimezone().isoformat())
print("=" * 75)

INSTALL_BAR = tqdm(
    total=1000,
    initial=0,
    desc="AI VIDEO STARTUP",
    unit="units",
    dynamic_ncols=True,
    leave=True,
    bar_format="{desc}: {percentage:3.0f}%|{bar}| {postfix}"
)

# ---------------------------------------------------------------------
# 2. DIRECTORIES — PRESERVE ALL EXISTING USER FILES
# ---------------------------------------------------------------------

ASSET_DIR = "/kaggle/working/assets"
VIDEO_OUTPUT_DIR = "/kaggle/working/video_outputs"
TEMP_DIR = "/kaggle/working/video_temp"

for directory in [ASSET_DIR, VIDEO_OUTPUT_DIR, TEMP_DIR]:
    os.makedirs(directory, exist_ok=True)

log("Directories ready. Existing assets and videos preserved.")
advance_progress(20, "Directories ready")

# ---------------------------------------------------------------------
# 3. KAGGLE SECRETS
# ---------------------------------------------------------------------

from kaggle_secrets import UserSecretsClient

user_secrets = UserSecretsClient()

HF_TOKEN = user_secrets.get_secret("HF_TOKEN")

if not HF_TOKEN:
    raise RuntimeError("Kaggle Secret HF_TOKEN is missing or empty.")

os.environ["HF_TOKEN"] = HF_TOKEN

try:
    NGROK_AUTHTOKEN = user_secrets.get_secret("NGROK_AUTHTOKEN")
except Exception:
    NGROK_AUTHTOKEN = None

log("Hugging Face authentication configured.")
advance_progress(40, "Secrets configured")

# ---------------------------------------------------------------------
# 4. PYTHON DEPENDENCIES — INSTALL ONLY WHAT IS MISSING
# ---------------------------------------------------------------------

package_map = {
    "pydantic": "pydantic",
    "ollama": "ollama",
    "gtts": "gTTS",
    "fastapi": "fastapi",
    "uvicorn": "uvicorn",
    "pyngrok": "pyngrok",
    "requests": "requests",
    "nest_asyncio": "nest_asyncio",
    "diffusers": "diffusers",
    "transformers": "transformers",
    "accelerate": "accelerate",
    "safetensors": "safetensors",
    "huggingface_hub": "huggingface_hub",
    "PIL": "Pillow",
    "numpy": "numpy",
}

missing_packages = [
    package_map[module]
    for module in package_map
    if importlib.util.find_spec(module) is None
]

# Check Click without unnecessarily reinstalling it.
click_needs_repair = False

try:
    click_version = metadata.version("click")
    version_numbers = tuple(
        int(x) for x in re.findall(r"\d+", click_version)[:3]
    )
    click_needs_repair = version_numbers < (8, 4, 2)
except metadata.PackageNotFoundError:
    click_needs_repair = True

pip_packages = list(missing_packages)

if click_needs_repair:
    pip_packages.append("click>=8.4.2,<9")

if pip_packages:
    log("Installing: " + ", ".join(pip_packages))

    install_result = subprocess.run(
        [
            sys.executable, "-m", "pip", "install",
            "-q",
            "--disable-pip-version-check",
            *pip_packages
        ],
        capture_output=True,
        text=True
    )

    if install_result.returncode != 0:
        print(install_result.stdout[-6000:])
        print(install_result.stderr[-6000:])
        raise RuntimeError("Python dependency installation failed.")

    log("Python dependency installation completed.")
else:
    log("Python dependencies already installed and Click is compatible.")

advance_progress(120, "Python dependencies ready")

# Verify critical packages without modifying the environment.
for module in package_map:
    if importlib.util.find_spec(module) is None:
        raise RuntimeError(f"Required Python module is missing: {module}")

try:
    click_version = metadata.version("click")
    log(f"Click version: {click_version}")
except Exception:
    pass

# ---------------------------------------------------------------------
# 5. SYSTEM DEPENDENCIES — NO UNNECESSARY APT UPDATE
# ---------------------------------------------------------------------

missing_system = []

for executable, package in [
    ("ffmpeg", "ffmpeg"),
    ("ffprobe", "ffmpeg"),
    ("zstd", "zstd"),
    ("curl", "curl"),
]:
    if shutil.which(executable) is None and package not in missing_system:
        missing_system.append(package)

if missing_system:
    log("Installing system packages: " + ", ".join(missing_system))

    apt_result = subprocess.run(
        ["apt-get", "install", "-y", "-qq", *missing_system],
        capture_output=True,
        text=True
    )

    if apt_result.returncode != 0:
        print(apt_result.stdout[-5000:])
        print(apt_result.stderr[-5000:])
        raise RuntimeError("System package installation failed.")
else:
    log("FFmpeg, ffprobe, zstd and curl are already available.")

for executable in ["ffmpeg", "ffprobe"]:
    if shutil.which(executable) is None:
        raise RuntimeError(f"Required executable is missing: {executable}")

advance_progress(170, "System dependencies ready")

# ---------------------------------------------------------------------
# 6. OLLAMA INSTALLATION
# ---------------------------------------------------------------------

if shutil.which("ollama") is None:
    log("Installing Ollama...")

    result = subprocess.run(
        [
            "bash", "-o", "pipefail", "-c",
            "curl -fsSL https://ollama.com/install.sh | sh"
        ],
        capture_output=True,
        text=True
    )

    if result.returncode != 0:
        print(result.stdout[-5000:])
        print(result.stderr[-5000:])
        raise RuntimeError("Ollama installation failed.")

    if shutil.which("ollama") is None:
        raise RuntimeError("Ollama installation did not create its executable.")

    log("Ollama installed.")
else:
    log("Ollama already installed. Reusing executable.")

advance_progress(220, "Ollama installed")

# ---------------------------------------------------------------------
# 7. START OR REUSE OLLAMA
# ---------------------------------------------------------------------

OLLAMA_URL = "http://127.0.0.1:11434"
OLLAMA_MODEL = "gemma4:e4b"

def ollama_is_online():
    try:
        response = requests.get(
            f"{OLLAMA_URL}/api/version",
            timeout=3
        )
        return response.status_code == 200
    except requests.RequestException:
        return False


OLLAMA_PROCESS = globals().get("OLLAMA_PROCESS")
OLLAMA_LOG_PATH = "/kaggle/working/ollama_server.log"

if not ollama_is_online():
    log("Starting Ollama server...")

    OLLAMA_LOG_FILE = open(
        OLLAMA_LOG_PATH,
        "a",
        buffering=1
    )

    OLLAMA_PROCESS = subprocess.Popen(
        ["ollama", "serve"],
        stdout=OLLAMA_LOG_FILE,
        stderr=subprocess.STDOUT
    )

    deadline = time.time() + 90

    while time.time() < deadline:
        if ollama_is_online():
            break

        if OLLAMA_PROCESS.poll() is not None:
            raise RuntimeError(
                f"Ollama exited unexpectedly. Check {OLLAMA_LOG_PATH}"
            )

        time.sleep(2)

    if not ollama_is_online():
        raise RuntimeError(
            f"Ollama did not start within 90 seconds. Check {OLLAMA_LOG_PATH}"
        )
else:
    log("Existing Ollama server detected. Reusing it.")

log("Ollama API is responding.")
advance_progress(250, "Ollama server ready")

# ---------------------------------------------------------------------
# 8. GEMMA MODEL — REAL DOWNLOAD PROGRESS
# ---------------------------------------------------------------------

tags_response = requests.get(
    f"{OLLAMA_URL}/api/tags",
    timeout=15
)
tags_response.raise_for_status()

installed_models = [
    item.get("name", "")
    for item in tags_response.json().get("models", [])
]

model_installed = any(
    name == OLLAMA_MODEL
    for name in installed_models
)

if not model_installed:
    log(f"Downloading {OLLAMA_MODEL} through Ollama API...")

    response = requests.post(
        f"{OLLAMA_URL}/api/pull",
        json={
            "name": OLLAMA_MODEL,
            "stream": True
        },
        stream=True,
        timeout=(15, 300)
    )

    response.raise_for_status()

    last_status = ""

    for line in response.iter_lines():
        if not line:
            continue

        item = json.loads(line)

        if item.get("error"):
            raise RuntimeError(
                "Ollama model pull failed: " + str(item["error"])
            )

        status = item.get("status", "")

        if status and status != last_status:
            last_status = status

        total = item.get("total")
        completed = item.get("completed")

        if total and completed is not None:
            fraction = completed / total

            set_download_progress(
                250,
                450,
                fraction,
                f"Gemma download: {fraction*100:.1f}%"
            )
        else:
            INSTALL_BAR.set_postfix_str(
                status or "Downloading Gemma",
                refresh=True
            )

    # Confirm the model actually exists after pull.
    tags_response = requests.get(
        f"{OLLAMA_URL}/api/tags",
        timeout=20
    )
    tags_response.raise_for_status()

    installed_models = [
        item.get("name", "")
        for item in tags_response.json().get("models", [])
    ]

    if OLLAMA_MODEL not in installed_models:
        raise RuntimeError(
            f"Gemma pull finished but {OLLAMA_MODEL} was not found."
        )

    log("Gemma model download completed and verified.")
else:
    log(f"{OLLAMA_MODEL} already installed. Download skipped.")

advance_progress(450, "Gemma model available")

# ---------------------------------------------------------------------
# 9. REAL STREAMED GEMMA INFERENCE TEST
# ---------------------------------------------------------------------

log("Testing Gemma inference with streamed output...")

payload = {
    "model": OLLAMA_MODEL,
    "prompt": "Reply with exactly: GEMMA_CELL1_OK",
    "stream": True,
    "think": False,
    "keep_alive": "10m",
    "options": {
        "temperature": 0,
        "num_predict": 64
    }
}

gemma_parts = []
gemma_completed = False
gemma_start = time.time()

try:
    with requests.post(
        f"{OLLAMA_URL}/api/generate",
        json=payload,
        stream=True,
        timeout=(15, 300)
    ) as response:

        response.raise_for_status()

        for line in response.iter_lines():
            if not line:
                continue

            item = json.loads(line)

            if item.get("error"):
                raise RuntimeError(str(item["error"]))

            chunk = item.get("response", "")
            if chunk:
                gemma_parts.append(chunk)

            if item.get("done"):
                gemma_completed = True
                break

except requests.exceptions.ReadTimeout as error:
    raise RuntimeError(
        "Gemma inference exceeded the 300-second read timeout. "
        "The Ollama process may still be running. "
        f"Check {OLLAMA_LOG_PATH}. Original error: {error}"
    )

gemma_text = "".join(gemma_parts).strip()

print("Gemma response:", repr(gemma_text))
log(f"Gemma inference elapsed: {time.time()-gemma_start:.1f}s")

if not gemma_completed:
    raise RuntimeError("Gemma response did not reach its final completion marker.")

if "GEMMA_CELL1_OK" not in gemma_text:
    raise RuntimeError(
        "Gemma inference validation failed. "
        f"Received: {gemma_text[:500]!r}"
    )

log("Gemma inference PASS.")
advance_progress(500, "Gemma inference verified")

# ---------------------------------------------------------------------
# 10. NGROK AUTHENTICATION
# ---------------------------------------------------------------------

if NGROK_AUTHTOKEN:
    from pyngrok import ngrok

    ngrok.set_auth_token(NGROK_AUTHTOKEN)
    log("Ngrok authentication configured.")
else:
    log("Ngrok token unavailable. Public tunnel will not be configured.")

advance_progress(520, "Ngrok configuration checked")

# ---------------------------------------------------------------------
# 11. GPU VALIDATION
# ---------------------------------------------------------------------

import torch

if not torch.cuda.is_available():
    raise RuntimeError("CUDA is unavailable.")

if torch.cuda.device_count() < 2:
    raise RuntimeError(
        "Two GPUs are required. "
        "This configuration will not load DreamShaper onto GPU 0."
    )

for gpu_id in range(torch.cuda.device_count()):
    props = torch.cuda.get_device_properties(gpu_id)
    free_mem, total_mem = torch.cuda.mem_get_info(gpu_id)

    log(
        f"GPU {gpu_id}: {props.name} | "
        f"Free: {free_mem/1024**3:.2f} GB | "
        f"Total: {total_mem/1024**3:.2f} GB"
    )

DEVICE = "cuda:1"

free_gpu1, total_gpu1 = torch.cuda.mem_get_info(1)

if free_gpu1 < 3 * 1024**3:
    raise RuntimeError(
        "GPU 1 has less than 3 GB free. "
        "Stopping before DreamShaper initialization."
    )

advance_progress(550, "GPU configuration verified")

# ---------------------------------------------------------------------
# 12. DREAMSHAPER DOWNLOAD AND CACHE
# ---------------------------------------------------------------------

from huggingface_hub import snapshot_download
from diffusers import StableDiffusionPipeline

DREAMSHAPER_REPO = "Lykon/dreamshaper-8"
HF_CACHE = "/root/.cache/huggingface"

allow_patterns = [
    "model_index.json",
    "feature_extractor/preprocessor_config.json",
    "scheduler/*.json",
    "text_encoder/*.json",
    "text_encoder/*.fp16.safetensors",
    "tokenizer/*",
    "unet/*.json",
    "unet/*.fp16.safetensors",
    "vae/*.json",
    "vae/*.fp16.safetensors",
]

if (
    globals().get("DREAMSHAPER_READY", False)
    and globals().get("pipe") is not None
):
    pipe = globals()["pipe"]
    DREAMSHAPER_MODEL_DIR = globals().get(
        "DREAMSHAPER_MODEL_DIR",
        "Existing loaded pipeline"
    )

    log("Existing DreamShaper pipeline detected. Reusing it.")
    advance_progress(760, "DreamShaper already loaded")

else:
    log("Checking local DreamShaper FP16 cache...")

    try:
        model_dir = snapshot_download(
            repo_id=DREAMSHAPER_REPO,
            cache_dir=HF_CACHE,
            allow_patterns=allow_patterns,
            local_files_only=True
        )

        log("Complete local DreamShaper cache found.")

    except Exception as cache_error:
        log("Local cache incomplete. Downloading authenticated FP16 files.")
        print("Cache lookup:", str(cache_error)[:500])

        # Suppress Hugging Face's separate progress bars.
        # The single startup progress bar remains visible.
        class QuietHubTqdm(tqdm):
            def __init__(self, *args, **kwargs):
                kwargs["disable"] = True
                super().__init__(*args, **kwargs)

        INSTALL_BAR.set_postfix_str(
            "Downloading DreamShaper FP16 from Hugging Face",
            refresh=True
        )

        model_dir = snapshot_download(
            repo_id=DREAMSHAPER_REPO,
            cache_dir=HF_CACHE,
            allow_patterns=allow_patterns,
            token=HF_TOKEN,
            tqdm_class=QuietHubTqdm
        )

    model_dir = Path(model_dir)

    required_files = [
        "model_index.json",
        "scheduler/scheduler_config.json",
        "text_encoder/config.json",
        "text_encoder/model.fp16.safetensors",
        "tokenizer/tokenizer_config.json",
        "tokenizer/vocab.json",
        "tokenizer/merges.txt",
        "unet/config.json",
        "unet/diffusion_pytorch_model.fp16.safetensors",
        "vae/config.json",
        "vae/diffusion_pytorch_model.fp16.safetensors",
    ]

    missing = [
        name for name in required_files
        if not (model_dir / name).is_file()
    ]

    if missing:
        raise RuntimeError(
            "DreamShaper files missing: " + ", ".join(missing)
        )

    weight_size = sum(
        (model_dir / name).stat().st_size
        for name in required_files
        if name.endswith(".safetensors")
    )

    log(
        f"DreamShaper FP16 weights verified: "
        f"{weight_size/1024**3:.2f} GiB"
    )

    DREAMSHAPER_MODEL_DIR = str(model_dir)

    advance_progress(760, "DreamShaper files verified")

    # -----------------------------------------------------------------
    # 13. LOAD DREAMSHAPER ON GPU 1
    # -----------------------------------------------------------------

    log("Loading DreamShaper pipeline onto GPU 1. Please wait...")

    pipe = StableDiffusionPipeline.from_pretrained(
        str(model_dir),
        dtype=torch.float16,
        variant="fp16",
        use_safetensors=True,
        local_files_only=True,
        safety_checker=None,
        feature_extractor=None,
        requires_safety_checker=False
    )

    pipe = pipe.to(DEVICE)

    pipe.enable_attention_slicing()
    pipe.set_progress_bar_config(disable=False)

    torch.cuda.synchronize(1)

    DREAMSHAPER_READY = True

    log("DreamShaper pipeline loaded onto GPU 1.")
    advance_progress(970, "DreamShaper loaded")

# ---------------------------------------------------------------------
# 14. SHARED IMAGE GENERATION LOCK
# ---------------------------------------------------------------------

IMAGE_GENERATION_LOCK = globals().get(
    "IMAGE_GENERATION_LOCK",
    threading.Lock()
)

DREAMSHAPER_READY = True

if pipe is None:
    raise RuntimeError("DreamShaper pipeline is unavailable.")

# ---------------------------------------------------------------------
# 15. FINAL READINESS CHECKS
# ---------------------------------------------------------------------

if not ollama_is_online():
    raise RuntimeError("Ollama stopped responding during initialization.")

if not DREAMSHAPER_READY:
    raise RuntimeError("DreamShaper readiness check failed.")

if DEVICE != "cuda:1":
    raise RuntimeError("DreamShaper is not configured for GPU 1.")

torch.cuda.synchronize(1)

advance_progress(1000, "All startup checks passed")
INSTALL_BAR.close()

print("\n" + "=" * 75)
print("UNIFIED AI VIDEO GENERATOR — STARTUP REPORT")
print("=" * 75)

print("Ollama API:", OLLAMA_URL)
print("Gemma model:", OLLAMA_MODEL)
print("Gemma inference: PASS")
print("DreamShaper:", "READY")
print("DreamShaper device:", DEVICE)
print("DreamShaper dtype:", torch.float16)
print("DreamShaper model directory:", DREAMSHAPER_MODEL_DIR)
print("Hugging Face authentication: CONFIGURED")
print("Ngrok authentication:", "CONFIGURED" if NGROK_AUTHTOKEN else "NOT CONFIGURED")
print("Assets directory:", ASSET_DIR)
print("Video output directory:", VIDEO_OUTPUT_DIR)
print("Temporary directory:", TEMP_DIR)

print("\nGPU memory:")
for gpu_id in range(torch.cuda.device_count()):
    free_mem, total_mem = torch.cuda.mem_get_info(gpu_id)
    used_mem = total_mem - free_mem

    print(
        f"GPU {gpu_id}: "
        f"{used_mem/1024**3:.2f} GB used / "
        f"{total_mem/1024**3:.2f} GB total"
    )

print("\nStartup elapsed:", round(time.time()-START_TIME, 1), "seconds")
print("=" * 75)
print("STARTUP COMPLETE — READY FOR CELL 2")
print("=" * 75)
