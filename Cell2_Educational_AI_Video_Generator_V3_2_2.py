
# =====================================================================
# CELL 2 — EDUCATIONAL AI VIDEO GENERATOR V3.2.2
#
# V3.2 — PRESERVES THE V3.0.1 API AND TASK ARCHITECTURE
#
# Preserves Cell 1:
#   Ollama / Gemma / DreamShaper / GPU 1 / existing directories
#
# Features:
#   3–10 minute educational videos
#   Arabic default / English selectable
#   Edge neural TTS, consistent voice per video
#   Dynamic educational scenes
#   Gemma visual relevance inspection
#   Automatic image regeneration
#   Embedded subtitles + downloadable SRT
#   1080p / 30 FPS / AnimateDiff motion clips / waveform
#   FFmpeg full decode validation
#   Existing FastAPI routes preserved
#
# V3.2.2 STABILITY FIXES:
#   Reliable JSON extraction and validation
#   Raw Gemma responses saved before parsing
#   Recovery from incomplete JSON responses
#   Controlled short-script expansion
#   Requested-duration validation
#   Visual inspection response retries
#   Unique server build ID to prevent stale-code reuse
#   Diffusers-version-safe VAE/attention memory optimization
#   Device-only component placement (no accidental dtype recasting)
#   Warning-free CLIP prompt fitting; positive and negative prompts bounded
#   BPE-safe tokenizer decoding (cleanup disabled)
#
# IMPORTANT:
#   Run Cell 1 first.
#   Do not modify Cell 1.
#   No model downloads are performed here.
# =====================================================================

import os
import re
import sys
import json
import uuid
import time
import math
import base64
import shutil
import socket
import threading
import subprocess
import traceback
import requests
import asyncio
import io

from pathlib import Path
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from PIL import Image
import numpy as np
import torch
import uvicorn


# =====================================================================
# 0. EDGE TTS DEPENDENCY
# =====================================================================

_tts_start = time.time()

try:
    import edge_tts
    print("Edge TTS: already installed.")
except ImportError:
    print("Installing edge-tts...")
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "-q", "edge-tts"],
        check=True
    )
    import edge_tts

print(f"Edge TTS check: {time.time() - _tts_start:.1f}s")


# =====================================================================
# 1. VERIFY CELL 1
# =====================================================================

REQUIRED_GLOBALS = [
    "OLLAMA_URL",
    "OLLAMA_MODEL",
    "pipe",
    "DEVICE",
    "IMAGE_GENERATION_LOCK",
    "DREAMSHAPER_READY",
    "ASSET_DIR",
    "VIDEO_OUTPUT_DIR",
    "TEMP_DIR",
    "NGROK_AUTHTOKEN",
    "log",
]

missing = [x for x in REQUIRED_GLOBALS if x not in globals()]

if missing:
    raise RuntimeError(
        "Cell 1 is incomplete. Missing: " + ", ".join(missing)
    )

if not DREAMSHAPER_READY or pipe is None:
    raise RuntimeError("DreamShaper is not ready.")

if not torch.cuda.is_available():
    raise RuntimeError("CUDA is unavailable.")

if DEVICE != "cuda:1":
    raise RuntimeError(
        f"Expected DreamShaper on cuda:1; found {DEVICE}"
    )

for executable in ("ffmpeg", "ffprobe"):
    if not shutil.which(executable):
        raise RuntimeError(f"{executable} is missing.")

OLLAMA_URL = OLLAMA_URL.rstrip("/")

OUTPUT_DIR = Path(VIDEO_OUTPUT_DIR)
TEMP_ROOT = Path(TEMP_DIR)
ASSETS_ROOT = Path(ASSET_DIR)

for directory in (OUTPUT_DIR, TEMP_ROOT, ASSETS_ROOT):
    directory.mkdir(parents=True, exist_ok=True)

log("Cell 1 verified. Cell 2 dependencies ready.")


# =====================================================================
# 2. CONFIGURATION
# =====================================================================

APP_VERSION = "3.2.3"

# A new value on each cell execution.
# This prevents reusing a server running older source code.
APP_BUILD_ID = uuid.uuid4().hex[:12]

VIDEO_WIDTH = 1920
VIDEO_HEIGHT = 1080
VIDEO_FPS = 30

IMAGE_WIDTH = 512
IMAGE_HEIGHT = 512
IMAGE_STEPS = 16
IMAGE_GUIDANCE = 7.0

# V3.2: real AI-generated motion clips on the second T4 (GPU 0).
# DreamShaper remains on GPU 1 exactly as initialized by Cell 1.
ANIMATION_DEVICE = "cuda:0"
ANIMATION_MODEL_ID = "runwayml/stable-diffusion-v1-5"
MOTION_ADAPTER_ID = "guoyww/animatediff-motion-adapter-v1-5-2"
ANIMATION_FRAMES = 24
ANIMATION_FPS = 8
ANIMATION_STEPS = 12
ANIMATION_GUIDANCE = 7.0
ANIMATION_PIPELINE = None
ANIMATION_PIPELINE_LOCK = threading.RLock()
ANIMATION_BACKEND = "AnimateDiff"

DEFAULT_DURATION_MINUTES = 3
MIN_DURATION_MINUTES = 3
MAX_DURATION_MINUTES = 10

SECONDS_PER_SCENE = 25

MAX_IMAGE_ATTEMPTS = 3
MAX_JSON_ATTEMPTS = 3
MAX_SCRIPT_ATTEMPTS = 3

GEMMA_TIMEOUT = 900
VISION_TIMEOUT = 240

# One video at a time.
VIDEO_TASK_LOCK = threading.Lock()

KEEP_TASK_ARTIFACTS = True

NEGATIVE_PROMPT = (
    "black image, blank image, empty scene, blurry, low quality, "
    "distorted objects, malformed anatomy, incorrect anatomy, "
    "unrelated objects, text, labels, watermark, logo, "
    "poor composition, low contrast, collage, diptych, triptych, "
    "split screen, multiple panels, duplicate subjects, side by side "
    "comparison, cropped main subject, cut off anatomy, gibberish text, "
    "misspelled labels, typography, diagram labels, borders, frames"
)

OPENING_SCENE_MARKER = "OPENING_SCENE_SINGLE_SUBJECT_COMPOSITION"

def build_scene_visual_prompt(scene_index, subject, visual_prompt):
    """Apply a strict single-subject composition to the opening shot only."""
    prompt = re.sub(r"\s+", " ", str(visual_prompt or "")).strip()
    if int(scene_index) != 1 or OPENING_SCENE_MARKER in prompt:
        return prompt

    subject = re.sub(r"\s+", " ", str(subject or "main educational subject")).strip()
    return (
        f"{OPENING_SCENE_MARKER}. ONE SINGLE {subject.upper()} ONLY. "
        "Create one complete, unmistakable main subject as one unified object. "
        "Place it large, centered, fully inside the frame, with clear space "
        "around every edge. Use one continuous full-frame composition and "
        "one simple uncluttered background. Premium educational 3D "
        "documentary illustration, accurate recognizable structure, clear "
        "silhouette, controlled soft studio lighting. This is an opening "
        "hero shot, NOT an infographic. "
        "Do not show multiple versions, duplicates, comparison views, "
        "collages, panels, split screens, borders, inset pictures, diagrams, "
        "arrows, labels, letters, numbers, captions or watermarks. "
        + prompt
    )

VOICE_OPTIONS = {
    "ar": {
        "default": "ar-LB-RamiNeural",
        "available": [
            "ar-LB-RamiNeural",
            "ar-LB-LaylaNeural"
        ]
    },
    "en": {
        "default": "en-US-GuyNeural",
        "available": [
            "en-US-GuyNeural",
            "en-US-JennyNeural"
        ]
    }
}


# =====================================================================
# 3. TASK STORAGE
# =====================================================================

TASKS = {}
TASKS_LOCK = threading.RLock()


def update_task(task_id, **values):
    with TASKS_LOCK:
        if task_id not in TASKS:
            return

        TASKS[task_id].update(values)
        TASKS[task_id]["updated_at"] = (
            datetime.now().astimezone().isoformat()
        )


def get_task(task_id):
    with TASKS_LOCK:
        if task_id not in TASKS:
            return None
        return dict(TASKS[task_id])


def task_log(task_id, message):
    with TASKS_LOCK:
        if task_id not in TASKS:
            return

        logs = list(TASKS[task_id].get("logs", []))

        logs.append({
            "time": datetime.now().astimezone().isoformat(),
            "message": str(message)
        })

        TASKS[task_id]["logs"] = logs[-150:]
        TASKS[task_id]["updated_at"] = (
            datetime.now().astimezone().isoformat()
        )

    print(f"[TASK {task_id[:8]}] {message}", flush=True)


def task_progress(task_id, progress, stage, message):
    update_task(
        task_id,
        progress=max(0, min(100, int(progress))),
        stage=stage,
        message=message
    )
    task_log(task_id, message)


# =====================================================================
# 4. FASTAPI
# =====================================================================

app = FastAPI(
    title="Educational AI Video Generator",
    description=(
        "Animated educational and story videos with neural narration, "
        "AI motion scenes and synchronized subtitles."
    ),
    version=APP_VERSION
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"]
)


class VideoRequest(BaseModel):
    prompt: str = Field(..., min_length=5, max_length=4000)

    duration_minutes: int = Field(
        default=DEFAULT_DURATION_MINUTES,
        ge=MIN_DURATION_MINUTES,
        le=MAX_DURATION_MINUTES
    )

    language: str = Field(default="ar")

    voice: Optional[str] = Field(default=None)


# =====================================================================
# 5. COMMON HELPERS
# =====================================================================

def parse_json_response(raw):
    """
    Parse a JSON object without using a greedy regular expression.

    This also handles harmless text before or after a complete
    JSON object. It cannot repair genuinely truncated JSON;
    those responses are saved and retried separately.
    """

    raw = str(raw or "").strip()

    if not raw:
        raise RuntimeError("Gemma returned an empty response.")

    try:
        result = json.loads(raw)
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()

    for index, character in enumerate(raw):
        if character != "{":
            continue

        try:
            result, end_index = decoder.raw_decode(raw[index:])

            if isinstance(result, dict):
                return result

        except json.JSONDecodeError:
            continue

    raise RuntimeError(
        "No complete valid JSON object found in Gemma response. "
        f"Response length: {len(raw)} characters."
    )


def count_words(text):
    return len(re.findall(r"\S+", str(text).strip()))


def get_media_duration(path):
    result = subprocess.run(
        [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path)
        ],
        capture_output=True,
        text=True,
        check=True
    )

    duration = float(result.stdout.strip())

    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError(f"Invalid media duration: {path}")

    return duration


def safe_voice(language, requested_voice):
    if language not in VOICE_OPTIONS:
        raise ValueError("Language must be 'ar' or 'en'.")

    options = VOICE_OPTIONS[language]
    selected = requested_voice or options["default"]

    if selected not in options["available"]:
        raise ValueError(
            f"Voice {selected} is not available for {language}. "
            f"Available voices: {options['available']}"
        )

    return selected


def save_debug_text(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(content or ""), encoding="utf-8")
    return path



# =====================================================================
# 6. EDUCATIONAL SCRIPT GENERATION — V3.2.0 SCRIPT VALIDATION
#
# Fixes:
#   - Count narration even when visual fields are missing.
#   - Validate every scene independently.
#   - Recover incomplete scenes using the existing lesson.
#   - Repair missing visual fields without discarding narration.
#   - Save raw Gemma output and metadata before parsing.
#   - Detect truncated Gemma responses.
#   - Preserve the existing Gemma/Ollama architecture.
# =====================================================================

def generate_scenario(user_prompt, duration_minutes, language):

    scene_count = math.ceil(
        duration_minutes * 60 / SECONDS_PER_SCENE
    )

    target_wpm = 130 if language == "ar" else 145
    target_total_words = duration_minutes * target_wpm

    minimum_words = math.ceil(target_total_words * 0.95)
    minimum_scene_words = 20

    debug_dir = (
        TEMP_ROOT / "gemma_raw_responses" / uuid.uuid4().hex[:10]
    )
    debug_dir.mkdir(parents=True, exist_ok=True)

    language_instruction = (
        """
All narration must be natural Modern Standard Arabic.
Use clear educational Arabic suitable for spoken narration.
Avoid awkward literal translations and unnecessarily complex wording.
"""
        if language == "ar"
        else
        """
All narration must be natural spoken English.
Use clear educational language suitable for a general audience.
"""
    )

    system_prompt = f"""
You are an experienced educational documentary writer,
science communicator, teacher and visual director.

Create a complete educational lesson.

LANGUAGE:
{language_instruction}

VIDEO REQUIREMENTS:
- Requested duration: {duration_minutes} minutes.
- Exact number of scenes: {scene_count}.
- Target spoken words: approximately {target_total_words}.
- Minimum total spoken words: {minimum_words}.
- Every scene must have meaningful, complete narration.
- Every narration must sound natural when spoken aloud.

EDUCATIONAL STRUCTURE:
1. Engaging opening.
2. Introduce the subject.
3. Explain the main concepts logically.
4. Use examples and analogies where useful.
5. Explain cause and effect.
6. Avoid repetition and filler.
7. Finish with a useful summary.

VISUAL REQUIREMENTS:
- Every image must illustrate its own narration.
- visual_subject must identify the main visible subject.
- visual_prompt_en must describe exactly what should appear.
- Keep the main subject recognizable and prominent.
- Use educational documentary illustration quality.
- No text, labels, captions, logos or watermarks inside images.

OUTPUT:
Return ONLY valid JSON:

{{
  "title": "Video title",
  "scenes": [
    {{
      "narration": "Complete spoken narration",
      "visual_subject": "Main visible subject",
      "visual_prompt_en": "Detailed English visual prompt"
    }}
  ]
}}

STRICT RULES:
- Exactly {scene_count} scenes.
- At least {minimum_scene_words} narration words per scene.
- No empty fields.
- No unfinished sentences.
- No missing visual fields.
- No markdown or commentary outside JSON.
"""

    # -------------------------------------------------------------
    # GEMMA REQUEST — RAW OUTPUT ALWAYS SAVED BEFORE PARSING
    # -------------------------------------------------------------

    def request_gemma(prompt_text, generation_number):

        token_budgets = [8000, 10000, 10000]
        last_error = None

        current_prompt = prompt_text

        for json_attempt in range(1, MAX_JSON_ATTEMPTS + 1):

            token_budget = token_budgets[json_attempt - 1]

            print(
                f"[SCRIPT] Generation {generation_number}; "
                f"JSON attempt {json_attempt}/{MAX_JSON_ATTEMPTS}; "
                f"output budget={token_budget}.",
                flush=True
            )

            payload = {
                "model": OLLAMA_MODEL,
                "system": system_prompt,
                "prompt": current_prompt,
                "format": "json",
                "stream": False,
                "think": False,
                "keep_alive": "10m",
                "options": {
                    "temperature": 0.25,
                    "num_ctx": 16384,
                    "num_predict": token_budget
                }
            }

            started = time.time()

            try:
                response = requests.post(
                    f"{OLLAMA_URL}/api/generate",
                    json=payload,
                    timeout=(15, GEMMA_TIMEOUT)
                )

                response.raise_for_status()
                data = response.json()

                raw = str(data.get("response", "") or "")
                elapsed = time.time() - started

                raw_path = debug_dir / (
                    f"generation_{generation_number}"
                    f"_json_{json_attempt}.txt"
                )

                # Save exact response before any parsing.
                save_debug_text(raw_path, raw)

                metadata = {
                    "generation": generation_number,
                    "json_attempt": json_attempt,
                    "elapsed_seconds": round(elapsed, 2),
                    "done": data.get("done"),
                    "done_reason": data.get("done_reason"),
                    "eval_count": data.get("eval_count"),
                    "prompt_eval_count": data.get(
                        "prompt_eval_count"
                    ),
                    "response_characters": len(raw),
                    "raw_response_file": str(raw_path)
                }

                save_debug_text(
                    debug_dir / (
                        f"generation_{generation_number}"
                        f"_json_{json_attempt}_metadata.json"
                    ),
                    json.dumps(metadata, indent=2)
                )

                print(
                    f"[SCRIPT] Received in {elapsed:.1f}s | "
                    f"Characters={len(raw)} | "
                    f"Done={data.get('done')} | "
                    f"Reason={data.get('done_reason')} | "
                    f"Tokens={data.get('eval_count')}",
                    flush=True
                )

                if not raw.strip():
                    raise RuntimeError(
                        "Gemma returned an empty response."
                    )

                if data.get("done") is False:
                    raise RuntimeError(
                        "Gemma response did not finish."
                    )

                if data.get("done_reason") == "length":
                    raise RuntimeError(
                        "Gemma stopped because the output token "
                        "limit was reached."
                    )

                scenario = parse_json_response(raw)

                if not isinstance(scenario, dict):
                    raise RuntimeError(
                        "Gemma JSON root is not an object."
                    )

                if not isinstance(scenario.get("scenes"), list):
                    raise RuntimeError(
                        "JSON is valid, but scenes is not a list."
                    )

                print(
                    f"[SCRIPT] Valid JSON received. "
                    f"Saved: {raw_path}",
                    flush=True
                )

                return scenario

            except Exception as exc:

                last_error = str(exc)

                print(
                    f"[SCRIPT] JSON attempt failed: {last_error}",
                    flush=True
                )

                if json_attempt < MAX_JSON_ATTEMPTS:

                    current_prompt = f"""
Regenerate the COMPLETE JSON response.

Original topic:
{user_prompt}

Required:
- Exactly {scene_count} scenes.
- At least {minimum_words} spoken words.
- Approximately {target_total_words} spoken words.
- Every scene must have narration, visual_subject,
  and visual_prompt_en.
- At least 20 narration words per scene.
- Complete sentences.
- Complete valid JSON.
- No markdown or explanations.
- Do not stop in the middle of a JSON string.

Return ONLY the JSON object.
"""

        raise RuntimeError(
            "Gemma could not produce complete valid JSON. "
            f"Last error: {last_error}. "
            f"Debug directory: {debug_dir}"
        )

    # -------------------------------------------------------------
    # VALIDATION — COUNT NARRATION BEFORE CHECKING VISUAL FIELDS
    # -------------------------------------------------------------

    def validate_scenario(scenario):

        problems = []
        validated = []
        total_words = 0

        scenes = scenario.get("scenes")

        if not isinstance(scenes, list):
            return [], 0, ["The scenes field is not a list."]

        if len(scenes) != scene_count:
            problems.append(
                f"Expected {scene_count} scenes; "
                f"received {len(scenes)}."
            )

        for index, scene in enumerate(scenes, start=1):

            if not isinstance(scene, dict):
                problems.append(
                    f"Scene {index} is not a JSON object."
                )
                continue

            narration = str(
                scene.get("narration") or ""
            ).strip()

            subject = str(
                scene.get("visual_subject") or ""
            ).strip()

            visual_prompt = str(
                scene.get("visual_prompt_en") or ""
            ).strip()

            # CRITICAL FIX:
            # Count narration independently of visual completeness.
            word_count = count_words(narration)
            total_words += word_count

            if not narration:
                problems.append(
                    f"Scene {index}: narration is missing."
                )
            elif word_count < minimum_scene_words:
                problems.append(
                    f"Scene {index}: only {word_count} narration "
                    f"words; minimum is {minimum_scene_words}."
                )

            if not subject:
                problems.append(
                    f"Scene {index}: visual_subject is missing."
                )

            if not visual_prompt:
                problems.append(
                    f"Scene {index}: visual_prompt_en is missing."
                )

            if visual_prompt and len(visual_prompt.split()) > 90:
                problems.append(
                    f"Scene {index}: visual prompt exceeds 90 words."
                )

            # Preserve every usable scene, even if its visual fields
            # need repair. This allows targeted recovery.
            validated.append({
                "narration": narration,
                "visual_subject": subject,
                "visual_prompt_en": visual_prompt,
                "word_count": word_count
            })

        if total_words < minimum_words:
            problems.append(
                f"Total narration is {total_words} words; "
                f"minimum is {minimum_words}."
            )

        return validated, total_words, problems

    # -------------------------------------------------------------
    # INITIAL GENERATION
    # -------------------------------------------------------------

    scenario = request_gemma(user_prompt, 1)

    # -------------------------------------------------------------
    # CONTROLLED REPAIR / EXPANSION
    # -------------------------------------------------------------

    for attempt in range(1, MAX_SCRIPT_ATTEMPTS + 1):

        validated, total_words, problems = validate_scenario(
            scenario
        )

        print(
            f"[SCRIPT] Validation {attempt}: "
            f"{total_words}/{target_total_words} words; "
            f"minimum={minimum_words}; "
            f"scenes={len(scenario.get('scenes', []))}/"
            f"{scene_count}.",
            flush=True
        )

        if not problems:

            title = str(
                scenario.get("title") or ""
            ).strip()

            if not title:
                title = (
                    "درس تعليمي"
                    if language == "ar"
                    else "Educational Lesson"
                )

            # Remove internal word_count from public scene schema.
            final_scenes = []

            for scene in validated:
                final_scenes.append({
                    "narration": scene["narration"],
                    "visual_subject": scene["visual_subject"],
                    "visual_prompt_en": scene["visual_prompt_en"]
                })

            result = {
                "title": title,
                "scenes": final_scenes,
                "total_words": total_words,
                "scene_count": scene_count
            }

            print(
                f"[SCRIPT] PASS: {total_words} words; "
                f"{scene_count} complete scenes.",
                flush=True
            )

            return result

        if attempt == MAX_SCRIPT_ATTEMPTS:
            raise RuntimeError(
                "Script validation failed after "
                f"{MAX_SCRIPT_ATTEMPTS} attempts. "
                + " | ".join(problems)
                + f" Debug directory: {debug_dir}"
            )

        # Keep the existing lesson and ask Gemma to repair it.
        # Do not throw away the usable narration.
        existing_lesson = json.dumps(
            scenario,
            ensure_ascii=False,
            indent=2
        )

        repair_prompt = f"""
REPAIR THE EXISTING EDUCATIONAL LESSON.

TOPIC:
{user_prompt}

CURRENT LESSON JSON:
{existing_lesson}

CURRENT NARRATION WORD COUNT:
{total_words}

REQUIRED MINIMUM:
{minimum_words}

TARGET:
{target_total_words} words

VALIDATION PROBLEMS:
{chr(10).join(problems)}

INSTRUCTIONS:

1. Return exactly {scene_count} scenes.
2. Preserve the existing lesson topic and logical sequence.
3. Preserve complete narration that is already correct.
4. Finish incomplete narration and sentences.
5. Fill in missing visual_subject fields.
6. Fill in missing visual_prompt_en fields.
7. Every scene must have at least 20 narration words.
8. Expand the lesson naturally to at least {minimum_words} words.
9. Aim for approximately {target_total_words} words.
10. Do not use filler or repeated sentences.
11. Keep every visual prompt relevant to its narration.
12. Keep visual prompts concise and specific.
13. Return the complete lesson using the same JSON schema.

Return ONLY valid JSON.
"""

        print(
            "[SCRIPT] Repairing incomplete scenes and/or "
            "expanding narration while preserving the lesson.",
            flush=True
        )

        scenario = request_gemma(
            repair_prompt,
            attempt + 1
        )

    raise RuntimeError("Unexpected script-generation exit.")

# =====================================================================
# 7. NEURAL TEXT-TO-SPEECH
# =====================================================================

def synthesize_audio(text, output_path, voice):

    output_path = Path(output_path)

    async def generate():
        communicate = edge_tts.Communicate(
            text=text,
            voice=voice,
            rate="-5%",
            volume="+0%",
            pitch="+0Hz"
        )

        await communicate.save(str(output_path))

    asyncio.run(generate())

    if (
        not output_path.exists()
        or output_path.stat().st_size < 1000
    ):
        raise RuntimeError(
            "Neural TTS returned an invalid audio file."
        )

    duration = get_media_duration(output_path)

    return {
        "path": output_path,
        "duration": duration,
        "size": output_path.stat().st_size
    }


# =====================================================================
# 8. PROMPT TOKEN SAFETY — CLIP 77-TOKEN LIMIT
# =====================================================================

def _clip_encode_without_length_warning(text, tokenizer):
    """Tokenize text without asking Transformers to pad/truncate or emit
    its overlength warning. We need the full token sequence to fit it safely.
    """
    text = str(text or "")
    pieces = tokenizer.tokenize(text)
    token_ids = tokenizer.convert_tokens_to_ids(pieces)
    if token_ids is None:
        return []
    return list(token_ids)


def _clip_count_with_special_tokens(token_ids, tokenizer):
    """Count CLIP input tokens without relying on an optional tokenizer API.

    Some Transformers CLIPTokenizer releases do not expose the optional
    special-token construction method, although they do expose
    num_special_tokens_to_add(). For a single CLIP text sequence, the final
    input length is the BPE token count plus the tokenizer's BOS/EOS tokens.
    """
    token_count = len(list(token_ids))
    special_counter = getattr(tokenizer, "num_special_tokens_to_add", None)
    if callable(special_counter):
        try:
            special_count = int(special_counter(pair=False))
        except TypeError:
            special_count = int(special_counter())
        return token_count + special_count

    # Compatibility fallback for tokenizer wrappers that expose their
    # special-token IDs but not num_special_tokens_to_add().
    special_ids = getattr(tokenizer, "all_special_ids", None)
    if special_ids is not None:
        # CLIP's single-sequence format uses BOS and EOS exactly once.
        return token_count + 2
    raise RuntimeError(
        "Cannot determine CLIP special-token count: tokenizer exposes neither "
        "num_special_tokens_to_add() nor CLIP special-token metadata."
    )


def _clip_decode(token_ids, tokenizer):
    # CLIP uses a BPE tokenizer. Cleanup=True is ignored and can alter spacing.
    return tokenizer.decode(
        list(token_ids),
        skip_special_tokens=True,
        clean_up_tokenization_spaces=False
    ).strip(" ,")


def fit_clip_text(prompt, tokenizer, limit=77):
    """Fit arbitrary CLIP text (notably negative prompts) to its real limit."""
    if tokenizer is None:
        raise RuntimeError("CLIP tokenizer is unavailable.")
    limit = int(limit)
    special_count = int(tokenizer.num_special_tokens_to_add(pair=False))
    token_budget = limit - special_count
    if token_budget < 1:
        raise ValueError(f"Invalid CLIP token limit: {limit}")

    token_ids = _clip_encode_without_length_warning(prompt, tokenizer)
    if _clip_count_with_special_tokens(token_ids, tokenizer) <= limit:
        return str(prompt or "").strip(), _clip_count_with_special_tokens(token_ids, tokenizer)

    fitted_ids = token_ids[:token_budget]
    fitted = _clip_decode(fitted_ids, tokenizer)

    # Decode/re-encode can change BPE boundaries. Verify the exact text that
    # will be passed to Diffusers and shorten until it is strictly safe.
    while fitted_ids and _clip_count_with_special_tokens(
        _clip_encode_without_length_warning(fitted, tokenizer), tokenizer
    ) > limit:
        fitted_ids.pop()
        fitted = _clip_decode(fitted_ids, tokenizer)

    final_count = _clip_count_with_special_tokens(
        _clip_encode_without_length_warning(fitted, tokenizer), tokenizer
    )
    if not fitted or final_count > limit:
        raise RuntimeError(f"Could not fit text within CLIP's {limit}-token limit.")
    return fitted, final_count


def fit_clip_prompt(prompt, subject="", tokenizer=None, limit=77):
    """Fit a positive visual prompt using the actual CLIP tokenizer.

    The subject is prioritized, then scene details, then a compact style tail.
    Tokenization is performed through the tokenizer's low-level BPE methods so
    an overlength input never reaches the Transformers model-length warning.
    """
    tokenizer = tokenizer or getattr(pipe, "tokenizer", None)
    if tokenizer is None:
        raise RuntimeError("DreamShaper CLIP tokenizer is unavailable.")

    subject = re.sub(r"\s+", " ", str(subject or "")).strip()
    prompt = re.sub(r"\s+", " ", str(prompt or "")).strip()
    style = "high quality cinematic 3D animated cartoon, expressive characters"
    parts = [x for x in (subject, prompt, style) if x]
    candidate = ", ".join(parts)

    candidate_ids = _clip_encode_without_length_warning(candidate, tokenizer)
    candidate_count = _clip_count_with_special_tokens(candidate_ids, tokenizer)
    if candidate_count <= limit:
        return candidate, candidate_count

    special_count = int(tokenizer.num_special_tokens_to_add(pair=False))
    budget = int(limit) - special_count
    if budget < 1:
        raise ValueError(f"Invalid CLIP token limit: {limit}")

    subject_ids = _clip_encode_without_length_warning(subject or "main subject", tokenizer)
    prompt_ids = _clip_encode_without_length_warning(prompt, tokenizer)
    style_ids = _clip_encode_without_length_warning(style, tokenizer)

    # Keep explicit separators between separately tokenized prompt sections.
    separator_ids = _clip_encode_without_length_warning(", ", tokenizer)
    subject_take = min(len(subject_ids), max(1, budget // 3))
    if len(subject_ids) >= budget:
        subject_take = budget
        prompt_take = 0
        style_take = 0
    else:
        remaining = budget - subject_take
        separator_cost = len(separator_ids) * 2
        style_reserve = min(len(style_ids), 8)
        prompt_take = min(
            len(prompt_ids),
            max(0, remaining - style_reserve - separator_cost)
        )
        remaining -= prompt_take
        if prompt_take:
            remaining -= len(separator_ids)
        style_take = min(len(style_ids), max(0, remaining - (len(separator_ids) if prompt_take else 0)))

    fitted_ids = subject_ids[:subject_take]
    if prompt_take:
        fitted_ids += separator_ids + prompt_ids[:prompt_take]
    if style_take:
        fitted_ids += separator_ids + style_ids[:style_take]
    fitted_ids = fitted_ids[:budget]
    fitted = _clip_decode(fitted_ids, tokenizer)

    # Exact post-decode verification prevents a second warning inside
    # Diffusers when it tokenizes the final prompt for inference.
    while fitted_ids:
        verified_ids = _clip_encode_without_length_warning(fitted, tokenizer)
        if _clip_count_with_special_tokens(verified_ids, tokenizer) <= limit:
            break
        fitted_ids.pop()
        fitted = _clip_decode(fitted_ids, tokenizer)

    final_count = _clip_count_with_special_tokens(
        _clip_encode_without_length_warning(fitted, tokenizer), tokenizer
    )
    if not fitted or final_count > limit:
        raise RuntimeError(f"Could not fit visual prompt within CLIP's {limit} tokens.")
    return fitted, final_count


def load_animation_pipeline():
    """
    Load AnimateDiff on GPU 0, keeping DreamShaper on GPU 1.
    Download/model compatibility is checked at runtime, not assumed.
    """
    global ANIMATION_PIPELINE

    with ANIMATION_PIPELINE_LOCK:
        if ANIMATION_PIPELINE is not None:
            return ANIMATION_PIPELINE

        if torch.cuda.device_count() < 2:
            raise RuntimeError(
                "V3.2 animation requires the second T4 (cuda:0); "
                "Cell 1 must expose both GPUs."
            )

        try:
            from diffusers import AnimateDiffPipeline, MotionAdapter
        except Exception as exc:
            raise RuntimeError(
                "AnimateDiff is unavailable in the installed diffusers build. "
                "Cell 1 was intentionally left unchanged. "
                "Install a compatible diffusers build in Cell 2, then rerun. "
                f"Import error: {exc}"
            ) from exc

        print(
            f"[ANIMATION] Loading motion adapter {MOTION_ADAPTER_ID} "
            f"and SD1.5 base on {ANIMATION_DEVICE}.",
            flush=True
        )
        started = time.time()

        adapter = MotionAdapter.from_pretrained(
            MOTION_ADAPTER_ID,
            torch_dtype=torch.float16
        )

        animation_pipe = AnimateDiffPipeline.from_pretrained(
            ANIMATION_MODEL_ID,
            motion_adapter=adapter,
            torch_dtype=torch.float16,
            variant="fp16",
            use_safetensors=True
        )

        # VAE slicing belongs to the VAE component, not the pipeline.
        vae = getattr(animation_pipe, "vae", None)
        enable_vae_slicing = getattr(vae, "enable_slicing", None)
        if callable(enable_vae_slicing):
            enable_vae_slicing()
            print("[ANIMATION] VAE slicing enabled.", flush=True)
        else:
            print("[ANIMATION] VAE slicing unsupported; safely skipped.", flush=True)

        # Attention slicing is optional and varies across Diffusers versions.
        enable_attention_slicing = getattr(
            animation_pipe, "enable_attention_slicing", None
        )
        if callable(enable_attention_slicing):
            enable_attention_slicing()
            print("[ANIMATION] Attention slicing enabled.", flush=True)
        else:
            print("[ANIMATION] Attention slicing unsupported; safely skipped.", flush=True)

        # Move each model component by DEVICE ONLY. Diffusers versions whose
        # pipeline.to() forwards dtype=None can trigger a misleading fp32
        # advisory. Both from_pretrained() calls above already set torch_dtype.
        target_device = torch.device(ANIMATION_DEVICE)
        moved_components = []
        for component_name, component in animation_pipe.components.items():
            if isinstance(component, torch.nn.Module):
                component.to(device=target_device)
                moved_components.append(component_name)

        if not moved_components:
            raise RuntimeError("AnimateDiff pipeline exposes no movable model components.")

        # Verify placement without recasting model weights.
        misplaced = []
        for component_name, component in animation_pipe.components.items():
            if isinstance(component, torch.nn.Module):
                try:
                    component_device = next(component.parameters()).device
                except StopIteration:
                    continue
                if component_device != target_device:
                    misplaced.append(f"{component_name}:{component_device}")
        if misplaced:
            raise RuntimeError(
                "AnimateDiff components were not moved to "
                f"{target_device}: {', '.join(misplaced)}"
            )
        print(
            "[ANIMATION] Components placed on "
            f"{target_device}: {', '.join(moved_components)}",
            flush=True
        )

        ANIMATION_PIPELINE = animation_pipe
        print(
            f"[ANIMATION] AnimateDiff ready in {time.time()-started:.1f}s "
            f"on {ANIMATION_DEVICE}.",
            flush=True
        )
        return ANIMATION_PIPELINE


def generate_animated_scene(visual_prompt, subject, output_path, seed, task_id, scene_index):
    """
    Generate a real multi-frame AnimateDiff clip, then encode it as MP4.
    The clip is looped to narration length during final segment rendering.
    A still image / zoompan fallback is deliberately forbidden.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    visual_prompt = build_scene_visual_prompt(
        scene_index, subject, visual_prompt
    )
    animation_pipe = load_animation_pipeline()
    fitted_prompt, token_count = fit_clip_prompt(
        visual_prompt,
        subject=subject,
        tokenizer=animation_pipe.tokenizer,
        limit=77
    )
    fitted_negative, negative_token_count = fit_clip_text(
        NEGATIVE_PROMPT,
        tokenizer=animation_pipe.tokenizer,
        limit=77
    )

    task_log(
        task_id,
        f"Scene {scene_index}: AnimateDiff positive={token_count}/77, "
        f"negative={negative_token_count}/77 CLIP tokens."
    )
    save_debug_text(
        output_path.with_suffix(".prompt.txt"),
        json.dumps(
            {
                "subject": subject,
                "prompt": fitted_prompt,
                "clip_tokens": token_count,
                "negative_clip_tokens": negative_token_count,
                "seed": int(seed),
                "backend": ANIMATION_BACKEND
            },
            ensure_ascii=False,
            indent=2
        )
    )

    generator = torch.Generator(device=ANIMATION_DEVICE).manual_seed(int(seed))
    started = time.time()

    with torch.inference_mode():
        result = animation_pipe(
            prompt=fitted_prompt,
            negative_prompt=fitted_negative,
            num_frames=ANIMATION_FRAMES,
            num_inference_steps=ANIMATION_STEPS,
            guidance_scale=ANIMATION_GUIDANCE,
            generator=generator
        )

    frames = result.frames[0]
    if len(frames) < 2:
        raise RuntimeError("AnimateDiff returned fewer than two frames.")

    first_frame_path = output_path.with_name(
        output_path.stem + "_first_frame.png"
    )
    frames[0].convert("RGB").save(first_frame_path)

    # Reject near-static generated sequences before encoding.
    frame_diffs = []
    prev = np.asarray(frames[0].convert("RGB"), dtype=np.int16)
    for frame in frames[1:]:
        current = np.asarray(frame.convert("RGB"), dtype=np.int16)
        frame_diffs.append(float(np.abs(current - prev).mean()))
        prev = current

    mean_motion = float(np.mean(frame_diffs)) if frame_diffs else 0.0
    if mean_motion < 0.75:
        raise RuntimeError(
            f"AnimateDiff motion validation failed: "
            f"mean frame difference={mean_motion:.3f}."
        )

    frame_dir = output_path.parent / (output_path.stem + "_frames")
    frame_dir.mkdir(parents=True, exist_ok=True)
    for frame_index, frame in enumerate(frames):
        frame.convert("RGB").save(
            frame_dir / f"frame_{frame_index:04d}.png"
        )

    command = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-framerate", str(ANIMATION_FPS),
        "-i", str(frame_dir / "frame_%04d.png"),
        "-vf", "scale=512:512:force_original_aspect_ratio=increase,crop=512:512,format=yuv420p",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-an", "-movflags", "+faststart",
        str(output_path)
    ]
    encoded = subprocess.run(command, capture_output=True, text=True)
    if encoded.returncode != 0:
        raise RuntimeError(
            "AnimateDiff clip encoding failed: " + encoded.stderr[-3000:]
        )

    clip_duration = get_media_duration(output_path)
    if clip_duration < 1.0 or output_path.stat().st_size < 20_000:
        raise RuntimeError("AnimateDiff produced an invalid motion clip.")

    motion_audit = {
        "backend": ANIMATION_BACKEND,
        "frame_count": len(frames),
        "fps": ANIMATION_FPS,
        "clip_duration_seconds": round(clip_duration, 3),
        "mean_frame_difference": round(mean_motion, 3),
        "clip_prompt_tokens": token_count,
        "negative_prompt_tokens": negative_token_count,
        "generation_seconds": round(time.time() - started, 2),
        "motion_validation": "PASS"
    }
    save_debug_text(
        output_path.with_suffix(".motion.json"),
        json.dumps(motion_audit, indent=2)
    )

    del result, frames
    if torch.cuda.is_available():
        torch.cuda.synchronize(0)
        torch.cuda.empty_cache()

    return {
        "path": output_path,
        "first_frame_path": first_frame_path,
        "duration": clip_duration,
        "motion_audit": motion_audit
    }


# =====================================================================
# 8. DREAMSHAPER IMAGE GENERATION
# =====================================================================

def generate_scene_image(
    visual_prompt, output_path, seed, scene_index=0, subject=""
):

    output_path = Path(output_path)

    if int(scene_index) == 1:
        visual_prompt = build_scene_visual_prompt(
            scene_index, subject, visual_prompt
        )
        priority_subject = (
            f"one single complete {subject}, centered, fully visible"
        )
    else:
        priority_subject = subject

    image_tokenizer = getattr(pipe, "tokenizer", None)
    visual_prompt, clip_tokens = fit_clip_prompt(
        visual_prompt,
        subject=priority_subject,
        tokenizer=image_tokenizer,
        limit=77
    )
    fitted_negative, negative_clip_tokens = fit_clip_text(
        NEGATIVE_PROMPT,
        tokenizer=image_tokenizer,
        limit=77
    )
    print(
        f"[IMAGE] DreamShaper positive={clip_tokens}/77, "
        f"negative={negative_clip_tokens}/77 CLIP tokens.",
        flush=True
    )

    generator = torch.Generator(
        device=DEVICE
    ).manual_seed(int(seed))

    with IMAGE_GENERATION_LOCK:

        with torch.inference_mode():

            result = pipe(
                prompt=visual_prompt,
                negative_prompt=fitted_negative,
                width=IMAGE_WIDTH,
                height=IMAGE_HEIGHT,
                num_inference_steps=IMAGE_STEPS,
                guidance_scale=IMAGE_GUIDANCE,
                generator=generator
            )

            generated_image = result.images[0]

        torch.cuda.synchronize(1)

    generated_image.save(output_path)

    with Image.open(output_path) as check:
        check = check.convert("RGB")
        pixels = np.asarray(check)

        if check.size != (IMAGE_WIDTH, IMAGE_HEIGHT):
            raise RuntimeError(
                "Generated image has incorrect dimensions."
            )

        if float(pixels.std()) < 5:
            raise RuntimeError(
                "Generated image appears blank or uniform."
            )

    if output_path.stat().st_size < 10_000:
        raise RuntimeError(
            "Generated image file is unexpectedly small."
        )

    del result
    del generated_image

    return output_path


# =====================================================================
# 9. GEMMA VISUAL RELEVANCE INSPECTION
# =====================================================================

def inspect_image_relevance(
    image_path,
    narration,
    expected_subject,
    scene_index=0
):

    image_path = Path(image_path)

    with Image.open(image_path) as image_file:
        image_file = image_file.convert("RGB")
        image_file.thumbnail((512, 512))

        buffer = io.BytesIO()
        image_file.save(buffer, format="JPEG", quality=88)

        image_base64 = base64.b64encode(
            buffer.getvalue()
        ).decode("utf-8")

    prompt = f"""
Inspect the supplied image carefully.

NARRATION:
{narration}

EXPECTED MAIN SUBJECT:
{expected_subject}

Determine whether the ACTUAL image clearly depicts the expected
subject and is suitable for this exact narration.

Do not approve a generic image merely because it relates
to the general topic.

Return ONLY JSON:

{{
  "relevant": true,
  "observed_subject": "What is actually visible",
  "reason": "Short explanation"
}}

Set relevant=false if the main subject is missing,
unrecognizable or clearly unrelated.

For the opening scene (scene_index=1), also evaluate composition:
- exactly ONE main subject, not several versions of it;
- one continuous full-frame image, not a collage or multi-panel layout;
- the complete main subject is inside the image, not cut off;
- no gibberish, pseudo-letters, fake labels or accidental typography.

Return "composition_ok": true only if ALL four opening-scene
composition requirements pass. Otherwise return false and explain
the defect in "reason". For other scenes set composition_ok=true.

Return JSON with these fields:
{{
  "relevant": true,
  "composition_ok": true,
  "observed_subject": "What is actually visible",
  "reason": "Short explanation"
}}
"""

    last_error = None

    for attempt in range(1, 3):

        try:

            response = requests.post(
                f"{OLLAMA_URL}/api/chat",
                json={
                    "model": OLLAMA_MODEL,
                    "messages": [
                        {
                            "role": "user",
                            "content": prompt,
                            "images": [image_base64]
                        }
                    ],
                    "format": "json",
                    "stream": False,
                    "think": False,
                    "keep_alive": "10m",
                    "options": {
                        "temperature": 0.1,
                        "num_ctx": 8192,
                        "num_predict": 400
                    }
                },
                timeout=(15, VISION_TIMEOUT)
            )

            response.raise_for_status()
            data = response.json()

            content = (
                data.get("message", {}).get("content", "")
            )

            # Preserve the actual visual-inspection response.
            vision_debug_dir = (
                TEMP_ROOT / "vision_raw_responses"
            )
            vision_debug_dir.mkdir(
                parents=True,
                exist_ok=True
            )

            save_debug_text(
                vision_debug_dir
                / f"vision_{uuid.uuid4().hex[:12]}.txt",
                content
            )

            result = parse_json_response(content)

            if not isinstance(result.get("relevant"), bool):
                raise RuntimeError(
                    "Gemma did not return a boolean relevance decision."
                )

            composition_ok = result.get("composition_ok")
            if int(scene_index) == 1 and not isinstance(composition_ok, bool):
                raise RuntimeError(
                    "Opening-scene inspection omitted the required "
                    "composition_ok boolean."
                )
            if int(scene_index) != 1:
                composition_ok = True

            reason = str(result.get("reason", ""))
            if int(scene_index) == 1 and not composition_ok:
                reason = (
                    "Opening-scene composition rejected: " + reason
                )

            return {
                "relevant": bool(result["relevant"] and composition_ok),
                "composition_ok": composition_ok,
                "observed_subject": str(
                    result.get("observed_subject", "")
                ),
                "reason": reason
            }

        except Exception as exc:
            last_error = str(exc)

            print(
                f"[VISION] Inspection attempt {attempt}/2 failed: "
                f"{last_error}"
            )

    raise RuntimeError(
        "Gemma visual inspection failed after two attempts: "
        + str(last_error)
    )


def generate_and_verify_image(
    narration,
    expected_subject,
    visual_prompt,
    output_path,
    seed,
    task_id,
    scene_index
):

    current_prompt = visual_prompt
    previous_feedback = ""

    for attempt in range(1, MAX_IMAGE_ATTEMPTS + 1):

        task_log(
            task_id,
            f"Scene {scene_index}: image generation "
            f"{attempt}/{MAX_IMAGE_ATTEMPTS}."
        )

        if previous_feedback:
            current_prompt = (
                visual_prompt
                + ". CORRECTION: "
                + previous_feedback
                + ". Make the required subject large, "
                "central and unmistakably visible."
            )

        generate_scene_image(
            current_prompt,
            output_path,
            seed + attempt * 997,
            scene_index=scene_index,
            subject=expected_subject
        )

        task_log(
            task_id,
            f"Scene {scene_index}: inspecting actual image with Gemma."
        )

        inspection = inspect_image_relevance(
            output_path,
            narration,
            expected_subject,
            scene_index=scene_index
        )

        task_log(
            task_id,
            f"Scene {scene_index}: relevant={inspection['relevant']}; "
            f"observed={inspection['observed_subject']}; "
            f"reason={inspection['reason']}"
        )

        if inspection["relevant"]:
            return {
                "image": str(output_path),
                "attempts": attempt,
                "inspection": inspection
            }

        previous_feedback = (
            inspection["reason"]
            + ". Actual observed subject: "
            + inspection["observed_subject"]
            + ". Required subject: "
            + expected_subject
        )

    raise RuntimeError(
        f"Scene {scene_index} failed visual relevance validation "
        f"after {MAX_IMAGE_ATTEMPTS} attempts."
    )


# =====================================================================
# 10. SUBTITLES
# =====================================================================

def split_subtitle_text(text, max_words=9):

    sentences = re.split(
        r"(?<=[.!?؟؛])\s+",
        str(text).strip()
    )

    chunks = []

    for sentence in sentences:
        words = sentence.split()

        for start in range(0, len(words), max_words):
            part = " ".join(words[start:start + max_words])

            if part:
                chunks.append(part)

    return chunks


def srt_timestamp(seconds):

    milliseconds = int(round(seconds * 1000))

    hours = milliseconds // 3_600_000
    milliseconds %= 3_600_000

    minutes = milliseconds // 60_000
    milliseconds %= 60_000

    secs = milliseconds // 1000
    milliseconds %= 1000

    return (
        f"{hours:02d}:{minutes:02d}:{secs:02d},"
        f"{milliseconds:03d}"
    )


def create_srt(scene_records, output_path):

    cues = []
    global_time = 0.0
    cue_number = 1

    for record in scene_records:

        duration = float(record["audio_duration"])
        chunks = split_subtitle_text(record["narration"])

        if not chunks:
            global_time += duration
            continue

        weights = [
            max(1, count_words(chunk))
            for chunk in chunks
        ]

        total_weight = sum(weights)
        cursor = 0.0

        for chunk, weight in zip(chunks, weights):

            segment_duration = (
                duration * weight / total_weight
            )

            start = global_time + cursor
            end = global_time + cursor + segment_duration

            cues.append(
                f"{cue_number}\n"
                f"{srt_timestamp(start)} --> "
                f"{srt_timestamp(end)}\n"
                f"{chunk}\n"
            )

            cue_number += 1
            cursor += segment_duration

        global_time += duration

    output_path = Path(output_path)

    output_path.write_text(
        "\n".join(cues),
        encoding="utf-8"
    )

    if (
        not output_path.exists()
        or output_path.stat().st_size < 10
    ):
        raise RuntimeError("Subtitle generation failed.")

    return output_path


# =====================================================================
# 11. VIDEO SEGMENT RENDERING
# =====================================================================

def render_segment(animation_path, audio_path, output_path):
    """
    Render a genuine AI motion clip looped under narration.
    No still-image / zoompan fallback is permitted in V3.2.
    """
    duration = get_media_duration(audio_path)
    filter_complex = (
        "[0:v]scale=1920:1080:force_original_aspect_ratio=increase,"
        "crop=1920:1080,fps=30,format=yuv420p[v];"
        "[1:a]showwaves=s=1920:110:mode=line:colors=white:rate=30,"
        "format=rgba,colorchannelmixer=aa=0.65[w];"
        "[v][w]overlay=0:H-h-35:format=auto[vout]"
    )
    command = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-stream_loop", "-1", "-i", str(animation_path),
        "-i", str(audio_path),
        "-filter_complex", filter_complex,
        "-map", "[vout]", "-map", "1:a:0",
        "-t", f"{duration:.3f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
        "-pix_fmt", "yuv420p", "-r", str(VIDEO_FPS),
        "-c:a", "aac", "-b:a", "160k",
        "-shortest", "-movflags", "+faststart",
        str(output_path)
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            "Animated segment rendering failed:\\n" + result.stderr[-4000:]
        )
    if not output_path.exists() or output_path.stat().st_size < 20_000:
        raise RuntimeError("Animated segment is missing or too small.")
    return duration


# =====================================================================
# 12. FINAL VIDEO ASSEMBLY
# =====================================================================

def assemble_video(segment_paths, output_path, concat_file):

    def escape_concat_path(path):
        return str(Path(path).resolve()).replace("'", "'\\''")

    with open(concat_file, "w", encoding="utf-8") as file:
        for segment_path in segment_paths:
            file.write(
                f"file '{escape_concat_path(segment_path)}'\n"
            )

    command = [
        "ffmpeg", "-y", "-hide_banner",
        "-loglevel", "error",

        "-f", "concat",
        "-safe", "0",
        "-i", str(concat_file),

        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "22",
        "-pix_fmt", "yuv420p",
        "-r", str(VIDEO_FPS),

        "-c:a", "aac",
        "-b:a", "160k",

        "-movflags", "+faststart",

        str(output_path)
    ]

    result = subprocess.run(
        command,
        capture_output=True,
        text=True
    )

    if result.returncode != 0:
        raise RuntimeError(
            "Final video assembly failed:\n"
            + result.stderr[-5000:]
        )

    if (
        not output_path.exists()
        or output_path.stat().st_size < 20_000
    ):
        raise RuntimeError(
            "Final MP4 is missing or too small."
        )

    return output_path


# =====================================================================
# 13. EMBED SUBTITLES
# =====================================================================

def embed_subtitles(video_path, srt_path):

    video_path = Path(video_path)
    srt_path = Path(srt_path)

    temporary_output = video_path.with_name(
        video_path.stem + "_subtitled.mp4"
    )

    command = [
        "ffmpeg", "-y", "-hide_banner",
        "-loglevel", "error",

        "-i", str(video_path),
        "-i", str(srt_path),

        "-map", "0:v:0",
        "-map", "0:a:0",
        "-map", "1:0",

        "-c:v", "copy",
        "-c:a", "copy",
        "-c:s", "mov_text",

        "-metadata:s:s:0",
        "language=und",

        "-movflags", "+faststart",

        str(temporary_output)
    ]

    result = subprocess.run(
        command,
        capture_output=True,
        text=True
    )

    if result.returncode != 0:
        raise RuntimeError(
            "Could not embed subtitles into MP4:\n"
            + result.stderr[-4000:]
        )

    if (
        not temporary_output.exists()
        or temporary_output.stat().st_size < 20_000
    ):
        raise RuntimeError(
            "Subtitled MP4 is missing or invalid."
        )

    os.replace(temporary_output, video_path)

    return video_path


# =====================================================================
# 14. FINAL VIDEO VALIDATION
# =====================================================================

def validate_final_video(video_path, task_id, requested_minutes, animation_checks=None):

    animation_checks = animation_checks or []
    if not animation_checks:
        raise RuntimeError(
            "V3.2 animation validation failed: no AI motion clips were recorded."
        )
    failed_motion = [
        item for item in animation_checks
        if item.get("motion_validation") != "PASS"
        or float(item.get("mean_frame_difference", 0)) < 0.75
    ]
    if failed_motion:
        raise RuntimeError(
            "V3.2 animation validation failed for scene(s): "
            + ", ".join(str(x.get("scene")) for x in failed_motion)
        )

    probe = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_streams",
            "-show_format",
            "-of", "json",
            str(video_path)
        ],
        capture_output=True,
        text=True,
        check=True
    )

    info = json.loads(probe.stdout)
    streams = info.get("streams", [])

    video_streams = [
        s for s in streams if s.get("codec_type") == "video"
    ]

    audio_streams = [
        s for s in streams if s.get("codec_type") == "audio"
    ]

    subtitle_streams = [
        s for s in streams if s.get("codec_type") == "subtitle"
    ]

    if not video_streams:
        raise RuntimeError("Final video has no video stream.")

    if not audio_streams:
        raise RuntimeError("Final video has no audio stream.")

    if not subtitle_streams:
        raise RuntimeError(
            "Final video has no embedded subtitle stream."
        )

    video_stream = video_streams[0]

    width = int(video_stream["width"])
    height = int(video_stream["height"])
    duration = float(info["format"]["duration"])

    if (width, height) != (VIDEO_WIDTH, VIDEO_HEIGHT):
        raise RuntimeError(
            f"Wrong video resolution: {width}x{height}"
        )

    if not math.isfinite(duration):
        raise RuntimeError("Invalid video duration.")

    requested_seconds = requested_minutes * 60

    # Permit modest natural speech-duration variation,
    # but do not accept a video outside the global 3–10 min range.
    minimum_seconds = max(
        MIN_DURATION_MINUTES * 60,
        requested_seconds * 0.90
    )

    maximum_seconds = min(
        MAX_DURATION_MINUTES * 60,
        requested_seconds * 1.20
    )

    if not minimum_seconds <= duration <= maximum_seconds:
        raise RuntimeError(
            f"Actual video duration is {duration:.1f}s. "
            f"For a {requested_minutes}-minute request, "
            f"accepted range is {minimum_seconds:.1f}–"
            f"{maximum_seconds:.1f}s."
        )

    # Full decode, not just a metadata check.
    decode = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-i", str(video_path),
            "-f", "null", "-"
        ],
        capture_output=True,
        text=True
    )

    if decode.returncode != 0:
        raise RuntimeError(
            "Full MP4 decode failed:\n"
            + decode.stderr[-4000:]
        )

    frame_path = (
        TEMP_ROOT / f"{task_id}_validation_frame.png"
    )

    frame_result = subprocess.run(
        [
            "ffmpeg", "-y",
            "-hide_banner", "-loglevel", "error",
            "-ss", str(min(10.0, duration / 2)),
            "-i", str(video_path),
            "-frames:v", "1",
            str(frame_path)
        ],
        capture_output=True,
        text=True
    )

    if frame_result.returncode != 0 or not frame_path.exists():
        raise RuntimeError(
            "Could not extract final validation frame."
        )

    with Image.open(frame_path) as frame:
        frame = frame.convert("RGB")
        pixels = np.asarray(frame)

        if frame.size != (VIDEO_WIDTH, VIDEO_HEIGHT):
            raise RuntimeError(
                "Final frame has incorrect dimensions."
            )

        pixel_std = float(pixels.std())

    if pixel_std < 5:
        raise RuntimeError(
            "Final video frame appears blank or uniform."
        )

    return {
        "duration_seconds": round(duration, 2),
        "requested_duration_minutes": requested_minutes,
        "resolution": f"{width}x{height}",
        "video_codec": video_stream.get("codec_name"),
        "audio_codec": audio_streams[0].get("codec_name"),
        "subtitle_codec": subtitle_streams[0].get("codec_name"),
        "frame_rate": video_stream.get("r_frame_rate"),
        "file_size_bytes": video_path.stat().st_size,
        "frame_pixel_std": round(pixel_std, 2),
        "subtitle_stream": "PASS",
        "animation_backend": ANIMATION_BACKEND,
        "animated_scenes": len(animation_checks),
        "motion_validation": "PASS",
        "full_decode": "PASS"
    }


# =====================================================================
# 15. VIDEO PROCESSING
# =====================================================================

def _process_video_task(task_id, request_data):

    task_start = time.time()

    task_dir = TEMP_ROOT / task_id
    task_dir.mkdir(parents=True, exist_ok=True)

    final_video = OUTPUT_DIR / f"{task_id}.mp4"
    final_srt = OUTPUT_DIR / f"{task_id}.srt"

    user_prompt = request_data["prompt"]
    duration_minutes = request_data["duration_minutes"]
    language = request_data["language"]
    voice = request_data["voice"]

    task_log(
        task_id,
        f"Started. Language={language}; "
        f"duration={duration_minutes} minutes; voice={voice}."
    )

    try:

        # STEP 1 — EDUCATIONAL LESSON

        task_progress(
            task_id,
            3,
            "scenario",
            "Generating the educational lesson."
        )

        scenario = generate_scenario(
            user_prompt,
            duration_minutes,
            language
        )

        scenes = scenario["scenes"]

        update_task(
            task_id,
            title=scenario["title"],
            total_scenes=len(scenes),
            total_words=scenario["total_words"],
            scenario=scenario
        )

        with open(
            task_dir / "scenario.json",
            "w",
            encoding="utf-8"
        ) as file:
            json.dump(
                scenario,
                file,
                ensure_ascii=False,
                indent=2
            )

        task_progress(
            task_id,
            8,
            "scenario",
            f"Lesson ready: {len(scenes)} scenes, "
            f"{scenario['total_words']} words."
        )

        segment_videos = []
        scene_records = []
        image_checks = []
        animation_checks = []
        audio_timeline = []

        # STEP 2 — EACH SCENE

        for index, scene in enumerate(scenes, start=1):

            prefix = f"scene_{index:02d}"

            narration = scene["narration"]
            subject = scene["visual_subject"]
            visual_prompt = scene["visual_prompt_en"]

            image_path = task_dir / f"{prefix}.png"
            audio_path = task_dir / f"{prefix}.mp3"
            video_path = task_dir / f"{prefix}.mp4"
            animation_path = task_dir / f"{prefix}_animation.mp4"

            base_progress = 8 + int(
                (index - 1) / len(scenes) * 78
            )

            # AUDIO

            task_progress(
                task_id,
                base_progress,
                "voice",
                f"Scene {index}/{len(scenes)}: "
                "generating neural narration."
            )

            audio_result = synthesize_audio(
                narration,
                audio_path,
                voice
            )

            audio_duration = audio_result["duration"]

            task_log(
                task_id,
                f"Scene {index}: narration ready, "
                f"{audio_duration:.2f}s."
            )

            # IMAGE + GEMMA INSPECTION

            task_progress(
                task_id,
                base_progress + 2,
                "image",
                f"Scene {index}/{len(scenes)}: "
                "generating matching illustration."
            )

            seed = (
                int(task_id.replace("-", "")[:8], 16)
                + index
            )

            image_result = generate_and_verify_image(
                narration=narration,
                expected_subject=subject,
                visual_prompt=visual_prompt,
                output_path=image_path,
                seed=seed,
                task_id=task_id,
                scene_index=index
            )

            image_checks.append({
                "scene": index,
                "expected_subject": subject,
                "observed_subject": (
                    image_result["inspection"]["observed_subject"]
                ),
                "relevance": "PASS",
                "attempts": image_result["attempts"],
                "reason": image_result["inspection"]["reason"]
            })

            # RENDER

            task_progress(
                task_id,
                base_progress + 5,
                "render",
                f"Scene {index}/{len(scenes)}: "
                "rendering synchronized video."
            )

            animation_result = generate_animated_scene(
                visual_prompt=visual_prompt,
                subject=subject,
                output_path=animation_path,
                seed=seed + 50000,
                task_id=task_id,
                scene_index=index
            )

            animation_inspection = inspect_image_relevance(
                animation_result["first_frame_path"],
                narration,
                subject,
                scene_index=index
            )
            if not animation_inspection.get("relevant"):
                raise RuntimeError(
                    f"Scene {index} AnimateDiff frame failed relevance "
                    f"inspection: {animation_inspection.get('reason')}"
                )

            animation_checks.append({
                "scene": index,
                "first_frame_relevance": animation_inspection,
                **animation_result["motion_audit"]
            })

            audio_timeline.append({
                "scene": index,
                "audio_duration_seconds": round(audio_duration, 3)
            })

            rendered_duration = render_segment(
                animation_path,
                audio_path,
                video_path
            )

            segment_videos.append(video_path)

            scene_records.append({
                "narration": narration,
                "audio_duration": rendered_duration,
                "image": str(image_path),
                "animation": str(animation_path),
                "animation_audit": animation_result["motion_audit"],
                "audio": str(audio_path),
                "video": str(video_path),
                "visual_subject": subject
            })

            update_task(
                task_id,
                completed_segments=index,
                image_checks=image_checks
            )

            task_log(
                task_id,
                f"Scene {index} complete: "
                f"{rendered_duration:.2f}s."
            )

        # STEP 3 — SUBTITLES

        task_progress(
            task_id,
            88,
            "subtitles",
            "Creating synchronized subtitles."
        )

        task_srt = task_dir / "subtitles.srt"

        create_srt(scene_records, task_srt)
        shutil.copy2(task_srt, final_srt)

        # STEP 4 — ASSEMBLY

        task_progress(
            task_id,
            90,
            "assembly",
            "Combining scene videos."
        )

        concat_file = task_dir / "concat_list.txt"

        assemble_video(
            segment_videos,
            final_video,
            concat_file
        )

        # STEP 5 — EMBED SUBTITLES

        task_progress(
            task_id,
            93,
            "subtitles",
            "Embedding subtitles into MP4."
        )

        embed_subtitles(final_video, task_srt)

        # STEP 6 — VALIDATION

        task_progress(
            task_id,
            96,
            "validation",
            "Validating final MP4 and media streams."
        )

        audio_timeline_path = task_dir / "audio_timeline.json"
        save_debug_text(
            audio_timeline_path,
            json.dumps(
                {
                    "requested_duration_seconds": duration_minutes * 60,
                    "scene_durations": audio_timeline,
                    "sum_scene_audio_seconds": round(
                        sum(x["audio_duration_seconds"] for x in audio_timeline), 3
                    ),
                    "final_render_duration_seconds": round(
                        get_media_duration(final_video), 3
                    )
                },
                indent=2
            )
        )

        validation = validate_final_video(
            final_video,
            task_id,
            duration_minutes,
            animation_checks=animation_checks
        )

        elapsed = time.time() - task_start

        audit_path = OUTPUT_DIR / (
            f"{task_id}_visual_audit.json"
        )

        audit_path.write_text(
            json.dumps(
                image_checks,
                ensure_ascii=False,
                indent=2
            ),
            encoding="utf-8"
        )

        animation_audit_path = OUTPUT_DIR / (
            f"{task_id}_animation_audit.json"
        )
        animation_audit_path.write_text(
            json.dumps(
                animation_checks,
                ensure_ascii=False,
                indent=2
            ),
            encoding="utf-8"
        )

        # STEP 7 — COMPLETE

        update_task(
            task_id,
            status="completed",
            progress=100,
            stage="completed",
            message="Video generated and technically validated.",
            output=str(final_video),
            subtitle_output=str(final_srt),
            visual_audit=str(audit_path),
            animation_audit=str(animation_audit_path),
            download_url=f"/download/{task_id}",
            subtitle_url=f"/subtitles/{task_id}",
            validation=validation,
            image_checks=image_checks,
            animation_checks=animation_checks,
            audio_timeline=str(audio_timeline_path),
            elapsed_seconds=round(elapsed, 2),
            completed_at=datetime.now().astimezone().isoformat()
        )

        task_log(
            task_id,
            f"COMPLETED: {final_video.name}; "
            f"{validation['duration_seconds']} seconds; "
            f"{validation['file_size_bytes']:,} bytes."
        )

        print(
            f"\n[VIDEO COMPLETE] {final_video}",
            flush=True
        )

        if not KEEP_TASK_ARTIFACTS:
            shutil.rmtree(task_dir, ignore_errors=True)

    except Exception as error:

        error_text = str(error)
        full_traceback = traceback.format_exc()

        print(
            f"\n[VIDEO FAILED] Task {task_id}\n{full_traceback}",
            flush=True
        )

        update_task(
            task_id,
            status="failed",
            progress=0,
            stage="failed",
            message=error_text,
            error=error_text,
            traceback=full_traceback,
            elapsed_seconds=round(
                time.time() - task_start,
                2
            ),
            debug_directory=str(task_dir)
        )

        task_log(
            task_id,
            f"FAILED: {error_text}. "
            f"Debug files: {task_dir}"
        )


def process_video_task(task_id, request_data):

    task_log(
        task_id,
        "Waiting for the video processing slot."
    )

    with VIDEO_TASK_LOCK:
        _process_video_task(task_id, request_data)


# =====================================================================
# 16. API ENDPOINTS — PRESERVED
# =====================================================================

@app.get("/health")
def health():

    return {
        "status": "ready",
        "version": APP_VERSION,
        "build_id": APP_BUILD_ID,
        "ollama": OLLAMA_URL,
        "gemma": OLLAMA_MODEL,
        "dreamshaper": "ready",
        "animation_backend": ANIMATION_BACKEND,
        "animation_device": ANIMATION_DEVICE,
        "dreamshaper_device": DEVICE,
        "video_resolution": f"{VIDEO_WIDTH}x{VIDEO_HEIGHT}",
        "fps": VIDEO_FPS,
        "languages": ["ar", "en"],
        "default_language": "ar",
        "default_duration_minutes": DEFAULT_DURATION_MINUTES,
        "maximum_duration_minutes": MAX_DURATION_MINUTES,
        "image_relevance_check": "Gemma vision",
        "voice_engine": "Edge neural TTS",
        "timestamp": datetime.now().astimezone().isoformat()
    }


@app.post("/generate")
def generate_video(
    request: VideoRequest,
    background_tasks: BackgroundTasks
):

    language = request.language.strip().lower()

    if language not in VOICE_OPTIONS:
        raise HTTPException(
            status_code=400,
            detail="language must be 'ar' or 'en'."
        )

    try:
        voice = safe_voice(language, request.voice)
    except ValueError as error:
        raise HTTPException(
            status_code=400,
            detail=str(error)
        )

    if not request.prompt.strip():
        raise HTTPException(
            status_code=400,
            detail="Prompt cannot be empty."
        )

    task_id = str(uuid.uuid4())
    now = datetime.now().astimezone().isoformat()

    request_data = {
        "prompt": request.prompt.strip(),
        "duration_minutes": request.duration_minutes,
        "language": language,
        "voice": voice
    }

    with TASKS_LOCK:
        TASKS[task_id] = {
            "task_id": task_id,
            "status": "queued",
            "progress": 0,
            "stage": "queued",
            "message": "Educational video queued.",
            "prompt": request.prompt,
            "duration_minutes": request.duration_minutes,
            "language": language,
            "voice": voice,
            "created_at": now,
            "updated_at": now,
            "logs": [],
            "completed_segments": 0,
            "total_scenes": math.ceil(
                request.duration_minutes * 60
                / SECONDS_PER_SCENE
            )
        }

    background_tasks.add_task(
        process_video_task,
        task_id,
        request_data
    )

    return {
        "success": True,
        "task_id": task_id,
        "status": "queued",
        "status_url": f"/status/{task_id}",
        "download_url": f"/download/{task_id}",
        "subtitle_url": f"/subtitles/{task_id}",
        "message": "Educational video generation accepted."
    }


@app.get("/status/{task_id}")
def video_status(task_id: str):

    task = get_task(task_id)

    if task is None:
        raise HTTPException(
            status_code=404,
            detail="Task not found."
        )

    task.pop("traceback", None)

    return task


@app.get("/download/{task_id}")
def download_video(task_id: str):

    task = get_task(task_id)

    if task is None:
        raise HTTPException(
            status_code=404,
            detail="Task not found."
        )

    if task.get("status") != "completed":
        raise HTTPException(
            status_code=409,
            detail=(
                "Video is not ready. Current status: "
                + str(task.get("status"))
            )
        )

    video_path = Path(task.get("output", ""))

    if not video_path.is_file():
        raise HTTPException(
            status_code=404,
            detail="Completed video file is missing."
        )

    return FileResponse(
        path=str(video_path),
        media_type="video/mp4",
        filename=f"Educational_Video_{task_id[:8]}.mp4"
    )


@app.get("/subtitles/{task_id}")
def download_subtitles(task_id: str):

    task = get_task(task_id)

    if task is None:
        raise HTTPException(
            status_code=404,
            detail="Task not found."
        )

    if task.get("status") != "completed":
        raise HTTPException(
            status_code=409,
            detail="Subtitles are not ready."
        )

    subtitle_path = Path(
        task.get("subtitle_output", "")
    )

    if not subtitle_path.is_file():
        raise HTTPException(
            status_code=404,
            detail="Subtitle file is missing."
        )

    return FileResponse(
        path=str(subtitle_path),
        media_type="application/x-subrip",
        filename=f"Educational_Subtitles_{task_id[:8]}.srt"
    )


# =====================================================================
# 17. SERVER STARTUP — PREVENT STALE V3.0 CODE
# =====================================================================

PORT = 8000

SERVER_THREAD = globals().get("SERVER_THREAD")
SERVER = globals().get("SERVER")


def port_is_open(port):

    with socket.socket(
        socket.AF_INET,
        socket.SOCK_STREAM
    ) as sock:
        return sock.connect_ex(("127.0.0.1", port)) == 0


if port_is_open(PORT):

    existing_health = None

    try:
        existing_health = requests.get(
            f"http://127.0.0.1:{PORT}/health",
            timeout=3
        )
    except requests.RequestException:
        pass

    existing_build_id = None

    if existing_health is not None and existing_health.ok:
        try:
            existing_build_id = (
                existing_health.json().get("build_id")
            )
        except Exception:
            existing_build_id = None

    if existing_build_id == APP_BUILD_ID:

        print("This exact V3.2.2 build is already running.")

    else:

        # Only stop a server that this notebook owns.
        if SERVER is not None and hasattr(SERVER, "should_exit"):

            print("Stopping previous notebook API server...")

            SERVER.should_exit = True

            if SERVER_THREAD is not None:
                SERVER_THREAD.join(timeout=20)

            deadline = time.time() + 20

            while time.time() < deadline:

                if not port_is_open(PORT):
                    break

                time.sleep(1)

        if port_is_open(PORT):
            raise RuntimeError(
                f"Port {PORT} is still occupied by a server "
                "that this cell cannot safely stop. "
                "Restart the Kaggle runtime, rerun Cell 1, "
                "then run this Cell 2 again. "
                "Do not run a second server on the same port."
            )

        SERVER = None
        SERVER_THREAD = None


if not port_is_open(PORT):

    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=PORT,
        log_level="info",
        access_log=True
    )

    SERVER = uvicorn.Server(config)

    SERVER_THREAD = threading.Thread(
        target=SERVER.run,
        daemon=True,
        name="Educational-AI-Video-API"
    )

    SERVER_THREAD.start()

    deadline = time.time() + 60
    ready = False

    while time.time() < deadline:

        if SERVER.should_exit:
            raise RuntimeError(
                "Uvicorn server stopped unexpectedly."
            )

        try:
            response = requests.get(
                f"http://127.0.0.1:{PORT}/health",
                timeout=2
            )

            if (
                response.ok
                and response.json().get("build_id") == APP_BUILD_ID
            ):
                ready = True
                break

        except requests.RequestException:
            pass

        time.sleep(1)

    if not ready:
        raise RuntimeError(
            "FastAPI server failed to become ready within 60 seconds."
        )


globals()["SERVER"] = SERVER
globals()["SERVER_THREAD"] = SERVER_THREAD


# =====================================================================
# 18. NGROK PUBLIC ACCESS
# =====================================================================

PUBLIC_URL = None

if NGROK_AUTHTOKEN:

    from pyngrok import ngrok

    try:

        existing_tunnels = ngrok.get_tunnels()
        matching = []

        for tunnel in existing_tunnels:

            address = str(
                tunnel.config.get("addr", "")
            ).strip()

            if (
                address == str(PORT)
                or address.endswith(f":{PORT}")
            ):
                matching.append(tunnel)

        if matching:

            PUBLIC_URL = matching[0].public_url.rstrip("/")
            print("Reusing existing ngrok tunnel.")

        else:

            tunnel = ngrok.connect(
                addr=PORT,
                proto="http"
            )

            PUBLIC_URL = tunnel.public_url.rstrip("/")
            print("Created new ngrok tunnel.")

        public_health = requests.get(
            f"{PUBLIC_URL}/health",
            timeout=30
        )

        print("PUBLIC URL:", PUBLIC_URL)
        print("PUBLIC HEALTH STATUS:", public_health.status_code)

        if not public_health.ok:
            raise RuntimeError("Public health check failed.")

        public_data = public_health.json()

        if public_data.get("build_id") != APP_BUILD_ID:
            raise RuntimeError(
                "Public URL is pointing to a different server build."
            )

        print("PUBLIC HEALTH:", public_data)
        print("NGROK ACCESS: PASS")

    except Exception as error:

        PUBLIC_URL = None

        print(
            "[NGROK WARNING]",
            type(error).__name__,
            str(error),
            flush=True
        )

        print(
            f"Local API remains available at "
            f"http://127.0.0.1:{PORT}"
        )

else:

    print("[NGROK WARNING] No ngrok token available.")


# =====================================================================
# 19. FINAL HEALTH VERIFICATION
# =====================================================================

health_response = requests.get(
    f"http://127.0.0.1:{PORT}/health",
    timeout=5
)

health_response.raise_for_status()

health_data = health_response.json()

if health_data.get("version") != APP_VERSION:
    raise RuntimeError("Unexpected API version.")

if health_data.get("build_id") != APP_BUILD_ID:
    raise RuntimeError("Server build ID does not match this cell.")

print("\n" + "=" * 72)
print("EDUCATIONAL AI VIDEO GENERATOR V3.2.3")
print("=" * 72)

print("Version:", APP_VERSION)
print("Build ID:", APP_BUILD_ID)
print("Local API:", f"http://127.0.0.1:{PORT}")
print("Public API:", PUBLIC_URL or "Unavailable")
print("Health:", f"http://127.0.0.1:{PORT}/health")

print("\nAPI ROUTES")
print("POST /generate")
print("GET  /status/{task_id}")
print("GET  /download/{task_id}")
print("GET  /subtitles/{task_id}")

print("\nGENERATION SETTINGS")
print("Default language: Arabic")
print("Alternative language: English")
print("Default duration: 3 minutes")
print("Maximum duration: 10 minutes")
print("Voice engine: Edge neural TTS")
print("Narrator consistency: One voice per video")
print("Image generation: DreamShaper / GPU 1")
print("Animation generation: AnimateDiff / GPU 0")
print("Visual relevance inspection: Gemma / Ollama")
print("Animation backend:", ANIMATION_BACKEND)
print("Animation model:", ANIMATION_MODEL_ID)
print("Motion adapter:", MOTION_ADAPTER_ID)
print("Automatic image regeneration:", MAX_IMAGE_ATTEMPTS)
print("Video: 1920x1080 / 30 FPS")
print("Subtitles: Embedded MP4 + downloadable SRT")
print("Concurrent video jobs: 1")
print("Animated scene frames:", ANIMATION_FRAMES)
print("Motion validation: enabled")
print("Preserve intermediates:", KEEP_TASK_ARTIFACTS)
print("Output directory:", str(OUTPUT_DIR))

print("=" * 72)

print("\nHEALTH RESPONSE:")
print(
    json.dumps(
        health_data,
        indent=2,
        ensure_ascii=False
    )
)

log(
    "Educational AI Video Generator V3.2.3 initialized. "
    "Local health check passed."
)

print("\nSERVER STARTUP CHECK: PASS")
print(
    "NOTE: This confirms server startup only. "
    "Actual video generation, TTS, image inspection, "
    "subtitles and rendering still require an end-to-end test."
)
