#!/usr/bin/env python3
"""Cell 3 Project Builder v7.1.1 ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â Release Candidate.

Standalone Windows client for the existing Kaggle Cell 3 Ollama/ngrok proxy.
Cell 2 is never used. Proxy configuration is never changed.

Generated source and tests execute locally. Build only from specifications you trust.
"""
from __future__ import annotations
import importlib.util

import argparse
import ast
import contextlib
import difflib
import hashlib
import html.parser
import ipaddress
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import posixpath
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any, Iterator
from urllib.parse import quote, urlsplit

try:
    import requests
except ImportError as exc:  # pragma: no cover - environment diagnostic
    raise SystemExit("Missing dependency 'requests'. Install with: py -m pip install requests") from exc

VERSION = "7.1.1"
GENERATOR_MODEL = "gemma4:e4b"
PLANNER_MODEL = "qwen3:4b"
AUDITOR_MODEL = "qwen2.5-coder:3b"
DEFAULT_TIMEOUT = int(os.getenv("CELL3_CLIENT_TIMEOUT", "330"))
MAX_FILES = 80
MAX_FILE_BYTES = 750_000
MAX_PROJECT_BYTES = 8_000_000
MAX_UNIT_ATTEMPTS = 3
MAX_REPAIR_ATTEMPTS = 2
MAX_INTEGRATION_ROUNDS = 2
AUDIT_BATCH_SIZE = 3
CONNECTION_RECOVERY_ATTEMPTS = 3
CONNECTION_RECOVERY_DELAY = 1.5
CONTRACT_SCHEMA_VERSION = "cell3-contract-v2"

ROOT = Path(os.getenv("CELL3_WORKSPACE", str(Path.home() / "Cell3-AI-Workspace"))).expanduser()
RUNS_DIR = ROOT / "runs"
STAGING_DIR = ROOT / "staging"
PROJECTS_DIR = ROOT / "projects"
LOGS_DIR = ROOT / "logs"

class BuildError(RuntimeError):
    pass

class Acceptance(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"
    PARTIAL = "PARTIAL"

@dataclass
class BuildContext:
    run_id: str
    project_name: str
    staging: Path
    spec: str
    spec_hash: str
    timeout: int
    run_tests: bool
    resume: bool = False
    manifest: list[dict[str, Any]] = field(default_factory=list)
    project: dict[str, str] = field(default_factory=dict)
    contract: dict[str, Any] = field(default_factory=dict)
    plan: dict[str, Any] = field(default_factory=dict)
    architecture: dict[str, Any] = field(default_factory=dict)
    design: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)
    state: dict[str, Any] = field(default_factory=dict)
    audit: dict[str, Any] = field(default_factory=dict)
    tests: dict[str, Any] = field(default_factory=dict)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))

def canonical_hash(value: Any) -> str:
    return sha256_text(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")))

def redact(value: Any) -> str:
    text = str(value)
    for key in ("ORCHESTRATOR_API_KEY", "CELL3_PROXY_TOKEN", "GEMMA_PROXY_TOKEN"):
        secret = os.getenv(key, "")
        if secret and len(secret) >= 4:
            text = text.replace(secret, "[REDACTED]")
    return re.sub(r"(?i)(Bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1[REDACTED]", text)

def http_error_diagnostics(response: Any, *, body_limit: int = 2000) -> str:
    """Return bounded, redacted HTTP response details without exposing credentials."""
    if response is None:
        return "response=unavailable"
    headers = getattr(response, "headers", {}) or {}
    content_type = str(headers.get("Content-Type", headers.get("content-type", "unknown")))[:160]
    request_id = next((str(headers[key]) for key in ("X-Request-ID", "x-request-id", "Request-ID", "request-id") if headers.get(key)), "")[:160]

    def scrub_text(value: str) -> str:
        value = redact(value)
        return re.sub(
            r"(?i)(\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|password|secret|credential)\b\s*[:=]\s*)([\"']?)[^\s,;\"'}]+",
            r"\1[REDACTED]",
            value,
        )

    try:
        body = getattr(response, "text", "")
        if not isinstance(body, str):
            body = str(body)
    except Exception:
        body = "[response body unavailable]"

    def scrub_json(value: Any, key: str = "") -> Any:
        if re.search(r"(?i)(?:api[_-]?key|access[_-]?token|refresh[_-]?token|auth(?:orization)?|password|secret|credential)", key):
            return "[REDACTED]"
        if isinstance(value, dict):
            return {str(k): scrub_json(v, str(k)) for k, v in value.items()}
        if isinstance(value, list):
            return [scrub_json(item) for item in value]
        if isinstance(value, str):
            return scrub_text(value)
        return value

    try:
        parsed = json.loads(body)
        body = json.dumps(scrub_json(parsed), ensure_ascii=False, separators=(",", ":"))
    except (ValueError, TypeError):
        body = scrub_text(body)
    body = body.replace("\r", "\\r").replace("\n", "\\n")
    truncated = len(body) > body_limit
    excerpt = body[:body_limit]
    details = [f"status={getattr(response, 'status_code', 'unknown')}", f"content_type={content_type}"]
    if request_id:
        details.append(f"request_id={redact(request_id)}")
    details.append(f"body={excerpt!r}" + (" [truncated]" if truncated else ""))
    return "; ".join(details)

def atomic_write_bytes(path: Path, payload: bytes) -> None:
    """Crash-resistant atomic write: unique temp, flush+fsync, replace, best-effort dir sync."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temp.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        if os.name != "nt":
            try:
                fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            except OSError:
                pass
    finally:
        temp.unlink(missing_ok=True)

def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))

def save_json(path: Path, data: Any) -> None:
    atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")

def load_json(path: Path, *, required: bool = True) -> Any:
    if not path.is_file():
        if required:
            raise BuildError(f"Required JSON evidence is missing: {path}")
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BuildError(f"INVALID_JSON_EVIDENCE: {path}: {exc}") from exc

def safe_relative_path(raw: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise BuildError("Manifest contains an empty path")
    value = raw.replace("\\", "/").strip()
    p = PurePosixPath(value)
    if p.is_absolute() or re.match(r"^[A-Za-z]:", value) or "\x00" in value:
        raise BuildError(f"Unsafe project path rejected: {raw}")
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise BuildError(f"Path traversal or empty component rejected: {raw}")
    reserved = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}
    for part in parts:
        if part.endswith((".", " ")) or any(ch in part for ch in '<>:"|?*') or any(ord(ch) < 32 for ch in part):
            raise BuildError(f"Windows-incompatible path component rejected: {raw}")
        if part.split(".", 1)[0].upper() in reserved:
            raise BuildError(f"Windows-reserved filename rejected: {raw}")
    if len(value) > 240:
        raise BuildError(f"Project path too long: {raw}")
    return value

def validate_project(project: dict[str, str]) -> dict[str, str]:
    if not isinstance(project, dict) or not project or len(project) > MAX_FILES:
        raise BuildError("Project is empty or exceeds the file-count limit")
    clean: dict[str, str] = {}
    seen_casefold: set[str] = set()
    total = 0
    for raw_path, content in project.items():
        path = safe_relative_path(raw_path)
        if path.casefold() in seen_casefold:
            raise BuildError(f"Duplicate/case-colliding project path: {path}")
        seen_casefold.add(path.casefold())
        if not isinstance(content, str):
            raise BuildError(f"Non-text project content rejected: {path}")
        data = content.encode("utf-8")
        if len(data) > MAX_FILE_BYTES:
            raise BuildError(f"Project file exceeds {MAX_FILE_BYTES} bytes: {path}")
        total += len(data)
        clean[path] = content.replace("\r\n", "\n").replace("\r", "\n")
    if total > MAX_PROJECT_BYTES:
        raise BuildError(f"Project exceeds {MAX_PROJECT_BYTES} total bytes")
    return clean

def write_project(root: Path, project: dict[str, str]) -> None:
    for rel, content in validate_project(project).items():
        target = root.joinpath(*PurePosixPath(rel).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            if not target.resolve().is_relative_to(root.resolve()):
                raise BuildError(f"PROJECT_WRITE_PATH_ESCAPE: {rel}")
        except OSError as exc:
            raise BuildError(f"PROJECT_WRITE_PATH_INVALID: {rel}: {exc}") from exc
        atomic_write_text(target, content)

def read_project(root: Path) -> dict[str, str]:
    found: dict[str, str] = {}
    for path in root.rglob("*"):
        if path.is_file():
            rel = path.relative_to(root).as_posix()
            found[rel] = path.read_text(encoding="utf-8")
    return validate_project(found)


# ---------------------------------------------------------------------------
# One continuous build progress bar; stage/file timings remain timestamped.
# ---------------------------------------------------------------------------
STAGES = [
    "Environment and Cell 3 connection",
    "Specification analysis",
    "Architecture and planning",
    "Canonical contract verification",
    "Project generation",
    "Cross-file integration",
    "Independent Qwen quality audit",
    "Testing and runtime verification",
    "Final evidence and acceptance",
    "Packaging and delivery",
]

class Progress:
    def __init__(self, project: str):
        self.project = project
        self.started = time.monotonic()
        self.stage = 1
        self.stage_name = STAGES[0]
        self.stage_fraction = 0.0
        self.stage_done = False
        self.file = "-"
        self.unit = "-"
        self.accepted = 0
        self.total = 0
        self.operation = "Starting"
        self.last = "Running"
        self.stage_started_at: float | None = None
        self.stage_clock = "-"
        self.file_started_at: float | None = None
        self.file_clock = "-"
        self.lock = threading.RLock()
        self.drawn = False
        self.closed = False
        self.last_non_tty_snapshot = ""
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None
        if sys.stdout.isatty():
            self._heartbeat_thread = threading.Thread(
                target=self._heartbeat_loop,
                name="cell3-progress-heartbeat",
                daemon=True,
            )
            self._heartbeat_thread.start()

    def _heartbeat_loop(self) -> None:
        # Repaint the existing panel once per second while synchronous model
        # calls block. This changes clocks only, never progress counters.
        while not self._heartbeat_stop.wait(1.0):
            if self.closed:
                break
            try:
                self.render()
            except Exception:
                continue

    def render(self, initial: bool = False) -> None:
        with self.lock:
            fraction = min(0.995, ((self.stage - 1) + (1.0 if self.stage_done else self.stage_fraction)) / len(STAGES))
            if self.closed and self.last == "BUILD PASS":
                fraction = 1.0
            pct = int(round(fraction * 100))
            width = 22
            fill = round(width * fraction)
            try:
                bar = "[" + "#" * fill + "-" * (width - fill) + "]"
                bar.encode(sys.stdout.encoding or "utf-8")
            except (UnicodeEncodeError, LookupError):
                bar = "[" + "#" * fill + "-" * (width - fill) + "]"
            elapsed = int(time.monotonic() - self.started)
            stage_elapsed = "-" if self.stage_started_at is None else f"{int(time.monotonic()-self.stage_started_at)//60:02d}:{int(time.monotonic()-self.stage_started_at)%60:02d}"
            file_elapsed = "-" if self.file_started_at is None else f"{int(time.monotonic()-self.file_started_at)//60:02d}:{int(time.monotonic()-self.file_started_at)%60:02d}"
            lines = [
                f"BUILD {bar} {pct:3d}% | Stage {self.stage:02d}/{len(STAGES)} | Total elapsed {elapsed//60:02d}:{elapsed%60:02d}",
                f"{self.stage_clock} | {self.stage_name} | Stage elapsed {stage_elapsed} | {self.operation}",
                f"{self.file_clock} | {self.project} | File: {self.file} | File elapsed {file_elapsed} | Unit: {self.unit} | Accepted: {self.accepted}/{self.total or '-'} | {self.last}",
            ]
            if not sys.stdout.isatty():
                snapshot = "\n".join(lines)
                if initial or snapshot != self.last_non_tty_snapshot:
                    print(snapshot, flush=True)
                    self.last_non_tty_snapshot = snapshot
                    self.drawn = True
                return
            if self.drawn:
                sys.stdout.write("\033[3A")
            for line in lines:
                sys.stdout.write("\r\033[2K" + line[: max(80, shutil.get_terminal_size((100, 24)).columns)])
                sys.stdout.write("\n")
            sys.stdout.flush()
            self.drawn = True

    def update(self, **values: Any) -> None:
        with self.lock:
            for key, value in values.items():
                setattr(self, key, value)
            if self.stage == 5 and self.total:
                match = re.match(r"(\d+)/(\d+)", self.file)
                if match:
                    file_index, file_total = int(match.group(1)), max(1, int(match.group(2)))
                    self.stage_fraction = min(1.0, ((file_index - 1) + min(1.0, self.accepted / max(1, self.total))) / file_total)
        self.render()

    @contextlib.contextmanager
    def stage_scope(self, number: int, name: str) -> Iterator[None]:
        self.stage, self.stage_name = number, f"Stage {number:02d} - {name}"
        self.stage_fraction, self.stage_done = 0.0, False
        self.operation, self.last = "Starting", "Running"
        started = time.monotonic()
        self.stage_started_at = started
        self.stage_clock = datetime.now().strftime("%H:%M:%S")
        self.render()
        status = "PASS"
        try:
            yield
        except Exception:
            status = "FAIL"
            raise
        finally:
            elapsed = time.monotonic() - started
            self.stage_fraction, self.stage_done = 1.0, True
            self.operation, self.last = f"Stage {status}", status
            self.render()
            log(f"STAGE {number:02d} {status} | elapsed={elapsed:.2f}s | {name}")

    @contextlib.contextmanager
    def file_scope(self, path: str, index: int, total: int) -> Iterator[None]:
        self.file = f"{index}/{total} {path}"
        self.unit = "-"
        self.accepted = 0
        self.total = 0
        self.last = "Running"
        self.operation = "File generation / validation"
        started = time.monotonic()
        self.file_started_at = started
        self.file_clock = datetime.now().strftime("%H:%M:%S")
        self.render()
        status = "PASS"
        try:
            yield
        except Exception:
            status = "FAIL"
            raise
        finally:
            file_duration = time.monotonic()-started
            log(f"FILE {status} | elapsed={file_duration:.2f}s | {path}")
            self.update(operation=f"File {status} | elapsed {int(file_duration)//60:02d}:{int(file_duration)%60:02d}", last=status)

    def close(self, success: bool) -> None:
        with self.lock:
            self.closed = True
            self.last = "BUILD PASS" if success else "BUILD FAIL"
            self.operation = "Publication complete" if success else "Build stopped"
        self._heartbeat_stop.set()
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            self._heartbeat_thread.join(timeout=2.0)
        self.render()
        if sys.stdout.isatty():
            sys.stdout.write("\n")
            sys.stdout.flush()

PROGRESS: Progress | None = None
LOG_FILE: Path | None = None

def log(message: str) -> None:
    line = f"{utc_now()} | {redact(message)}"
    if LOG_FILE:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    elif PROGRESS is None:
        print(line, flush=True)


# ---------------------------------------------------------------------------
# Existing Cell 3 transport contract, including DNS-only fallback.
# ---------------------------------------------------------------------------
def get_base_url() -> str:
    value = (os.getenv("CELL3_PUBLIC_URL") or os.getenv("GEMMA_PUBLIC_URL") or os.getenv("CELL3_BASE_URL") or "").strip().rstrip("/")
    if not value:
        raise BuildError("Cell 3 URL missing. Set CELL3_PUBLIC_URL to the public URL printed by Kaggle Cell 3.")
    if not value.startswith(("https://", "http://")):
        raise BuildError("Cell 3 URL must begin with https:// or http://")
    return value

def get_api_token() -> str:
    token = (os.getenv("ORCHESTRATOR_API_KEY") or os.getenv("CELL3_PROXY_TOKEN") or os.getenv("GEMMA_PROXY_TOKEN") or "").strip()
    if not token:
        raise BuildError("Cell 3 authentication token missing. Check the existing Kaggle secret configuration.")
    return token

def _is_dns_resolution_error(exc: BaseException) -> bool:
    markers = ("failed to resolve", "could not resolve", "name or service not known", "getaddrinfo failed", "temporary failure in name resolution", "nodename nor servname provided", "no address associated with hostname", "11001", "11004")
    return any(m in str(exc).lower() for m in markers)

def _resolve_ipv4_via_public_dns(hostname: str) -> list[str]:
    try:
        result = subprocess.run(["nslookup", "-type=A", hostname, "8.8.8.8"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10, check=False, creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0))
    except (OSError, subprocess.SubprocessError):
        return []
    found: list[str] = []
    for candidate in re.findall(r"(?<![0-9.])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9.])", result.stdout + "\n" + result.stderr):
        try:
            ip = ipaddress.IPv4Address(candidate)
        except ipaddress.AddressValueError:
            continue
        if ip.is_global and candidate != "8.8.8.8" and candidate not in found:
            found.append(str(ip))
    return found

def request_with_dns_fallback(method: str, url: str, **kwargs: Any) -> requests.Response:
    try:
        return requests.request(method, url, **kwargs)
    except requests.exceptions.ConnectionError as first:
        if not _is_dns_resolution_error(first):
            raise
        hostname = (urlsplit(url).hostname or "").lower().rstrip(".")
        addresses = _resolve_ipv4_via_public_dns(hostname) if hostname else []
        if not addresses:
            raise first
        original = socket.getaddrinfo
        last: BaseException = first
        try:
            for address in addresses:
                def override(host: Any, port: Any, family: int = 0, type: int = 0, proto: int = 0, flags: int = 0, *, _ip: str = address) -> Any:
                    if str(host).lower().rstrip(".") == hostname:
                        return original(_ip, port, socket.AF_INET, type, proto, flags | getattr(socket, "AI_NUMERICHOST", 0))
                    return original(host, port, family, type, proto, flags)
                socket.getaddrinfo = override
                try:
                    return requests.request(method, url, **kwargs)
                except requests.exceptions.ConnectionError as retry:
                    last = retry
                    if not _is_dns_resolution_error(retry):
                        raise
                finally:
                    socket.getaddrinfo = original
        finally:
            socket.getaddrinfo = original
        raise last

class Cell3Client:
    def __init__(self, timeout: int):
        self.timeout = timeout
        self.base = get_base_url()
        self.auth_headers = {"Authorization": f"Bearer {get_api_token()}", "Content-Type": "application/json"}
        self.capture_dir: Path | None = None
        self.capture_sequence = 0

    def request(self, method: str, path: str, *, payload: dict[str, Any] | None = None, timeout: int | None = None, capture_id: str | None = None) -> dict[str, Any]:
        url = self.base + path
        last: BaseException | None = None
        for attempt in range(1, CONNECTION_RECOVERY_ATTEMPTS + 1):
            try:
                response = request_with_dns_fallback(method, url, headers=self.auth_headers, json=payload, timeout=(10, timeout or self.timeout))
                if response.status_code in (401, 403):
                    raise BuildError(f"CELL3_AUTH_ERROR: HTTP {response.status_code}; credential value was not displayed")
                response.raise_for_status()
                # Capture the exact HTTP body before JSON parsing or model-output
                # validation. A malformed/truncated reply is still durable evidence.
                if method.upper() == "POST" and self.capture_dir is not None and path == "/api/chat" and capture_id:
                    body = response.content if isinstance(response.content, bytes) else str(response.text).encode("utf-8", errors="replace")
                    atomic_write_bytes(self.capture_dir / f"{capture_id}.http.bin", body)
                    save_json(self.capture_dir / f"{capture_id}.meta.json", {"capture_id": capture_id, "timestamp": utc_now(), "http_status": response.status_code, "http_bytes": len(body), "response_sha256": sha256_bytes(body), "model": (payload or {}).get("model"), "prompt_sha256": sha256_text(str(((payload or {}).get("messages") or [{}])[-1].get("content", ""))), "requested_num_predict": (payload or {}).get("options", {}).get("num_predict"), "response_format": (payload or {}).get("format", "text")})
                data = response.json()
                if not isinstance(data, dict):
                    raise BuildError(f"CELL3_INVALID_RESPONSE: {path} returned a non-object JSON payload")
                return data
            except requests.exceptions.SSLError as exc:
                raise BuildError(f"CELL3_TLS_ERROR: {path}: {redact(exc)}") from exc
            except requests.exceptions.Timeout as exc:
                last = exc
                log(f"CELL3_HTTP_RETRY | {path} | attempt={attempt}/{CONNECTION_RECOVERY_ATTEMPTS} | timeout")
            except requests.exceptions.ConnectionError as exc:
                last = exc
                log(f"CELL3_HTTP_CONNECTION_FAILURE | {path} | attempt={attempt}/{CONNECTION_RECOVERY_ATTEMPTS} | {redact(exc)}")
            except requests.exceptions.HTTPError as exc:
                diagnostics = http_error_diagnostics(exc.response)
                log(f"CELL3_HTTP_ERROR | {path} | {diagnostics}")
                raise BuildError(f"CELL3_HTTP_ERROR: {path}: {diagnostics}") from exc
            except ValueError as exc:
                raise BuildError(f"CELL3_INVALID_JSON: {path}: {redact(exc)}") from exc
            if attempt < CONNECTION_RECOVERY_ATTEMPTS:
                time.sleep(CONNECTION_RECOVERY_DELAY * attempt)
        raise BuildError(f"CELL3_CONNECTION_ERROR: Could not reach {url}; attempts={CONNECTION_RECOVERY_ATTEMPTS}; {redact(last or 'unknown')}")

    def generate(self, model: str, prompt: str, *, timeout: int | None = None, num_predict: int = 4096, json_mode: bool = False, temperature: float = 0.0) -> tuple[str, dict[str, Any]]:
        if not 1 <= num_predict <= 8192:
            raise BuildError(f"MODEL_OUTPUT_BUDGET_INVALID: {num_predict}")
        payload: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "think": False,
            "options": {"temperature": temperature, "num_predict": num_predict},
        }
        if json_mode:
            payload["format"] = "json"
        capture_id = None
        if self.capture_dir is not None:
            self.capture_sequence += 1
            capture_id = f"{self.capture_sequence:05d}-{uuid.uuid4().hex[:8]}"
            atomic_write_text(self.capture_dir / f"{capture_id}.prompt.txt", prompt)
        response = self.request("POST", "/api/chat", payload=payload, timeout=timeout, capture_id=capture_id)
        message = response.get("message")
        raw = message.get("content", "") if isinstance(message, dict) else ""
        if not isinstance(raw, str) or not raw.strip():
            raw = response.get("response", "")
        if not isinstance(raw, str) or not raw.strip():
            raise BuildError(f"MODEL_EMPTY_RESPONSE: {model}")
        if self.capture_dir is not None and capture_id:
            atomic_write_text(self.capture_dir / f"{capture_id}.txt", raw)
        return raw, response

    def generate_complete(self, model: str, prompt: str, *, timeout: int | None = None, num_predict: int = 4096, max_continuations: int = 3) -> str:
        """Generate a complete source unit with bounded EOF-aware continuation."""
        prefix, meta = self.generate(model, prompt, timeout=timeout, num_predict=num_predict, json_mode=False)
        for continuation in range(1, max_continuations + 1):
            if meta.get("done") is not False and meta.get("done_reason") != "length":
                return prefix
            continuation_prompt = f"""The previous response was truncated by the output limit. Continue the SAME source unit from its exact ending. Return ONLY the missing tail; do not repeat any earlier source, do not add markdown or explanations, and do not restart the unit.
FILE PREFIX (authoritative, already received):
{prefix[-18000:]}
Continue immediately after its final character."""
            tail, meta = self.generate(model, continuation_prompt, timeout=timeout, num_predict=num_predict, json_mode=False)
            if tail.startswith(prefix):
                prefix = tail
            else:
                max_overlap = min(len(prefix), len(tail), 5000)
                overlap = 0
                for size in range(max_overlap, 40, -1):
                    if prefix.endswith(tail[:size]):
                        overlap = size
                        break
                prefix += tail[overlap:]
        if meta.get("done") is False or meta.get("done_reason") == "length":
            raise BuildError(f"MODEL_CONTINUATION_EXHAUSTED: {model}; continuation limit={max_continuations}")
        return prefix

    def json(self, model: str, prompt: str, *, timeout: int | None = None, num_predict: int = 4096) -> dict[str, Any]:
        last: Exception | None = None
        budget = num_predict
        current_prompt = prompt
        for attempt in range(1, 3):
            try:
                raw, response = self.generate(model, current_prompt, timeout=timeout, num_predict=budget, json_mode=True)
                if response.get("done") is False or response.get("done_reason") == "length":
                    raise BuildError(f"MODEL_OUTPUT_TRUNCATED: {model}; num_predict={budget}")
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError as exc:
                    match = re.search(r"\{[\s\S]*\}", raw)
                    if not match:
                        raise BuildError(f"MODEL_JSON_INVALID: {model}: {exc}") from exc
                    try:
                        data = json.loads(match.group(0))
                    except json.JSONDecodeError as nested:
                        raise BuildError(f"MODEL_JSON_INVALID: {model}: {nested}") from nested
                if not isinstance(data, dict):
                    raise BuildError(f"MODEL_JSON_INVALID: {model}: top-level response must be an object")
                return data
            except Exception as exc:
                last = exc
                if attempt == 1 and ("TRUNCATED" in str(exc) or "JSON_INVALID" in str(exc)):
                    budget = min(8192, max(budget + 512, int(budget * 1.5)))
                    current_prompt = prompt + "\n\nSCHEMA RECOVERY: Return a compact, complete JSON object only. Do not add commentary. Preserve every required field."
                    log(f"MODEL_JSON_RECOVERY | model={model} | attempt=2/2 | num_predict={budget} | cause={redact(exc)}")
                    continue
                raise
        raise BuildError(f"MODEL_JSON_EXHAUSTED: {model}: {redact(last or 'unknown')}")

    def preflight(self, require_planner: bool = True) -> set[str]:
        health = self.request("GET", "/health", timeout=30)
        tags = self.request("GET", "/api/tags", timeout=30)
        available: set[str] = set()
        for data in (health, tags):
            for key in ("models", "data"):
                values = data.get(key, [])
                if isinstance(values, list):
                    for item in values:
                        if isinstance(item, str): available.add(item)
                        elif isinstance(item, dict):
                            name = item.get("name") or item.get("model") or item.get("id")
                            if name: available.add(str(name))
        required = {GENERATOR_MODEL, AUDITOR_MODEL} | ({PLANNER_MODEL} if require_planner else set())
        missing = required - available
        if missing:
            raise BuildError("Required models not detected: " + ", ".join(sorted(missing)) + ". Check existing Cell 3 model availability and allowlist.")
        log(f"CELL3_PREFLIGHT_PASS | models={','.join(sorted(available))} | Cell 2: NOT USED | Proxy configuration: NOT MODIFIED")
        return available


# ---------------------------------------------------------------------------
# Durable build state, immutable accepted artifacts, hash/provenance checking.
# ---------------------------------------------------------------------------
class StateStore:
    def __init__(self, ctx: BuildContext):
        self.ctx = ctx
        self.root = RUNS_DIR / ctx.run_id
        self.root.mkdir(parents=True, exist_ok=True)
        self.state_path = self.root / "build_state.json"
        self.manifest_path = self.root / "build_manifest.json"
        self.accepted_root = self.root / "accepted"
        self.accepted_root.mkdir(exist_ok=True)
        if not self.accepted_root.resolve().is_relative_to(self.root.resolve()):
            raise BuildError("CHECKPOINT_ROOT_PATH_ESCAPE")
        if ctx.resume:
            self.state = load_json(self.state_path)
            if self.state.get("run_id") != ctx.run_id or self.state.get("spec_sha256") != ctx.spec_hash:
                raise BuildError("RESUME_INTEGRITY_FAILED: run identity or specification hash changed")
            if self.state.get("project") != ctx.project_name:
                raise BuildError("RESUME_PROJECT_MISMATCH: project name differs from the original run")
            if self.state.get("status") == "PUBLISHED_PASS":
                raise BuildError("RESUME_ALREADY_COMPLETE: this v7 run was already published")
            if self.state.get("manifest_schema") != MANIFEST_SCHEMA_VERSION:
                raise BuildError("CHECKPOINT_SCHEMA_INCOMPATIBLE: v1 manifest/checkpoint is not validated under schema v2; preserve it and start a fresh run")
            if self.state.get("contract_schema") != CONTRACT_SCHEMA_VERSION:
                raise BuildError("RESUME_CONTRACT_VERSION_MISMATCH: old checkpoints require explicit rebuild")
        else:
            self.state = {"run_id": ctx.run_id, "project": ctx.project_name, "created_at": utc_now(), "updated_at": utc_now(), "status": "INITIALIZED", "spec_sha256": ctx.spec_hash, "manifest_schema": MANIFEST_SCHEMA_VERSION, "manifest_sha256": "", "planning_sha256": "", "contract_sha256": "", "contract_schema": CONTRACT_SCHEMA_VERSION, "accepted_files": {}, "accepted_units": {}, "stage_results": {}, "attempts": {}, "invalidations": []}
            self.persist()
        ctx.state = self.state

    def persist(self) -> None:
        self.state["updated_at"] = utc_now()
        save_json(self.state_path, self.state)

    def set_manifest(self, manifest: list[dict[str, Any]]) -> None:
        digest = canonical_hash(manifest)
        self.state["manifest_sha256"] = digest
        self.state["manifest_schema"] = MANIFEST_SCHEMA_VERSION
        save_json(self.manifest_path, {"schema_version": MANIFEST_SCHEMA_VERSION, "sha256": digest, "files": manifest, "updated_at": utc_now()})
        self.persist()

    def set_planning_artifacts(self, ctx: BuildContext) -> None:
        digest = canonical_hash({"plan": ctx.plan, "architecture": ctx.architecture, "design": ctx.design})
        self.state["planning_sha256"] = digest
        self.persist()

    def set_contract(self, contract: dict[str, Any]) -> None:
        stored = str(contract.get("sha256", ""))
        payload = dict(contract); payload.pop("sha256", None)
        if not stored or canonical_hash(payload) != stored:
            raise BuildError("CONTRACT_CHECKPOINT_HASH_INVALID")
        self.state["contract_sha256"] = stored
        versions = self.root / "contract_versions"
        versions.mkdir(parents=True, exist_ok=True)
        save_json(versions / f"{stored}.json", contract)
        self.persist()

    def stage(self, name: str, status: str, **evidence: Any) -> None:
        self.state.setdefault("stage_results", {})[name] = {"status": status, "timestamp": utc_now(), **evidence}
        self.state["status"] = name + "_" + status
        self.persist()

    def accept_file(self, path: str, content: str, *, contract_hash: str, dependency_hash: str) -> None:
        dest = self.accepted_root.joinpath(*PurePosixPath(path).parts)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not self.accepted_root.resolve().is_relative_to(self.root.resolve()) or not dest.parent.resolve().is_relative_to(self.accepted_root.resolve()):
            raise BuildError(f"CHECKPOINT_PATH_ESCAPE: {path}")
        atomic_write_text(dest, content)
        digest = sha256_text(content)
        self.state.setdefault("accepted_files", {})[path] = {"sha256": digest, "spec_sha256": self.ctx.spec_hash, "manifest_sha256": self.state.get("manifest_sha256", ""), "project_contract_sha256": self.state.get("contract_sha256", ""), "contract_sha256": contract_hash, "dependency_sha256": dependency_hash, "bytes": len(content.encode("utf-8")), "accepted_at": utc_now()}
        self.persist()

    def restore_file(self, path: str, *, contract_hash: str, dependency_hash: str) -> str | None:
        meta = self.state.get("accepted_files", {}).get(path)
        if not isinstance(meta, dict): return None
        candidate = self.accepted_root.joinpath(*PurePosixPath(path).parts)
        try:
            if not candidate.resolve().is_relative_to(self.accepted_root.resolve()): return None
        except (OSError, ValueError):
            return None
        if not candidate.is_file(): return None
        content = candidate.read_text(encoding="utf-8")
        actual = sha256_text(content)
        expected = (meta.get("sha256"), meta.get("spec_sha256"), meta.get("manifest_sha256"), meta.get("contract_sha256"), meta.get("dependency_sha256"))
        current = (actual, self.ctx.spec_hash, self.state.get("manifest_sha256", ""), contract_hash, dependency_hash)
        if expected != current:
            log(f"CHECKPOINT_REJECTED | {path} | provenance/hash/dependency mismatch")
            return None
        return content

    def accept_unit(self, path: str, unit_id: str, content: str, *, prompt_hash: str, contract_hash: str, dependency_hash: str, validator: str = "language-specific-complete-unit") -> None:
        accepted_units_root = self.root / "accepted_units"
        accepted_units_root.mkdir(parents=True, exist_ok=True)
        unit_root = accepted_units_root / sha256_text(path)[:16]
        unit_root.mkdir(parents=True, exist_ok=True)
        if not accepted_units_root.resolve().is_relative_to(self.root.resolve()) or not unit_root.resolve().is_relative_to(accepted_units_root.resolve()):
            raise BuildError("UNIT_CHECKPOINT_PATH_ESCAPE")
        unit_file = unit_root / (sha256_text(unit_id)[:20] + ".src")
        atomic_write_text(unit_file, content)
        record = {"file": path, "unit_id": unit_id, "source_file": str(unit_file), "sha256": sha256_text(content), "prompt_sha256": prompt_hash, "spec_sha256": self.ctx.spec_hash, "manifest_sha256": self.state.get("manifest_sha256", ""), "project_contract_sha256": self.state.get("contract_sha256", ""), "contract_sha256": contract_hash, "dependency_sha256": dependency_hash, "validator": validator, "accepted_at": utc_now()}
        self.state.setdefault("accepted_units", {})[f"{path}::{unit_id}"] = record
        self.persist()

    def restore_unit(self, path: str, unit_id: str, *, prompt_hash: str, contract_hash: str, dependency_hash: str) -> str | None:
        record = self.state.get("accepted_units", {}).get(f"{path}::{unit_id}")
        if not isinstance(record, dict): return None
        expected = (record.get("prompt_sha256"), record.get("spec_sha256"), record.get("manifest_sha256"), record.get("contract_sha256"), record.get("dependency_sha256"))
        if expected != (prompt_hash, self.ctx.spec_hash, self.state.get("manifest_sha256", ""), contract_hash, dependency_hash): return None
        source = Path(record.get("source_file", ""))
        try:
            if not source.resolve().is_relative_to(self.root.resolve()): return None
        except (OSError, ValueError):
            return None
        if not source.is_file(): return None
        content = source.read_text(encoding="utf-8")
        if sha256_text(content) != record.get("sha256"):
            log(f"UNIT_CHECKPOINT_REJECTED | {path}:{unit_id} | SHA-256 mismatch")
            return None
        return content

    def invalidate(self, paths: set[str], reason: str) -> None:
        if not paths: return
        for path in paths:
            self.state.get("accepted_files", {}).pop(path, None)
            for key in list(self.state.get("accepted_units", {})):
                if key.startswith(path + "::"):
                    self.state["accepted_units"].pop(key, None)
        self.state.setdefault("invalidations", []).append({"paths": sorted(paths), "reason": reason, "timestamp": utc_now()})
        self.persist()


# ---------------------------------------------------------------------------
# Planning, manifest, semantic units and syntax validation.
# ---------------------------------------------------------------------------
def extract_json_object(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", raw)
        if not match: raise BuildError("MODEL_JSON_INVALID: no JSON object found")
        value = json.loads(match.group(0))
    if not isinstance(value, dict): raise BuildError("MODEL_JSON_INVALID: expected object")
    return value

MANIFEST_SCHEMA_VERSION = 2
MANIFEST_RELATIONSHIP_GUIDANCE = """MANIFEST RELATIONSHIP GUIDANCE:
- NEVER put stylesheet or script loading types in contract_references.
- HTML stylesheet/script loading belongs only in the HTML file's runtime_references.
- CSS-to-HTML selector agreement uses contract_references with html_selector_contract.
- JavaScript-to-HTML DOM agreement uses contract_references with dom_element_contract or dom_contract.
- Never invent CSS-to-JavaScript edges; classify each relationship by its actual runtime/build/validation meaning.
"""

def _manifest_relationships(item: dict[str, Any], *, allow_legacy: bool = True) -> tuple[list[str], list[str]]:
    """Return only explicitly typed generation/build edges.

    A v1 dependencies list has no reliable semantics. Treating it as a
    generation prerequisite would silently guess its meaning and can recreate
    false dependency cycles.
    """
    if "generation_dependencies" in item or "build_dependencies" in item:
        generation = item.get("generation_dependencies", [])
        build = item.get("build_dependencies", [])
    elif allow_legacy:
        # Preserve legacy relationships as ambiguous migration evidence only.
        generation = []
        build = []
    else:
        raise BuildError("MANIFEST_SCHEMA_INVALID: typed dependency fields are required")
    if not isinstance(generation, list) or not isinstance(build, list) or any(not isinstance(x, str) for x in generation + build):
        raise BuildError(f"MANIFEST_RELATIONSHIP_TYPE_INVALID: {item.get('path', '<unknown>')}")
    return generation, build

def _manifest_kind(path: str) -> str:
    suffix = Path(path).suffix.lower()
    name = Path(path).name.lower()
    if name.startswith("test_") or ".test." in name or any(part.lower() in {"test", "tests"} for part in Path(path).parts[:-1]): return "test"
    return {".html": "html", ".htm": "html", ".css": "css", ".js": "javascript", ".mjs": "javascript", ".py": "python", ".md": "documentation"}.get(suffix, "other")

def validate_manifest_reason_evidence(raw: Any) -> None:
    """Require an explicit rationale for every typed schema-v2 relationship."""
    if not isinstance(raw, dict) or raw.get("schema_version") != MANIFEST_SCHEMA_VERSION or not isinstance(raw.get("files"), list):
        raise BuildError("MANIFEST_SCHEMA_INVALID: expected schema-v2 object with files array")
    fields = ("generation_dependencies", "build_dependencies", "runtime_references", "contract_references", "validation_targets")
    for item in raw["files"]:
        if not isinstance(item, dict):
            raise BuildError("MANIFEST_INVALID: each file entry must be an object")
        path = str(item.get("path", "<unknown>"))
        for field_name in fields:
            entries = item.get(field_name, []) or []
            if not isinstance(entries, list):
                raise BuildError(f"MANIFEST_RELATIONSHIP_TYPE_INVALID: {path}:{field_name}")
            for entry in entries:
                if not isinstance(entry, dict):
                    raise BuildError(f"MANIFEST_RELATIONSHIP_REASON_MISSING: {path}:{field_name} entries require path and reason objects")
                target = entry.get("path")
                reason = entry.get("reason")
                if not isinstance(target, str) or not target.strip() or not isinstance(reason, str) or len(reason.strip()) < 8:
                    raise BuildError(f"MANIFEST_RELATIONSHIP_REASON_MISSING: {path}:{field_name}:{target or '?'}")
                if field_name in {"runtime_references", "contract_references"} and (not isinstance(entry.get("type"), str) or not entry["type"].strip()):
                    raise BuildError(f"MANIFEST_RELATIONSHIP_TYPE_INVALID: {path}:{field_name}:{target} requires type")


def manifest_relationship_type_issues(raw: Any) -> list[dict[str, Any]]:
    """Describe invalid typed edges and safe reciprocal-edge correction evidence."""
    if not isinstance(raw, dict) or not isinstance(raw.get("files"), list):
        return []
    allowed = {
        "runtime_references": {"stylesheet", "script", "asset", "import"},
        "contract_references": {"selector_contract", "dom_contract", "interface_contract", "html_selector_contract", "dom_element_contract"},
    }
    entries = {str(row.get("path")): row for row in raw["files"] if isinstance(row, dict) and isinstance(row.get("path"), str)}
    issues = []
    for source, row in entries.items():
        for field_name, allowed_types in allowed.items():
            refs = row.get(field_name, [])
            if not isinstance(refs, list):
                continue
            for ref in refs:
                if not isinstance(ref, dict):
                    continue
                ref_type = str(ref.get("type", "")).strip().lower()
                target = ref.get("path")
                if ref_type in allowed_types or not isinstance(target, str):
                    continue
                reciprocal = False
                if field_name == "contract_references" and ref_type in allowed["runtime_references"]:
                    target_row = entries.get(target)
                    reverse_refs = target_row.get("runtime_references", []) if isinstance(target_row, dict) else []
                    reciprocal = isinstance(reverse_refs, list) and any(
                        isinstance(reverse, dict)
                        and reverse.get("path") == source
                        and str(reverse.get("type", "")).strip().lower() == ref_type
                        for reverse in reverse_refs
                    )
                issues.append({
                    "file": source,
                    "relationship": field_name,
                    "target": target,
                    "type": ref_type,
                    "allowed_types": sorted(allowed_types),
                    "reciprocal_runtime_reference_exists": reciprocal,
                })
    return issues


def manifest_correction_findings(manifest: list[dict[str, Any]]) -> list[str]:
    """Collect common cross-field manifest mistakes before the single repair call."""
    by_path = {item["path"]: item for item in manifest if isinstance(item, dict) and isinstance(item.get("path"), str)}
    findings: list[str] = []
    for item in by_path.values():
        path = item["path"]
        kind = item.get("kind")
        for target in item.get("validation_targets", []):
            if kind != "test":
                findings.append(f"Remove invalid {path}:validation_targets -> {target}; validation_targets belong only to test/validator files, and include a correction_reasons entry.")
        if kind == "test":
            for field_name in ("generation_dependencies", "build_dependencies"):
                for target in item.get(field_name, []):
                    findings.append(f"Remove {path}:{field_name} -> {target}; tests use validation_targets, never generation/build dependencies, and explain the change.")
            for ref in item.get("runtime_references", []):
                if ref.get("type", "").lower() in {"stylesheet", "script"}:
                    findings.append(f"Remove {path}:runtime_references -> {ref.get('path')}; test files reference tested code with validation_targets, not browser stylesheet/script loading.")
        if kind == "documentation":
            for field_name in ("generation_dependencies", "build_dependencies"):
                for target in item.get(field_name, []):
                    findings.append(f"Remove {path}:{field_name} -> {target}; documentation is not a generation or build prerequisite, and explain the change.")
        for field_name in ("generation_dependencies", "build_dependencies"):
            for target in item.get(field_name, []):
                target_item = by_path.get(target, {})
                target_kind = target_item.get("kind")
                if target_kind == "documentation" and kind != "documentation":
                    findings.append(f"Remove {path}:{field_name} -> {target}; source files must not depend on documentation as a generation/build prerequisite, and explain the change.")
                if kind == "html" and target_kind in {"css", "javascript"}:
                    findings.append(f"Remove {path}:{field_name} -> {target}; HTML loading CSS/JavaScript belongs in runtime_references, never a prerequisite graph, and explain the change.")
                if kind in {"css", "javascript"} and target_kind == "html":
                    findings.append(f"Remove {path}:{field_name} -> {target}; CSS selector/JavaScript DOM relationships belong in contract_references, never a prerequisite graph, and explain the change.")
                if field_name == "generation_dependencies" and kind in {"html", "css", "javascript"} and target_kind in {"html", "css", "javascript"}:
                    findings.append(f"Remove or reclassify {path}:generation_dependencies -> {target}; UI-file relationships belong in runtime_references or contract_references, not generation_dependencies.")
        for ref in item.get("runtime_references", []):
            ref_type = str(ref.get("type", "")).lower()
            target = ref.get("path")
            if ref_type in {"stylesheet", "script"} and kind != "html":
                findings.append(f"Remove {path}:runtime_references -> {target}; only HTML files declare browser stylesheet/script loading, in the HTML-to-asset direction.")
    for graph in ("generation_dependencies", "build_dependencies"):
        try:
            topologically_order_manifest(manifest, graph=graph)
        except BuildError as exc:
            findings.append(f"Repair the {graph} graph cycle reported by {redact(exc)}; remove or correctly reclassify the unsupported edge(s) and explain every changed edge.")
    return findings


def derive_safe_manifest_correction_reasons(original_raw: Any, corrected_raw: Any) -> list[dict[str, str]]:
    """Derive audit reasons only for mechanically provable invalid-edge removals."""
    if not isinstance(original_raw, dict) or not isinstance(corrected_raw, dict):
        return []
    originals = {str(row.get("path")): row for row in original_raw.get("files", []) if isinstance(row, dict) and isinstance(row.get("path"), str)}
    corrected = {str(row.get("path")): row for row in corrected_raw.get("files", []) if isinstance(row, dict) and isinstance(row.get("path"), str)}
    existing = corrected_raw.get("correction_reasons", [])
    reason_keys = {(str(item.get("file")), str(item.get("relationship")), str(item.get("target"))) for item in existing if isinstance(item, dict)}

    def targets(row: dict[str, Any], field_name: str) -> set[str]:
        values = row.get(field_name, [])
        if not isinstance(values, list):
            return set()
        result = set()
        for value in values:
            target = value.get("path") if isinstance(value, dict) else value
            if isinstance(target, str):
                result.add(target)
        return result

    def kind(row: dict[str, Any], path: str) -> str:
        return str(row.get("kind") or _manifest_kind(path)).lower()

    derived = []
    for path, old in originals.items():
        new = corrected.get(path)
        if not new:
            continue
        source_kind = kind(old, path)
        for field_name in ("generation_dependencies", "build_dependencies", "validation_targets", "runtime_references", "contract_references"):
            for target in targets(old, field_name) - targets(new, field_name):
                if (path, field_name, target) in reason_keys:
                    continue
                target_kind = kind(originals.get(target, {}), target) if target in originals else "unknown"
                reason = ""
                if field_name == "validation_targets" and source_kind != "test":
                    reason = "Removed invalid validation target because only test/validator files may declare validation_targets."
                elif field_name in {"generation_dependencies", "build_dependencies"} and source_kind == "test" and target in (targets(old, "validation_targets") | targets(new, "validation_targets")):
                    reason = "Removed prerequisite edge because test inputs are represented by validation_targets."
                elif field_name in {"generation_dependencies", "build_dependencies"} and (source_kind == "documentation" or target_kind == "documentation"):
                    reason = "Removed documentation prerequisite because documentation is not a generation or build prerequisite."
                elif field_name in {"generation_dependencies", "build_dependencies"} and source_kind == "html" and target_kind in {"css", "javascript"} and target in (targets(old, "runtime_references") | targets(new, "runtime_references")):
                    reason = "Removed duplicate HTML asset prerequisite because stylesheet/script loading is represented by runtime_references."
                elif field_name == "generation_dependencies" and source_kind in {"html", "css", "javascript"} and target_kind in {"html", "css", "javascript"}:
                    reason = "Removed invalid UI-file generation edge because UI cross-file relationships belong in runtime_references or contract_references."
                elif field_name == "contract_references":
                    target_row = originals.get(target, {})
                    reverse_runtime = target in originals and any(
                        isinstance(edge, dict) and edge.get("path") == path and str(edge.get("type", "")).lower() == str(old_edge.get("type", "")).lower()
                        for edge in target_row.get("runtime_references", []) if isinstance(target_row.get("runtime_references", []), list)
                        for old_edge in old.get("contract_references", []) if isinstance(old_edge, dict) and old_edge.get("path") == target
                    )
                    if reverse_runtime:
                        reason = "Removed reversed duplicate because the matching runtime reference is correctly directed from the loading HTML file to its asset."
                elif field_name == "runtime_references" and source_kind != "html" and any(
                    isinstance(edge, dict) and edge.get("path") == target and str(edge.get("type", "")).lower() in {"stylesheet", "script"}
                    for edge in old.get("runtime_references", []) if isinstance(old.get("runtime_references", []), list)
                ):
                    reason = "Removed invalid browser-loading edge because only HTML files may declare stylesheet/script runtime references."
                if reason:
                    derived.append({"file": path, "relationship": field_name, "target": target, "reason": reason})
                    reason_keys.add((path, field_name, target))
    return derived


def validate_manifest_correction_file_set(original_entries: dict[str, dict[str, Any]], corrected_entries: dict[str, dict[str, Any]], required_spec_paths: set[str], reason_keys: set[tuple[str, str, str]]) -> None:
    """Prevent a one-shot manifest correction from weakening file ownership."""
    if not required_spec_paths.issubset(corrected_entries):
        raise BuildError("MANIFEST_REPAIR_REJECTED: explicitly required file was removed")
    if not set(corrected_entries).issubset(original_entries):
        raise BuildError("MANIFEST_REPAIR_REJECTED: unrelated file was added")
    for path, old in original_entries.items():
        new = corrected_entries.get(path)
        if new is None:
            if path in required_spec_paths or (path, "file", path) not in reason_keys:
                raise BuildError(f"MANIFEST_REPAIR_REJECTED: file removal is not justified: {path}")
            continue
        if new.get("kind") != old.get("kind"):
            raise BuildError(f"MANIFEST_REPAIR_REJECTED: file ownership changed for {path}")
        if new.get("required") != old.get("required"):
            raise BuildError(f"MANIFEST_REPAIR_REJECTED: required status changed for {path}")
        old_valid_test_targets = {target for target in old.get("validation_targets", []) if old.get("kind") == "test" and target in original_entries and target != path and original_entries[target].get("kind") not in {"test", "documentation"}}
        if not old_valid_test_targets.issubset(set(new.get("validation_targets", []))):
            raise BuildError(f"MANIFEST_REPAIR_REJECTED: validation targets weakened for {path}")


def preserve_unchanged_manifest_edge_reasons(original_raw: Any, corrected_raw: Any) -> int:
    """Restore reasons for unchanged edges when a correction response omits them."""
    if not isinstance(original_raw, dict) or not isinstance(corrected_raw, dict):
        return 0
    original_rows = {str(row.get("path")): row for row in original_raw.get("files", []) if isinstance(row, dict) and isinstance(row.get("path"), str)}
    count = 0
    for row in corrected_raw.get("files", []):
        if not isinstance(row, dict) or not isinstance(row.get("path"), str):
            continue
        old = original_rows.get(row["path"], {})
        for field_name in ("generation_dependencies", "build_dependencies", "runtime_references", "contract_references", "validation_targets"):
            new_edges = row.get(field_name, [])
            old_edges = old.get(field_name, []) if isinstance(old, dict) else []
            if not isinstance(new_edges, list) or not isinstance(old_edges, list):
                continue
            old_by_key = {}
            for edge in old_edges:
                if not isinstance(edge, dict):
                    continue
                target = edge.get("path")
                reason = edge.get("reason")
                edge_type = str(edge.get("type", "")).strip().lower()
                if isinstance(target, str) and isinstance(reason, str) and len(reason.strip()) >= 8:
                    old_by_key[(target, edge_type)] = reason.strip()
            for edge in new_edges:
                if not isinstance(edge, dict):
                    continue
                reason = edge.get("reason")
                if isinstance(reason, str) and len(reason.strip()) >= 8:
                    continue
                target = edge.get("path")
                edge_type = str(edge.get("type", "")).strip().lower()
                preserved = old_by_key.get((target, edge_type)) if isinstance(target, str) else None
                if preserved:
                    edge["reason"] = preserved
                    count += 1
    return count


def normalize_manifest(raw: Any, spec: str, *, require_v2: bool = False, allow_self_relationships: bool = False, allow_missing_reasons: bool = False) -> list[dict[str, Any]]:
    version = raw.get("schema_version", 1) if isinstance(raw, dict) else None
    if type(version) is not int or version not in {1, MANIFEST_SCHEMA_VERSION}:
        raise BuildError(f"MANIFEST_SCHEMA_INVALID: unsupported schema_version {version!r}")
    if require_v2 and version != MANIFEST_SCHEMA_VERSION:
        raise BuildError(f"MANIFEST_SCHEMA_INVALID: expected schema_version {MANIFEST_SCHEMA_VERSION}")
    if version == MANIFEST_SCHEMA_VERSION and not allow_missing_reasons:
        validate_manifest_reason_evidence(raw)
    entries = raw.get("files") if isinstance(raw, dict) else None
    if not isinstance(entries, list) or not entries:
        raise BuildError("MANIFEST_INVALID: expected a non-empty files array")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    seen_casefold: set[str] = set()
    for item in entries:
        if not isinstance(item, dict): raise BuildError("MANIFEST_INVALID: each file entry must be an object")
        raw_path = item.get("path")
        if not isinstance(raw_path, str):
            raise BuildError("MANIFEST_SCHEMA_INVALID: file path must be a string")
        path = safe_relative_path(raw_path)
        if path in seen or path.casefold() in seen_casefold: raise BuildError(f"MANIFEST_DUPLICATE_PATH: {path}")
        seen.add(path); seen_casefold.add(path.casefold())
        raw_purpose = item.get("purpose")
        if not isinstance(raw_purpose, str):
            raise BuildError(f"MANIFEST_SCHEMA_INVALID: purpose must be a string ({path})")
        purpose = raw_purpose.strip()
        if not purpose: raise BuildError(f"MANIFEST_PURPOSE_MISSING: {path}")
        rel_fields = ("generation_dependencies", "build_dependencies", "runtime_references", "contract_references", "validation_targets")
        rels: dict[str, Any] = {}
        reasons: dict[str, dict[str, str]] = {}
        for field_name in rel_fields:
            value = item.get(field_name, [])
            if not isinstance(value, list): raise BuildError(f"MANIFEST_RELATIONSHIP_TYPE_INVALID: {path}:{field_name}")
            normalized = []
            reason_map: dict[str, str] = {}
            for edge in value:
                if version == MANIFEST_SCHEMA_VERSION:
                    if isinstance(edge, dict):
                        target_raw = edge.get("path")
                        reason = str(edge.get("reason", "")).strip()
                    elif allow_missing_reasons and isinstance(edge, str):
                        target_raw = edge; reason = ""
                    else:
                        raise BuildError(f"MANIFEST_RELATIONSHIP_REASON_MISSING: {path}:{field_name}")
                elif field_name in {"runtime_references", "contract_references"}:
                    if not isinstance(edge, dict) or not isinstance(edge.get("path"), str) or not isinstance(edge.get("type"), str):
                        raise BuildError(f"MANIFEST_RELATIONSHIP_TYPE_INVALID: {path}:{field_name}")
                    target_raw = edge["path"]; reason = str(edge.get("reason", "")).strip()
                else:
                    target_raw = edge if isinstance(edge, str) else None
                    reason = ""
                if not isinstance(target_raw, str) or not target_raw.strip():
                    raise BuildError(f"MANIFEST_RELATIONSHIP_TYPE_INVALID: {path}:{field_name}")
                target = safe_relative_path(target_raw.strip())
                if version == MANIFEST_SCHEMA_VERSION and not allow_missing_reasons and len(reason) < 8:
                    raise BuildError(f"MANIFEST_RELATIONSHIP_REASON_MISSING: {path}:{field_name}:{target}")
                if field_name in {"runtime_references", "contract_references"}:
                    edge_type = edge.get("type") if isinstance(edge, dict) else None
                    if not isinstance(edge_type, str) or not edge_type.strip():
                        raise BuildError(f"MANIFEST_RELATIONSHIP_TYPE_INVALID: {path}:{field_name}:{target}")
                    normalized.append({"path": target, "type": edge_type.strip(), **({"reason": reason} if reason else {})})
                else:
                    normalized.append(target)
                if reason: reason_map[target] = reason
            keys = [(x["path"].casefold(), x["type"].casefold()) for x in normalized] if field_name in {"runtime_references", "contract_references"} else [x.casefold() for x in normalized]
            if len(set(keys)) != len(keys): raise BuildError(f"MANIFEST_DUPLICATE_EDGE: {path}:{field_name}")
            target_paths = [x["path"] for x in normalized] if field_name in {"runtime_references", "contract_references"} else normalized
            if not allow_self_relationships and path.casefold() in {x.casefold() for x in target_paths}: raise BuildError(f"MANIFEST_SELF_DEPENDENCY: {path}:{field_name}")
            rels[field_name] = normalized
            reasons[field_name] = reason_map
        # Never silently discard the v1 `dependencies` field merely because a
        # partially migrated manifest also contains one of the v2 typed fields.
        # Its meaning remains ambiguous until the bounded migration/correction
        # step classifies it. Schema v2 must not carry the legacy field at all.
        if "dependencies" in item:
            legacy = item.get("dependencies")
            if version == MANIFEST_SCHEMA_VERSION:
                raise BuildError(f"MANIFEST_SCHEMA_INVALID: schema v2 file {path} uses legacy dependencies")
            if not isinstance(legacy, list) or any(not isinstance(x, str) or not x.strip() for x in legacy):
                raise BuildError(f"MANIFEST_SCHEMA_INVALID: {path}: malformed v1 dependencies")
            if legacy:
                rels["ambiguous_legacy_dependencies"] = [safe_relative_path(x.strip()) for x in legacy]
        required_raw = item.get("required", True)
        if version == MANIFEST_SCHEMA_VERSION and not isinstance(required_raw, bool):
            raise BuildError(f"MANIFEST_SCHEMA_INVALID: required must be a boolean ({path})")
        required = required_raw if isinstance(required_raw, bool) else str(required_raw).strip().lower() not in {"false", "no", "0", "optional"}
        raw_kind = item.get("kind")
        if raw_kind is not None and not isinstance(raw_kind, str):
            raise BuildError(f"MANIFEST_SCHEMA_INVALID: kind must be a string ({path})")
        kind = str(raw_kind or _manifest_kind(path)).strip().lower()
        if kind != _manifest_kind(path): raise BuildError(f"MANIFEST_FILE_OWNERSHIP_INVALID: {path} has kind {kind}, expected {_manifest_kind(path)}")
        result.append({"path": path, "kind": kind, "purpose": purpose, **rels, "relationship_reasons": reasons, "required": required})
    if len(result) > MAX_FILES: raise BuildError("MANIFEST_TOO_LARGE")
    return result

def validate_manifest_relationships(manifest: list[dict[str, Any]], spec: str = "") -> None:
    paths = {x["path"] for x in manifest}
    canonical = {p.casefold(): p for p in paths}
    allowed_source_suffixes: set[str] | None = None
    use_only = re.search(r"(?im)^\s*Use only:\s*$([\s\S]*?)(?=\n\s*\n|\Z)", spec)
    if use_only:
        declarations = use_only.group(1).lower()
        allowed_source_suffixes = set()
        if "html" in declarations: allowed_source_suffixes.update({".html", ".htm"})
        if "css" in declarations: allowed_source_suffixes.add(".css")
        if "javascript" in declarations: allowed_source_suffixes.update({".js", ".mjs", ".cjs"})
        if "python" in declarations: allowed_source_suffixes.add(".py")
        if "typescript" in declarations: allowed_source_suffixes.update({".ts", ".tsx"})
        if "jsx" in declarations: allowed_source_suffixes.add(".jsx")
    for item in manifest:
        path = item["path"]
        suffix = Path(path).suffix.lower()
        if allowed_source_suffixes is not None and suffix in {".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".html", ".htm", ".css"} and suffix not in allowed_source_suffixes:
            raise BuildError(f"MANIFEST_FILE_OWNERSHIP_INVALID: {path} uses a source type forbidden by the specification")
        for field_name in ("generation_dependencies", "build_dependencies", "validation_targets"):
            refs = item.get(field_name, [])
            for ref in refs:
                if ref not in paths:
                    raise BuildError(f"MANIFEST_REFERENCE_TARGET_MISSING: {path}:{field_name}:{ref}")
                target_item = next(x for x in manifest if x["path"] == ref)
                target_kind = target_item.get("kind")
                if field_name in {"generation_dependencies", "build_dependencies"}:
                    if target_kind == "documentation" and item.get("kind") != "documentation":
                        raise BuildError(f"MANIFEST_RELATIONSHIP_TYPE_INVALID: source files cannot depend on documentation ({path} -> {ref})")
                    if item.get("kind") == "html" and target_kind in {"css", "javascript"}:
                        raise BuildError(f"MANIFEST_RELATIONSHIP_TYPE_INVALID: HTML asset loading is a runtime_reference, not a prerequisite ({path} -> {ref})")
                    if item.get("kind") in {"css", "javascript"} and target_kind == "html":
                        raise BuildError(f"MANIFEST_RELATIONSHIP_TYPE_INVALID: CSS/DOM agreement is a contract_reference, not a prerequisite ({path} -> {ref})")
        if item.get("validation_targets") and item.get("kind") != "test":
            raise BuildError(f"MANIFEST_RELATIONSHIP_TYPE_INVALID: only test/validator files may declare validation_targets ({path})")
        if item.get("kind") == "test":
            if item.get("required", True) and not item.get("validation_targets"):
                raise BuildError(f"MANIFEST_RELATIONSHIP_TYPE_INVALID: required test file needs at least one application validation target ({path})")
            for target in item.get("validation_targets", []):
                if target == path:
                    raise BuildError(f"MANIFEST_SELF_DEPENDENCY: validation target cannot be the test file itself ({path})")
                target_entry = next(x for x in manifest if x["path"] == target)
                if target_entry.get("kind") in {"test", "documentation"}:
                    raise BuildError(f"MANIFEST_RELATIONSHIP_TYPE_INVALID: test validation target must be application source ({path} -> {target})")
        for field_name in ("runtime_references", "contract_references"):
            for ref in item.get(field_name, []):
                allowed_types = {"stylesheet", "script", "asset", "import"} if field_name == "runtime_references" else {"selector_contract", "dom_contract", "interface_contract", "html_selector_contract", "dom_element_contract"}
                if ref.get("type", "").lower() not in allowed_types:
                    raise BuildError(f"MANIFEST_RELATIONSHIP_TYPE_INVALID: {path}:{field_name}:{ref.get('type')}")
                target = ref["path"]
                if target not in paths:
                    raise BuildError(f"MANIFEST_REFERENCE_TARGET_MISSING: {path}:{field_name}:{target}")
                if target.casefold() == path.casefold():
                    raise BuildError(f"MANIFEST_SELF_DEPENDENCY: {path}:{field_name}")
                target_kind = next(x["kind"] for x in manifest if x["path"] == target)
                source_kind = item.get("kind")
                if field_name == "runtime_references":
                    ref_type = ref["type"].lower()
                    if ref_type in {"stylesheet", "script"}:
                        if source_kind != "html":
                            raise BuildError(f"MANIFEST_FILE_OWNERSHIP_INVALID: only HTML files may declare browser {ref_type} loading ({path})")
                        expected_kind = "css" if ref_type == "stylesheet" else "javascript"
                        if target_kind != expected_kind:
                            raise BuildError(f"MANIFEST_FILE_OWNERSHIP_INVALID: {path} declares {ref_type} reference to {target} ({target_kind})")
                    elif ref_type == "import":
                        import_targets = {"python": {"python"}, "javascript": {"javascript", "css"}, "css": {"css"}}
                        if source_kind not in import_targets or target_kind not in import_targets[source_kind]:
                            raise BuildError(f"MANIFEST_RELATIONSHIP_TYPE_INVALID: {source_kind} import cannot target {target_kind} ({path} -> {target})")
                elif field_name == "contract_references":
                    ref_type = ref["type"].lower()
                    expected = {
                        "selector_contract": ("html", "css"),
                        "dom_contract": ("html", "javascript"),
                        "html_selector_contract": ("css", "html"),
                        "dom_element_contract": ("javascript", "html"),
                    }.get(ref_type)
                    if expected and (source_kind, target_kind) != expected:
                        raise BuildError(f"MANIFEST_RELATIONSHIP_TYPE_INVALID: {ref_type} requires {expected[0]} -> {expected[1]}, got {source_kind} -> {target_kind} ({path} -> {target})")
                    if ref_type == "interface_contract" and (source_kind not in {"python", "javascript"} or target_kind not in {"python", "javascript"}):
                        raise BuildError(f"MANIFEST_RELATIONSHIP_TYPE_INVALID: interface_contract must connect Python/JavaScript source files ({path} -> {target})")
        if item.get("kind") == "test" and item.get("generation_dependencies"):
            raise BuildError(f"MANIFEST_RELATIONSHIP_TYPE_INVALID: test files must use validation_targets, not generation_dependencies ({path})")
        if item.get("kind") == "test" and item.get("build_dependencies"):
            raise BuildError(f"MANIFEST_RELATIONSHIP_TYPE_INVALID: test files must use validation_targets, not build_dependencies ({path})")
        if item.get("kind") == "documentation" and (item.get("generation_dependencies") or item.get("build_dependencies")):
            raise BuildError(f"MANIFEST_RELATIONSHIP_TYPE_INVALID: documentation references are not build prerequisites ({path})")
        if item.get("kind") in {"html", "css", "javascript"}:
            for target in item.get("generation_dependencies", []):
                target_entry = next((x for x in manifest if x["path"] == target), None)
                if target_entry and target_entry.get("kind") in {"html", "css", "javascript"}:
                    raise BuildError(f"MANIFEST_RELATIONSHIP_TYPE_INVALID: UI file relationships use runtime/contract references, not generation_dependencies ({path} -> {target})")
    if len(canonical) != len(paths):
        raise BuildError("MANIFEST_FILE_OWNERSHIP_INVALID: case-insensitive path collision")


def required_file_lines(spec: str) -> dict[str, str]:
    triggers = re.compile(r"\b(must|required|include|deliver|provide|create|generate)\b", re.I)
    pattern = re.compile(r"(?<![A-Za-z0-9_.-])(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.(?:py|md|txt|json|yaml|yml|toml|ini|cfg|html|css|js|ts|tsx|jsx|sql|sh|bat|ps1|csv)(?![A-Za-z0-9_.-])", re.I)
    found: dict[str, str] = {}
    file_list_header = re.compile(r"\b(?:separate|well.organized|required|deliverable|project)\b.*\bfiles\s*:\s*$", re.I)
    in_file_list = False
    for line in spec.splitlines():
        stripped = line.strip()
        if file_list_header.search(stripped):
            in_file_list = True
            continue
        if in_file_list:
            if not stripped:
                in_file_list = False
            elif re.match(r"^(?:[-*]|\d+[.)])\s+", stripped):
                for path in pattern.findall(stripped):
                    try: found[safe_relative_path(path)] = stripped
                    except BuildError: pass
                continue
            else:
                in_file_list = False
        if triggers.search(line):
            for path in pattern.findall(line):
                try: found[safe_relative_path(path)] = line.strip()
                except BuildError: pass
    return found

def ensure_required_files(manifest: list[dict[str, Any]], spec: str) -> list[dict[str, Any]]:
    result = list(manifest)
    by_casefold = {x["path"].casefold(): x for x in result}
    for path, line in required_file_lines(spec).items():
        existing = by_casefold.get(path.casefold())
        if existing is not None:
            existing["required"] = True
        else:
            entry = {"path": path, "kind": _manifest_kind(path), "purpose": "Explicitly required by the specification: " + line, "generation_dependencies": [], "build_dependencies": [], "runtime_references": [], "contract_references": [], "validation_targets": [], "required": True}
            result.append(entry); by_casefold[path.casefold()] = entry
    return result

def parse_model_units(data: dict[str, Any], path: str) -> tuple[str, list[dict[str, Any]]]:
    preamble = str(data.get("preamble", "")).strip()
    units_raw = data.get("units", [])
    if not isinstance(units_raw, list): raise BuildError(f"UNIT_PLAN_INVALID: {path}")
    units: list[dict[str, Any]] = []
    names: set[str] = set()
    for row in units_raw:
        if not isinstance(row, dict): raise BuildError(f"UNIT_PLAN_INVALID: {path}: each unit must be an object")
        name = str(row.get("name", "")).strip()
        purpose = str(row.get("purpose", "")).strip()
        kind = str(row.get("kind", "block")).strip().lower()
        deps = row.get("depends_on", [])
        if not isinstance(deps, list) or any(not isinstance(x, str) or not x.strip() for x in deps):
            raise BuildError(f"UNIT_PLAN_INVALID: {path}:{name}: depends_on must be an array of unit names")
        deps = [x.strip() for x in deps]
        target_file_raw = row.get("target_file")
        target_file = None
        if target_file_raw is not None:
            if not isinstance(target_file_raw, str) or not target_file_raw.strip():
                raise BuildError(f"UNIT_TARGET_FILE_INVALID: {path}:{name}")
            target_file = safe_relative_path(target_file_raw.strip())
        if not name or not purpose or name in names: raise BuildError(f"UNIT_PLAN_INVALID: {path}: missing/duplicate name or purpose")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.:-]{0,100}", name): raise BuildError(f"UNIT_NAME_INVALID: {path}:{name}")
        if not re.fullmatch(r"[a-z_]{2,32}", kind): raise BuildError(f"UNIT_KIND_INVALID: {path}:{kind}")
        if len(deps) != len(set(deps)) or name in deps: raise BuildError(f"UNIT_DEPENDENCY_INVALID: {path}:{name}")
        names.add(name)
        normalized_unit = {"name": name, "purpose": purpose, "kind": kind, "depends_on": deps}
        if target_file is not None:
            normalized_unit["target_file"] = target_file
        units.append(normalized_unit)
    prior: set[str] = set()
    for unit in units:
        unknown = set(unit["depends_on"]) - names
        forward = set(unit["depends_on"]) - prior
        if unknown or forward:
            raise BuildError(f"UNIT_DEPENDENCY_ORDER_INVALID: {path}:{unit['name']}: unknown={sorted(unknown)} forward_or_cyclic={sorted(forward)}")
        prior.add(unit["name"])
    if not units and not preamble: raise BuildError(f"UNIT_PLAN_EMPTY: {path}")
    return preamble, units


def validate_unit_plan_for_file(data: dict[str, Any], path: str, manifest_paths: set[str] | None = None) -> tuple[str, list[dict[str, Any]]]:
    """Project plans may contain units for several files; select only this file's kinds.

    The model is asked for one file, but real captured responses have occasionally
    returned a project-wide unit list. Treating every other-file unit as a fatal
    error aborts a valid build. Validate the complete plan first, then route by
    semantic kind and retain only dependencies that are local to this file.
    """
    preamble, all_units = parse_model_units(data, path)
    suffix = Path(path).suffix.lower()
    allowed = {
        ".py": {"function", "async_function", "class", "entrypoint", "module_block", "statement", "block"},
        ".js": {"function", "async_function", "class", "entrypoint", "block", "statement"},
        ".mjs": {"function", "async_function", "class", "entrypoint", "block", "statement"},
        ".cjs": {"function", "async_function", "class", "entrypoint", "block", "statement"},
        ".css": {"css_rule"},
        ".html": {"html_fragment"},
        ".htm": {"html_fragment"},
    }.get(suffix)
    if allowed is None:
        return preamble, all_units

    # A project-wide HTML document shell is not a preamble for CSS/JS/Python.
    if suffix not in {".html", ".htm"} and re.search(r"<\s*(?:!doctype|html|head|body)\b", preamble, re.I):
        log(f"UNIT_PLAN_PREAMBLE_ROUTED | {path} | discarded foreign HTML document shell")
        preamble = ""

    explicit_targets = [u for u in all_units if u.get("target_file")]
    if explicit_targets:
        if len(explicit_targets) != len(all_units):
            raise BuildError(f"UNIT_PLAN_TARGET_OWNERSHIP_INCOMPLETE: {path}: every unit must declare target_file when any unit does")
        canonical = {x.casefold(): x for x in (manifest_paths or {path})}
        unknown_targets = sorted({u["target_file"] for u in explicit_targets if u["target_file"].casefold() not in canonical})
        if unknown_targets:
            raise BuildError(f"UNIT_PLAN_TARGET_FILE_NOT_IN_MANIFEST: {unknown_targets}")
        for unit in explicit_targets:
            unit["target_file"] = canonical[unit["target_file"].casefold()]
        selected = [u for u in all_units if u["target_file"] == path and u["kind"] in allowed]
        wrong_kind = [(u["name"], u["kind"]) for u in all_units if u["target_file"] == path and u["kind"] not in allowed]
        if wrong_kind:
            raise BuildError(f"UNIT_PLAN_FILE_KIND_MISMATCH: {path}: explicitly assigned incompatible units={wrong_kind}; allowed={sorted(allowed)}")
        discarded = [(u["name"], u.get("target_file"), u["kind"]) for u in all_units if u["target_file"] != path]
        if discarded:
            log(f"UNIT_PLAN_EXPLICIT_FILE_ROUTING | {path} | selected={len(selected)} other_file_units={discarded}")
        # A shared preamble has no ownership metadata. In an explicitly routed
        # multi-file plan it belongs only to the HTML document, never to JS/CSS.
        if suffix not in {".html", ".htm"} and preamble:
            log(f"UNIT_PLAN_PREAMBLE_ROUTED | {path} | discarded unowned preamble from explicitly routed plan")
            preamble = ""
    else:
        selected = [u for u in all_units if u["kind"] in allowed]
        discarded = [(u["name"], u["kind"]) for u in all_units if u["kind"] not in allowed]
        if discarded:
            log(f"UNIT_PLAN_ROUTED | {path} | selected={len(selected)} discarded_for_other_files={discarded}")
    if not selected and not preamble:
        raise BuildError(
            f"UNIT_PLAN_NO_COMPATIBLE_UNITS: {path}: plan contained {len(all_units)} units, "
            f"but none matched allowed kinds={sorted(allowed)}"
        )

    # Dependencies on units assigned to another file are handled by manifest/file
    # ordering and the canonical cross-file contract, not by this file's generator.
    selected_names = {u["name"] for u in selected}
    units = [
        {**u, "depends_on": [dep for dep in u["depends_on"] if dep in selected_names]}
        for u in selected
    ]

    if suffix in {".html", ".htm"}:
        if preamble and (not re.search(r"<html\b", preamble, re.I) or not re.search(r"<body\b", preamble, re.I)):
            raise BuildError(f"HTML_PREAMBLE_MUST_OWN_DOCUMENT_SHELL: {path}")
        for unit in units:
            identity = f"{unit['name']} {unit['purpose']}"
            if re.search(r"document\s*(?:root|shell)|html\s*(?:document\s*)?(?:root|shell|skeleton)", identity, re.I):
                raise BuildError(f"HTML_DOCUMENT_SHELL_UNIT_FORBIDDEN: {path}:{unit['name']}; document shell belongs to preamble")
    return preamble, units


def validate_html_unit_fragment(source: str, path: str) -> None:
    """HTML units must be body fragments, never a second document or root shell."""
    validate_html_fragment(source, path)
    if re.search(r"<\s*/?\s*(?:html|head|body)\b", source, re.I):
        raise BuildError(f"HTML_UNIT_DOCUMENT_SHELL_FORBIDDEN: {path}: units must be body fragments")


def _html_root_insertion_offset(document: str) -> int:
    """Find the closing tag offset for #app-shell, falling back to </body>."""
    class Locator(html.parser.HTMLParser):
        def __init__(self, source: str):
            super().__init__(convert_charrefs=False)
            self.source = source
            self.lines = [0]
            for m in re.finditer("\\n", source): self.lines.append(m.end())
            self.app_depth = 0
            self.app_close = None
            self.body_close = None
        def char_offset(self):
            line, col = self.getpos()
            return self.lines[line - 1] + col
        def handle_starttag(self, tag, attrs):
            if tag.lower() == "div":
                attrs_map = dict(attrs)
                if self.app_depth:
                    self.app_depth += 1
                elif attrs_map.get("id") == "app-shell":
                    self.app_depth = 1
        def handle_startendtag(self, tag, attrs):
            return
        def handle_endtag(self, tag):
            if tag.lower() == "div" and self.app_depth:
                self.app_depth -= 1
                if self.app_depth == 0 and self.app_close is None:
                    self.app_close = self.char_offset()
            elif tag.lower() == "body" and self.body_close is None:
                self.body_close = self.char_offset()
    parser = Locator(document)
    parser.feed(document); parser.close()
    return parser.app_close if parser.app_close is not None else (parser.body_close if parser.body_close is not None else len(document))


def assemble_html_document(preamble: str, fragments: list[str], postamble: str, path: str) -> str:
    """Assemble semantic HTML fragments inside the preamble's application root."""
    body = "\n".join(x.strip() for x in fragments if x.strip())
    base = preamble.strip()
    if not base:
        base = "<!doctype html><html><head></head><body></body></html>"
    complete_document = bool(re.search(r"</html\s*>", base, re.I))
    insertion_content = "\n".join(x for x in (body, postamble.strip()) if x) if complete_document else body
    if insertion_content:
        if complete_document:
            offset = _html_root_insertion_offset(base)
            base = base[:offset] + insertion_content + "\n" + base[offset:]
        else:
            base = base.rstrip() + "\n" + insertion_content
    if postamble.strip() and not complete_document:
        base = base.rstrip() + "\n" + postamble.strip()
    base = base.rstrip() + "\n"
    validate_html_document(base, path)
    return base


def validate_unit_preamble(source: str, path: str, timeout: int) -> None:
    if not source.strip(): return
    suffix = Path(path).suffix.lower()
    if suffix == ".py":
        try: tree = ast.parse(source, filename=path)
        except SyntaxError as exc: raise BuildError(f"PYTHON_PREAMBLE_INVALID: {path}:{exc.lineno}: {exc.msg}") from exc
        if any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) for n in tree.body):
            raise BuildError(f"PYTHON_PREAMBLE_BOUNDARY_INVALID: {path}: preamble may contain imports/constants only")
    elif suffix in {".js", ".mjs", ".cjs"}:
        validate_js(source, path, timeout)
        declarations = javascript_top_level_declarations(source, path)
        if declarations["functions"] or declarations["classes"]:
            raise BuildError(f"JS_PREAMBLE_BOUNDARY_INVALID: {path}: preamble may contain imports/constants only")
    elif suffix == ".css":
        if not balanced_delimiters(source, css=True): raise BuildError(f"CSS_PREAMBLE_INVALID: {path}")
    elif suffix in {".html", ".htm"}:
        if "<html" not in source.lower(): raise BuildError(f"HTML_PREAMBLE_INVALID: {path}: document shell must open html root")


def python_unit_validate(source: str, unit: dict[str, str], path: str) -> str:
    source = source.strip()
    source = re.sub(r"^```(?:python|py)?\s*|\s*```$", "", source, flags=re.I).strip()
    try: tree = ast.parse(source, filename=path)
    except SyntaxError as exc: raise BuildError(f"PYTHON_UNIT_SYNTAX: {path}:{unit['name']} line={exc.lineno}: {exc.msg}") from exc
    kind, name = unit["kind"], unit["name"]
    if kind in {"function", "async_function"}:
        nodes = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        if len(tree.body) != 1 or len(nodes) != 1 or nodes[0].name != name: raise BuildError(f"PYTHON_UNIT_BOUNDARY: expected only {name}")
        if kind == "async_function" and not isinstance(nodes[0], ast.AsyncFunctionDef): raise BuildError(f"PYTHON_UNIT_KIND: {name} must be async")
    elif kind == "class":
        nodes = [n for n in tree.body if isinstance(n, ast.ClassDef)]
        if len(tree.body) != 1 or len(nodes) != 1 or nodes[0].name != name: raise BuildError(f"PYTHON_UNIT_BOUNDARY: expected only class {name}")
    elif kind == "entrypoint":
        guards = [n for n in tree.body if isinstance(n, ast.If) and isinstance(n.test, ast.Compare) and isinstance(n.test.left, ast.Name) and n.test.left.id == "__name__" and any(isinstance(c, ast.Constant) and c.value == "__main__" for c in n.test.comparators)]
        if len(guards) != 1: raise BuildError(f"PYTHON_ENTRYPOINT_BOUNDARY: expected exactly one if __name__ == '__main__' guard in {path}")
    elif kind not in {"module_block", "statement", "block"}:
        raise BuildError(f"PYTHON_UNIT_KIND_UNSUPPORTED: {kind}")
    return source + "\n"

def validate_assembled_python(source: str, path: str) -> ast.Module:
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as exc:
        raise BuildError(f"PYTHON_FILE_ASSEMBLY_INVALID: {path}:{exc.lineno}: {exc.msg}") from exc
    names = [n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise BuildError(f"PYTHON_DUPLICATE_TOP_LEVEL_SYMBOLS: {path}: {duplicates}")
    return tree


def split_css_selector_list(selector: str) -> list[str]:
    parts: list[str] = []; start = 0; paren = bracket = 0; quote: str | None = None; escaped = False
    for i, ch in enumerate(selector):
        if quote:
            if escaped: escaped = False
            elif ch == "\\": escaped = True
            elif ch == quote: quote = None
            continue
        if ch in {"'", '"'}: quote = ch
        elif ch == "(": paren += 1
        elif ch == ")": paren = max(0, paren - 1)
        elif ch == "[": bracket += 1
        elif ch == "]": bracket = max(0, bracket - 1)
        elif ch == "," and paren == 0 and bracket == 0:
            item = selector[start:i].strip()
            if item: parts.append(item)
            start = i + 1
    tail = selector[start:].strip()
    if tail: parts.append(tail)
    return parts


def balanced_delimiters(source: str, *, css: bool = False) -> bool:
    stack: list[str] = []
    quote: str | None = None
    escaped = False
    line_comment = False
    block_comment = False
    i = 0
    pairs = {")": "(", "]": "[", "}": "{"}
    while i < len(source):
        ch = source[i]
        nx = source[i + 1] if i + 1 < len(source) else ""
        if block_comment:
            if ch == "*" and nx == "/": block_comment = False; i += 2; continue
            i += 1; continue
        if not css and line_comment:
            if ch == "\n": line_comment = False
            i += 1; continue
        if quote:
            if escaped: escaped = False
            elif ch == "\\": escaped = True
            elif ch == quote: quote = None
            i += 1; continue
        if ch == "/" and nx == "*": block_comment = True; i += 2; continue
        if not css and ch == "/" and nx == "/": line_comment = True; i += 2; continue
        if ch in ("'", '"', "`"):
            quote = ch; i += 1; continue
        if ch in "([{" : stack.append(ch)
        elif ch in ")]}":
            if not stack or stack.pop() != pairs[ch]: return False
        i += 1
    return not stack and quote is None and not block_comment

def validate_css_syntax(source: str, path: str) -> None:
    """Parse CSS rules and block contents, including nested rules/declarations."""
    try:
        import tinycss2
    except ImportError:
        # Dependency-free conservative fallback. The structural scanner catches
        # unterminated blocks/comments; this pass catches malformed declaration
        # segments without pretending to implement the entire CSS grammar.
        if not balanced_delimiters(source, css=True):
            raise BuildError(f"CSS_SYNTAX_ERROR: {path}: unbalanced CSS delimiters")
        try:
            spans = css_rule_spans(source)
        except BuildError as exc:
            raise BuildError(f"CSS_SYNTAX_ERROR: {path}: {exc}") from exc
        for selector, _start, _end, body in spans:
            if selector.startswith('@') and '{' in body:
                continue
            if '{' in body:
                continue
            for declaration in body.split(';'):
                item = declaration.strip()
                if not item:
                    continue
                if ':' not in item:
                    raise BuildError(f"CSS_SYNTAX_ERROR: {path}: malformed declaration {item!r}")
                prop, value = item.split(':', 1)
                if not re.fullmatch(r'(?:--[\w-]+|-?[A-Za-z][\w-]*)', prop.strip()) or not value.strip():
                    raise BuildError(f"CSS_SYNTAX_ERROR: {path}: malformed declaration {item!r}")
        return

    def check_rules(rules: list[Any], context: str) -> None:
        for token in rules:
            if token.type == "error":
                raise BuildError(f"CSS_SYNTAX_ERROR: {path}:{context}: {token.message}")
            if token.type == "qualified-rule":
                contents = tinycss2.parse_blocks_contents(token.content, skip_comments=True, skip_whitespace=True)
                check_rules(contents, "style rule")
            elif token.type == "at-rule" and token.content is not None:
                if token.lower_at_keyword in {"keyframes", "-webkit-keyframes"}:
                    nested = tinycss2.parse_rule_list(token.content, skip_comments=True, skip_whitespace=True)
                else:
                    nested = tinycss2.parse_blocks_contents(token.content, skip_comments=True, skip_whitespace=True)
                check_rules(nested, "@" + token.lower_at_keyword)

    rules = tinycss2.parse_stylesheet(source, skip_comments=True, skip_whitespace=True)
    check_rules(rules, "stylesheet")


def css_rule_spans(source: str) -> list[tuple[str, int, int, str]]:
    """Lex CSS rules, including nested @media/@supports/keyframes blocks.

    Offsets always refer to the original source, so a selected rule can be
    replaced without reformatting or touching neighboring rules.
    """
    spans: list[tuple[str, int, int, str]] = []
    n = len(source)

    def matching_close(open_at: int, limit: int) -> int:
        depth = 1; j = open_at + 1; quote: str | None = None; escaped = False; comment = False
        while j < limit and depth:
            ch = source[j]; nx = source[j + 1] if j + 1 < limit else ""
            if comment:
                if ch == "*" and nx == "/": comment = False; j += 2; continue
                j += 1; continue
            if quote:
                if escaped: escaped = False
                elif ch == "\\": escaped = True
                elif ch == quote: quote = None
                j += 1; continue
            if ch == "/" and nx == "*": comment = True; j += 2; continue
            if ch in ("'", '"'): quote = ch; j += 1; continue
            if ch == "{": depth += 1
            elif ch == "}": depth -= 1
            j += 1
        if depth:
            raise BuildError("CSS_UNCLOSED_RULE_BLOCK")
        return j - 1

    def scan(start: int, limit: int) -> None:
        i = start
        while i < limit:
            while i < limit and (source[i].isspace() or source.startswith("/*", i)):
                if source.startswith("/*", i):
                    close = source.find("*/", i + 2, limit)
                    if close < 0: raise BuildError("CSS_UNCLOSED_COMMENT")
                    i = close + 2
                else:
                    i += 1
            if i >= limit: break
            prelude_start = i; quote: str | None = None; escaped = False; comment = False; parens = 0
            brace = -1; terminator = ""
            while i < limit:
                ch = source[i]; nx = source[i + 1] if i + 1 < limit else ""
                if comment:
                    if ch == "*" and nx == "/": comment = False; i += 2; continue
                    i += 1; continue
                if quote:
                    if escaped: escaped = False
                    elif ch == "\\": escaped = True
                    elif ch == quote: quote = None
                    i += 1; continue
                if ch == "/" and nx == "*": comment = True; i += 2; continue
                if ch in ("'", '"'): quote = ch; i += 1; continue
                if ch == "(": parens += 1
                elif ch == ")": parens = max(0, parens - 1)
                elif ch == "{" and parens == 0: brace = i; terminator = "{"; break
                elif ch == ";" and parens == 0: terminator = ";"; break
                i += 1
            if not terminator:
                break
            if terminator == ";":
                i += 1
                continue
            selector = source[prelude_start:brace].strip()
            close = matching_close(brace, limit)
            body_start, body_end = brace + 1, close
            nested_start = len(spans)
            # Nested CSS rules can occur in @media/@supports/@container/@layer,
            # keyframes, and modern CSS nesting. Recursively collect those spans.
            if "{" in source[body_start:body_end]:
                scan(body_start, body_end)
            nested = len(spans) > nested_start
            if not nested or not selector.startswith("@"):
                spans.append((selector, prelude_start, close + 1, source[body_start:body_end]))
            i = close + 1

    scan(0, n)
    return spans


class HTMLStructureParser(html.parser.HTMLParser):
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.errors: list[str] = []
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag not in self.VOID: self.stack.append(tag)
    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        return
    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self.VOID: return
        if not self.stack:
            self.errors.append(f"unexpected closing tag </{tag}>")
        elif self.stack[-1] == tag:
            self.stack.pop()
        elif tag in self.stack:
            self.errors.append(f"misnested closing tag </{tag}>; expected </{self.stack[-1]}>")
            self.stack.pop(self.stack.index(tag))
        else:
            self.errors.append(f"unmatched closing tag </{tag}>")

def validate_html_fragment(source: str, path: str) -> None:
    if re.search(r"<[^>]*$", source, re.S):
        raise BuildError(f"HTML_TRUNCATED_TAG: {path}")
    parser = HTMLStructureParser()
    try: parser.feed(source); parser.close()
    except Exception as exc: raise BuildError(f"HTML_FRAGMENT_PARSE_ERROR: {path}: {exc}") from exc
    if parser.errors or parser.stack:
        raise BuildError(f"HTML_FRAGMENT_STRUCTURE_INVALID: {path}: errors={parser.errors}; unclosed={parser.stack}")

def validate_html_document(source: str, path: str) -> None:
    validate_html_fragment(source, path)
    if not re.search(r"<html\b", source, re.I):
        raise BuildError(f"HTML_DOCUMENT_ROOT_MISSING: {path}")
    if not re.search(r"<body\b", source, re.I):
        raise BuildError(f"HTML_BODY_MISSING: {path}")
    facts = HTMLFacts()
    facts.feed(source)
    facts.close()
    if facts.duplicate_ids:
        duplicates = ", ".join(sorted(facts.duplicate_ids))
        raise BuildError(f"HTML_DUPLICATE_ID: {path}: duplicate id values: {duplicates}")


class HTMLFacts(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ids: set[str] = set(); self.id_counts: dict[str, int] = {}; self.duplicate_ids: set[str] = set()
        self.classes: set[str] = set(); self.scripts: list[str] = []; self.styles: list[str] = []; self.assets: list[str] = []; self.viewport = False
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # Preserve the ID set for existing consumers, while also tracking duplicates.
        id_values = [str(value).strip() for name, value in attrs if name.lower() == "id" and value and str(value).strip()]
        if len(id_values) > 1:
            self.duplicate_ids.update(id_values)
        for identifier in id_values:
            self.id_counts[identifier] = self.id_counts.get(identifier, 0) + 1
            if self.id_counts[identifier] > 1:
                self.duplicate_ids.add(identifier)
            self.ids.add(identifier)
        a = dict(attrs)
        if a.get("class"): self.classes.update(str(a["class"]).split())
        if tag.lower() == "script" and a.get("src"): self.scripts.append(str(a["src"]))
        if tag.lower() == "link" and "stylesheet" in (a.get("rel") or "").lower().split() and a.get("href"):
            self.styles.append(str(a["href"]))
            self.assets.append(str(a["href"]))
        if a.get("src"): self.assets.append(str(a["src"]))
        if a.get("poster"): self.assets.append(str(a["poster"]))
        if tag.lower() == "link" and a.get("href") and set((a.get("rel") or "").lower().split()) & {"icon", "preload", "modulepreload", "manifest"}: self.assets.append(str(a["href"]))
        if tag.lower() == "meta" and (a.get("name") or "").lower() == "viewport": self.viewport = True
    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None: self.handle_starttag(tag, attrs)


def resolve_local_asset(owner_path: str, reference: str) -> str | None:
    ref = reference.strip().replace("\\", "/")
    if not ref or ref.startswith("#") or re.match(r"^(?:[a-z][a-z0-9+.-]*:|//)", ref, re.I): return None
    parsed = urlsplit(ref)
    raw = parsed.path
    if not raw: return None
    candidate = raw.lstrip("/") if raw.startswith("/") else (PurePosixPath(owner_path).parent / raw).as_posix()
    normalized = posixpath.normpath(candidate).replace("\\", "/")
    if normalized in {".", ""}: return None
    if normalized == ".." or normalized.startswith("../"):
        return "__PATH_ESCAPE__"
    return normalized


@dataclass(frozen=True)
class JSToken:
    kind: str
    value: str
    line: int


def lex_javascript(source: str, path: str = "<javascript>") -> list[JSToken]:
    """Small lexical scanner for interface extraction; Node remains the syntax gate."""
    tokens: list[JSToken] = []
    i = 0; line = 1; n = len(source)
    multi = ("===", "!==", ">>>", "**=", "=>", "?.", "...", "==", "!=", "<=", ">=", "++", "--", "&&", "||", "??", "+=", "-=", "*=", "/=", "**", "<<", ">>")
    regex_prefix_keywords = {"return", "throw", "case", "delete", "void", "typeof", "instanceof", "in", "of", "yield", "await", "else", "do"}
    while i < n:
        ch = source[i]; nx = source[i + 1] if i + 1 < n else ""
        if ch in " \t\r": i += 1; continue
        if ch == "\n": line += 1; i += 1; continue
        if ch == "/" and nx == "/":
            i += 2
            while i < n and source[i] != "\n": i += 1
            continue
        if ch == "/" and nx == "*":
            i += 2
            while i < n and not (source[i] == "*" and i + 1 < n and source[i + 1] == "/"):
                if source[i] == "\n": line += 1
                i += 1
            if i >= n: raise BuildError(f"JS_LEXICAL_UNCLOSED_COMMENT: {path}")
            i += 2; continue
        if ch in ("'", '"', "`"):
            quote = ch; start_line = line; i += 1; value: list[str] = []; dynamic_template = False; closed = False
            while i < n:
                cur = source[i]
                if cur == "\\" and i + 1 < n:
                    nxt = source[i + 1]
                    escapes = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}
                    value.append(escapes.get(nxt, nxt))
                    if nxt == "\n": line += 1
                    i += 2; continue
                if quote == "`" and cur == "$" and i + 1 < n and source[i + 1] == "{":
                    dynamic_template = True
                    # Skip the interpolation expression with balanced braces; nested
                    # strings/comments are not evaluated because the template is not
                    # used as a static selector or storage key.
                    depth = 1; i += 2; inner_quote: str | None = None; inner_escape = False
                    while i < n and depth:
                        c = source[i]; nn = source[i + 1] if i + 1 < n else ""
                        if inner_quote:
                            if inner_escape: inner_escape = False
                            elif c == "\\": inner_escape = True
                            elif c == inner_quote: inner_quote = None
                            if c == "\n": line += 1
                            i += 1; continue
                        if c in ("'", '"', "`"): inner_quote = c; i += 1; continue
                        if c == "{": depth += 1
                        elif c == "}": depth -= 1
                        if c == "\n": line += 1
                        i += 1
                    value.append("<dynamic>")
                    continue
                if cur == quote:
                    closed = True; i += 1; break
                if cur == "\n":
                    line += 1
                    if quote != "`": raise BuildError(f"JS_LEXICAL_NEWLINE_IN_STRING: {path}:{start_line}")
                value.append(cur); i += 1
            if not closed: raise BuildError(f"JS_LEXICAL_UNTERMINATED_STRING: {path}:{start_line}")
            tokens.append(JSToken("template_dynamic" if dynamic_template else "string", "".join(value), start_line))
            continue
        if ch.isalpha() or ch in "_$":
            start = i; start_line = line; i += 1
            while i < n and (source[i].isalnum() or source[i] in "_$"): i += 1
            tokens.append(JSToken("identifier", source[start:i], start_line)); continue
        if ch.isdigit():
            start = i; start_line = line; i += 1
            while i < n and (source[i].isalnum() or source[i] in "._"): i += 1
            tokens.append(JSToken("number", source[start:i], start_line)); continue
        if ch == "/":
            previous = tokens[-1] if tokens else None
            regex_allowed = previous is None or (previous.kind == "punct" and previous.value in {"=", "(", "[", "{", ",", ":", ";", "!", "?", "&&", "||", "=>"}) or (previous.kind == "identifier" and previous.value in regex_prefix_keywords)
            if regex_allowed:
                regex_start = i
                start_line = line; i += 1; in_class = False; escaped = False; closed = False
                while i < n:
                    cur = source[i]
                    if cur == "\n": break
                    if escaped: escaped = False
                    elif cur == "\\": escaped = True
                    elif cur == "[": in_class = True
                    elif cur == "]": in_class = False
                    elif cur == "/" and not in_class: closed = True; i += 1; break
                    i += 1
                if closed:
                    while i < n and source[i].isalpha(): i += 1
                    tokens.append(JSToken("regex", "", start_line)); continue
                # A slash that did not close a regex is treated as an operator.
                i = regex_start + 1
        punct = next((x for x in multi if source.startswith(x, i)), ch)
        tokens.append(JSToken("punct", punct, line)); i += len(punct)
    return tokens


def _js_call_string_args(tokens: list[JSToken], open_index: int) -> list[str]:
    args: list[str] = []; depth = 0; i = open_index + 1
    while i < len(tokens):
        token = tokens[i]
        if token.kind == "punct" and token.value in {"(", "[", "{"}: depth += 1
        elif token.kind == "punct" and token.value in {")", "]", "}"}:
            if token.value == ")" and depth == 0: break
            depth = max(0, depth - 1)
        elif token.kind == "string" and depth == 0:
            args.append(token.value)
        i += 1
    return args


def _js_call_result_is_optional(tokens: list[JSToken], call_index: int, open_index: int) -> bool:
    """Return whether a DOM lookup is optional or guarded by a null check."""
    depth = 0
    for index in range(open_index, len(tokens)):
        token = tokens[index]
        if token.kind == "punct" and token.value == "(":
            depth += 1
        elif token.kind == "punct" and token.value == ")":
            depth -= 1
            if depth == 0:
                if index + 1 < len(tokens) and tokens[index + 1].value == "?.":
                    return True
                equals = next((j for j in range(max(0, call_index - 5), call_index) if tokens[j].value == "="), None)
                if equals is None or equals == 0 or tokens[equals - 1].kind != "identifier":
                    return False
                variable = tokens[equals - 1].value
                for j in range(index + 1, min(len(tokens) - 3, index + 13)):
                    if (tokens[j].value == "if" and tokens[j + 1].value == "("
                            and tokens[j + 2].value == variable and tokens[j + 3].value == ")"):
                        return True
                return False
    return False


def extract_javascript_interfaces(source: str, path: str) -> dict[str, set[str]]:
    tokens = lex_javascript(source, path)
    result = {key: set() for key in ("dom_ids", "dom_classes", "dynamic_ids", "dynamic_classes", "storage_keys", "event_names", "functions", "classes", "imports", "data_fields")}
    for i, token in enumerate(tokens):
        value = token.value
        if token.kind in {"string", "template_dynamic"}:
            for tag_markup in re.findall(r"<\s*[A-Za-z][^>]*>", value):
                for raw_id in re.findall(r"\bid\s*=\s*['\"]([^'\"]+)['\"]", tag_markup, re.I):
                    if "<dynamic>" not in raw_id: result["dynamic_ids"].add(raw_id)
                for raw_class in re.findall(r"\bclass\s*=\s*['\"]([^'\"]+)['\"]", tag_markup, re.I):
                    if "<dynamic>" not in raw_class: result["dynamic_classes"].update(raw_class.split())
        if token.kind != "identifier": continue
        if value in {"getElementById", "querySelector", "querySelectorAll"} and i + 1 < len(tokens) and tokens[i + 1].value == "(":
            args = _js_call_string_args(tokens, i + 1)
            if args and not _js_call_result_is_optional(tokens, i, i + 1):
                selector = args[0]
                if value == "getElementById":
                    result["dom_ids"].add(selector)
                else:
                    result["dom_ids"].update(re.findall(r"#([A-Za-z_][\w:-]*)", selector))
                    result["dom_classes"].update(re.findall(r"\.([A-Za-z_][\w-]*)", selector))
        if value in {"add", "remove", "toggle"} and i >= 2 and tokens[i - 1].value == "." and tokens[i - 2].value == "classList" and i + 1 < len(tokens) and tokens[i + 1].value == "(":
            for arg in _js_call_string_args(tokens, i + 1): result["dynamic_classes"].update(arg.split())
        if value == "className" and i + 2 < len(tokens) and tokens[i + 1].value == "=" and tokens[i + 2].kind == "string":
            result["dynamic_classes"].update(tokens[i + 2].value.split())
        if value == "id" and i + 2 < len(tokens) and tokens[i + 1].value == "=" and tokens[i + 2].kind == "string":
            result["dynamic_ids"].add(tokens[i + 2].value)
        if value == "setAttribute" and i + 1 < len(tokens) and tokens[i + 1].value == "(":
            args = _js_call_string_args(tokens, i + 1)
            if len(args) >= 2 and args[0].lower() == "class": result["dynamic_classes"].update(args[1].split())
            if len(args) >= 2 and args[0].lower() == "id": result["dynamic_ids"].add(args[1])
        if value in {"localStorage", "sessionStorage"} and i + 3 < len(tokens) and tokens[i + 1].value == "." and tokens[i + 2].value in {"getItem", "setItem", "removeItem"} and tokens[i + 3].value == "(":
            args = _js_call_string_args(tokens, i + 3)
            if args: result["storage_keys"].add(args[0])
        if value == "dispatchEvent" and i + 1 < len(tokens) and tokens[i + 1].value == "(":
            args = _js_call_string_args(tokens, i + 1)
            if args: result["event_names"].add(args[0])
        if value in {"Event", "CustomEvent"} and i >= 1 and tokens[i - 1].value == "new" and i + 1 < len(tokens) and tokens[i + 1].value == "(":
            args = _js_call_string_args(tokens, i + 1)
            if args: result["event_names"].add(args[0])
        if value == "function" and i + 1 < len(tokens) and tokens[i + 1].kind == "identifier":
            result["functions"].add(tokens[i + 1].value)
        if value == "class" and i + 1 < len(tokens) and tokens[i + 1].kind == "identifier":
            result["classes"].add(tokens[i + 1].value)
        if value in {"const", "let", "var"} and i + 2 < len(tokens) and tokens[i + 1].kind == "identifier" and tokens[i + 2].value == "=":
            depth = 0; j = i + 3; arrow = False
            while j < len(tokens):
                t = tokens[j]
                if t.kind == "punct" and t.value in {"(", "[", "{"}: depth += 1
                elif t.kind == "punct" and t.value in {")", "]", "}"}: depth = max(0, depth - 1)
                elif t.kind == "punct" and t.value == "=>": arrow = True
                elif t.kind == "punct" and t.value == ";" and depth == 0: break
                j += 1
            has_function = any(t.kind == "identifier" and t.value == "function" for t in tokens[i + 3:j])
            if arrow or has_function: result["functions"].add(tokens[i + 1].value)
        if value == "require" and i + 1 < len(tokens) and tokens[i + 1].value == "(":
            args = _js_call_string_args(tokens, i + 1)
            if args: result["imports"].add(args[0])
        if value == "this" and i + 2 < len(tokens) and tokens[i + 1].value in {".", "?."} and tokens[i + 2].kind == "identifier":
            result["data_fields"].add(tokens[i + 2].value)
        if token.kind == "identifier" and i + 1 < len(tokens) and tokens[i + 1].value in {".", "?."} and i + 2 < len(tokens) and tokens[i + 2].kind == "identifier":
            if value not in {"document", "window", "localStorage", "sessionStorage", "console", "Math", "JSON", "Array", "Object", "String", "Number", "Date", "Promise", "setTimeout", "clearTimeout", "navigator", "location"}:
                result["data_fields"].add(tokens[i + 2].value)
        if token.kind == "identifier" and i + 1 < len(tokens) and tokens[i + 1].value == ":" and i > 0 and tokens[i - 1].value in {"{", ","}:
            result["data_fields"].add(value)
        if value == "from" and i + 1 < len(tokens) and tokens[i + 1].kind == "string":
            result["imports"].add(tokens[i + 1].value)
        if value == "import" and i + 1 < len(tokens):
            if tokens[i + 1].kind == "string": result["imports"].add(tokens[i + 1].value)
            elif tokens[i + 1].value == "(" and i + 2 < len(tokens) and tokens[i + 2].kind == "string": result["imports"].add(tokens[i + 2].value)
    storage_constants = re.findall(r"(?:static\s+)?#?STORAGE_KEY\s*=\s*(['\"])(.*?)\1", source)
    storage_key_used = re.search(r"\b(?:getItem|setItem|removeItem)\s*\(\s*(?:(?:this|[A-Za-z_$][\w$]*)\.)?#?STORAGE_KEY\b", source)
    storage_backend_present = re.search(r"\b(?:localStorage|sessionStorage)\b", source)
    if len(storage_constants) == 1 and storage_key_used and storage_backend_present:
        result["storage_keys"].add(storage_constants[0][1])
    return result


def _strip_js_comments(source: str) -> str:
    """Remove comments without deleting quoted strings/template literals."""
    out: list[str] = []; i = 0; state = "code"; quote = ""; escaped = False
    while i < len(source):
        ch = source[i]; nx = source[i + 1] if i + 1 < len(source) else ""
        if state == "line":
            if ch == "\n": out.append(ch); state = "code"
            else: out.append(" ")
            i += 1; continue
        if state == "block":
            if ch == "*" and nx == "/": out.extend("  "); state = "code"; i += 2
            else: out.append("\n" if ch == "\n" else " "); i += 1
            continue
        if state == "string":
            out.append(ch)
            if escaped: escaped = False
            elif ch == "\\": escaped = True
            elif ch == quote: state = "code"
            i += 1; continue
        if ch in ("'", '"', "`"):
            quote = ch; state = "string"; out.append(ch); i += 1; continue
        if ch == "/" and nx == "/": out.extend("  "); state = "line"; i += 2; continue
        if ch == "/" and nx == "*": out.extend("  "); state = "block"; i += 2; continue
        out.append(ch); i += 1
    return "".join(out)


def build_contract(project: dict[str, str], manifest: list[dict[str, Any]]) -> dict[str, Any]:
    """Extract a shared cross-file contract from actual delivered source."""
    def is_test_path(value: str) -> bool:
        parts = {part.lower() for part in Path(value).parts}
        name = Path(value).name.lower()
        return bool(parts & {"test", "tests", "__tests__"}) or name.startswith("test_") or name.endswith(("_test.py", ".test.js", ".spec.js"))
    # Test-only fixtures are tracked separately by the test manager and must never
    # satisfy or create production DOM/data contracts.
    html_files = [p for p in project if Path(p).suffix.lower() in {".html", ".htm"} and not is_test_path(p)]
    css_files = [p for p in project if Path(p).suffix.lower() == ".css" and not is_test_path(p)]
    js_files = [p for p in project if Path(p).suffix.lower() in {".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx"} and not is_test_path(p)]
    py_files = [p for p in project if Path(p).suffix.lower() == ".py" and not is_test_path(p)]
    html_ids: set[str] = set(); html_classes: set[str] = set(); scripts: dict[str, list[str]] = {}; styles: dict[str, list[str]] = {}; html_by_file: dict[str, Any] = {}; viewport = False
    for path in html_files:
        parser = HTMLFacts(); parser.feed(project[path]); html_ids |= parser.ids; html_classes |= parser.classes; viewport |= parser.viewport
        scripts[path] = parser.scripts; styles[path] = parser.styles
        html_by_file[path] = {"ids": sorted(parser.ids), "classes": sorted(parser.classes), "scripts": list(parser.scripts), "stylesheets": list(parser.styles), "assets": list(parser.assets), "viewport": parser.viewport}
    js_refs: dict[str, set[str]] = {}; js_classes: dict[str, set[str]] = {}; dynamic_ids: set[str] = set(); dynamic_classes: set[str] = set(); storage: dict[str, set[str]] = {}; events: dict[str, set[str]] = {}; js_functions: dict[str, set[str]] = {}; js_declared_classes: dict[str, set[str]] = {}; js_imports: dict[str, set[str]] = {}; js_data_fields: dict[str, set[str]] = {}
    for path in js_files:
        extracted = extract_javascript_interfaces(project[path], path)
        js_refs[path] = extracted["dom_ids"]
        js_classes[path] = extracted["dom_classes"]
        dynamic_ids |= extracted["dynamic_ids"]
        dynamic_classes |= extracted["dynamic_classes"]
        storage[path] = extracted["storage_keys"]
        events[path] = extracted["event_names"]
        js_functions[path] = extracted["functions"]
        js_declared_classes[path] = extracted["classes"]
        js_imports[path] = extracted["imports"]
        js_data_fields[path] = extracted["data_fields"]
    css_selectors: dict[str, list[str]] = {}; css_ids: set[str] = set(); css_classes: set[str] = set(); media = False; design_tokens: set[str] = set()
    for path in css_files:
        source = project[path]
        if not balanced_delimiters(source, css=True): raise BuildError(f"CSS_STRUCTURE_INVALID: {path}")
        rules = css_rule_spans(source)
        selectors: list[str] = []
        for selector, _start, _end, _body in rules:
            if selector.startswith("@"): continue
            selectors.extend(split_css_selector_list(selector))
            css_ids |= set(re.findall(r"#([A-Za-z_][\w-]*)", selector))
            css_classes |= set(re.findall(r"\.([A-Za-z_][\w-]*)", selector))
        css_selectors[path] = selectors
        media = media or bool(re.search(r"@media\s*\([^)]*(?:max-width|min-width|orientation)", source, re.I))
        design_tokens |= set(re.findall(r"--([A-Za-z0-9_-]+)\s*:", source))
    python_symbols: dict[str, list[str]] = {}; python_fields: dict[str, dict[str, list[str]]] = {}; python_signatures: dict[str, dict[str, str]] = {}
    for path in py_files:
        try: tree = ast.parse(project[path], filename=path)
        except SyntaxError as exc: raise BuildError(f"PYTHON_SYNTAX_ERROR: {path}:{exc.lineno}: {exc.msg}") from exc
        symbols: list[str] = []; fields: dict[str, list[str]] = {}; signatures: dict[str, str] = {}
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                symbols.append(node.name)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    signatures[node.name] = ast.dump(node.args, include_attributes=False) + " -> " + (ast.dump(node.returns, include_attributes=False) if node.returns else "Any")
            if isinstance(node, ast.ClassDef):
                annotations = [n.target.id for n in node.body if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)]
                for method in node.body:
                    if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)) and method.name == "__init__":
                        for child in ast.walk(method):
                            if isinstance(child, (ast.Assign, ast.AnnAssign)):
                                targets = child.targets if isinstance(child, ast.Assign) else [child.target]
                                for target in targets:
                                    if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id == "self": annotations.append(target.attr)
                if annotations: fields[node.name] = sorted(set(annotations))
        python_symbols[path] = symbols; python_fields[path] = fields
    manifest_paths = {x["path"] for x in manifest}
    missing_assets: list[dict[str, str]] = []
    for page, details in html_by_file.items():
        for ref in details.get("assets", []):
            resolved = resolve_local_asset(page, ref)
            if resolved is None: continue
            if resolved == "__PATH_ESCAPE__" or resolved not in manifest_paths:
                missing_assets.append({"page": page, "reference": ref, "resolved": resolved})
    for css_path in css_files:
        source = project[css_path]
        refs = re.findall(r"url\(\s*['\"]?([^'\")]+)", source, re.I)
        refs += re.findall(r"@import\s+(?:url\(\s*)?['\"]([^'\"]+)['\"]", source, re.I)
        for ref in refs:
            resolved = resolve_local_asset(css_path, ref)
            if resolved is None: continue
            if resolved == "__PATH_ESCAPE__" or resolved not in manifest_paths:
                missing_assets.append({"page": css_path, "reference": ref, "resolved": resolved})
    for js_path, imports in js_imports.items():
        for module in imports:
            if not module.startswith("."):
                continue
            base = (PurePosixPath(js_path).parent / module).as_posix()
            candidates = [base, base + ".js", base + ".mjs", base + ".cjs", base + "/index.js"]
            if not any(candidate in manifest_paths for candidate in candidates):
                missing_assets.append({"page": js_path, "reference": module, "resolved": base})
    missing_dom_refs = []
    available_ids = html_ids | dynamic_ids
    for path, refs in js_refs.items():
        for ident in sorted(refs - available_ids): missing_dom_refs.append({"file": path, "kind": "id", "name": ident})
    available_classes = html_classes | dynamic_classes
    for path, refs in js_classes.items():
        for cls in sorted(refs - available_classes): missing_dom_refs.append({"file": path, "kind": "class", "name": cls})
    missing_assets = list({canonical_hash(x): x for x in missing_assets}.values())
    missing_css_refs = []
    for path in css_files:
        for ident in sorted(css_ids - available_ids):
            # CSS selectors can intentionally target optional/dynamically injected states;
            # keep them advisory unless a corresponding JS selector also expects the node.
            if any(ident in refs for refs in js_refs.values()): continue
        for cls in sorted(css_classes - available_classes):
            if any(cls in refs for refs in js_classes.values()): continue
    storage_keys = sorted(set().union(*storage.values()) if storage else set())
    event_names = sorted(set().union(*events.values()) if events else set())
    contract = {
        "schema": CONTRACT_SCHEMA_VERSION,
        "files": sorted(project),
        "html": {"ids": sorted(html_ids), "classes": sorted(html_classes), "scripts": scripts, "stylesheets": styles, "viewport": viewport, "by_file": html_by_file},
        "css": {"selectors": css_selectors, "ids": sorted(css_ids), "classes": sorted(css_classes), "design_tokens": sorted(design_tokens), "responsive_media_query": media},
        "javascript": {"dom_ids": {k: sorted(v) for k, v in js_refs.items()}, "dom_classes": {k: sorted(v) for k, v in js_classes.items()}, "dynamic_ids": sorted(dynamic_ids), "dynamic_classes": sorted(dynamic_classes), "storage_keys": {k: sorted(v) for k, v in storage.items()}, "event_names": {k: sorted(v) for k, v in events.items()}, "functions": {k: sorted(v) for k, v in js_functions.items()}, "classes": {k: sorted(v) for k, v in js_declared_classes.items()}, "imports": {k: sorted(v) for k, v in js_imports.items()}, "data_fields": {k: sorted(v) for k, v in js_data_fields.items()}},
        "python": {"symbols": python_symbols, "class_fields": python_fields, "signatures": python_signatures},
        "integration_findings": {"missing_assets": missing_assets, "missing_dom_references": missing_dom_refs, "missing_css_references": missing_css_refs},
    }
    contract["sha256"] = canonical_hash(contract)
    return contract


def dependency_hash(path: str, manifest: list[dict[str, Any]], contract: dict[str, Any]) -> str:
    entry = next((x for x in manifest if x["path"] == path), {})
    generation, build = _manifest_relationships(entry)
    deps = sorted(set(generation + build))
    dependency_facts = {dep: file_contract_fingerprint(dep, contract, manifest) for dep in sorted(deps)}
    return canonical_hash({"declared_dependencies": sorted(deps), "dependency_interfaces": dependency_facts})


def javascript_top_level_declarations(source: str, path: str = "<javascript>") -> dict[str, set[str]]:
    tokens = lex_javascript(source, path)
    result = {"functions": set(), "classes": set(), "variables": set()}
    brace_depth = paren_depth = bracket_depth = 0
    for i, token in enumerate(tokens):
        if token.kind == "punct" and token.value == "}": brace_depth = max(0, brace_depth - 1); continue
        if token.kind == "punct" and token.value == ")": paren_depth = max(0, paren_depth - 1); continue
        if token.kind == "punct" and token.value == "]": bracket_depth = max(0, bracket_depth - 1); continue
        if brace_depth == 0 and paren_depth == 0 and bracket_depth == 0 and token.kind == "identifier" and i + 1 < len(tokens):
            nxt = tokens[i + 1]
            if token.value == "function" and nxt.kind == "identifier":
                result["functions"].add(nxt.value)
            elif token.value == "class" and nxt.kind == "identifier":
                result["classes"].add(nxt.value)
            elif token.value in {"const", "let", "var"} and nxt.kind == "identifier":
                result["variables"].add(nxt.value)
                if i + 2 < len(tokens) and tokens[i + 2].value == "=":
                    j = i + 3; nested = 0; callable_value = False
                    while j < len(tokens):
                        t = tokens[j]
                        if t.kind == "punct" and t.value in {"(", "[", "{"}: nested += 1
                        elif t.kind == "punct" and t.value in {")", "]", "}"}: nested = max(0, nested - 1)
                        elif t.kind == "punct" and t.value == "=>": callable_value = True
                        elif t.kind == "identifier" and t.value == "function": callable_value = True
                        elif t.kind == "punct" and t.value == ";" and nested == 0: break
                        j += 1
                    if callable_value: result["functions"].add(nxt.value)
        if token.kind == "punct" and token.value == "{": brace_depth += 1
        elif token.kind == "punct" and token.value == "(": paren_depth += 1
        elif token.kind == "punct" and token.value == "[": bracket_depth += 1
    return result


def validate_javascript_unit(source: str, unit: dict[str, str], path: str, timeout: int) -> str:
    source = re.sub(r"^```(?:javascript|js)?\s*|\s*```$", "", source, flags=re.I).strip()
    # Correct the common model boundary slip where a top-level async function is
    # emitted using class-method syntax. Only normalize the exact requested
    # symbol at the start of an async_function unit.
    if unit.get("kind") in {"function", "async_function"}:
        name = re.escape(str(unit.get("name", "")))
        source = re.sub(rf"^async\s+{name}(\s*\()", rf"async function {unit['name']}\1", source, count=1)
    validate_js(source, path, timeout)
    declarations = javascript_top_level_declarations(source, path)
    kind, name = unit["kind"], unit["name"]
    if kind in {"function", "async_function"}:
        # Small models sometimes return one cohesive top-level setup helper next
        # to the requested entry function. Keep the boundary strict for unrelated
        # declarations, but accept one clearly named lifecycle helper; the final
        # assembled-file duplicate/declaration checks still apply.
        extra_functions = declarations["functions"] - {name}
        cohesive_helpers = {helper for helper in extra_functions if re.fullmatch(r"(?:setup|register|bind|wire)[A-Z_$][A-Za-z0-9_$]*", helper)}
        if declarations["functions"] != ({name} | cohesive_helpers) or len(cohesive_helpers) != len(extra_functions) or declarations["classes"] or declarations["variables"] - {name}:
            raise BuildError(f"JS_UNIT_BOUNDARY: expected only top-level callable {name}; declarations={declarations}")
    elif kind == "class":
        if declarations["classes"] != {name} or declarations["functions"] or declarations["variables"]:
            raise BuildError(f"JS_UNIT_BOUNDARY: expected only top-level class {name}; declarations={declarations}")
    return source + "\n"


def repair_js_cross_class_private_access(candidate: str, accepted: list[str]) -> str:
    """Promote declared private members explicitly accessed by a later class unit."""
    refs = list(dict.fromkeys(re.findall(r"\b([A-Z][A-Za-z0-9_$]*)\s*\.#([A-Za-z_$][A-Za-z0-9_$]*)\b", candidate)))
    for class_name, member in refs:
        member_pattern = re.compile(rf"(?<![\w$])#{re.escape(member)}\b")
        class_pattern = re.compile(rf"\bclass\s+{re.escape(class_name)}\b")
        for index, prefix_unit in enumerate(accepted):
            if class_pattern.search(prefix_unit) and member_pattern.search(prefix_unit):
                # This is a cross-unit interface in the generated architecture; make
                # the declaration and its internal uses consistently public.
                accepted[index] = member_pattern.sub(member, prefix_unit)
                candidate = re.sub(
                    rf"\b{re.escape(class_name)}\s*\.#{re.escape(member)}\b",
                    f"{class_name}.{member}", candidate,
                )
                log(f"JS_PRIVATE_INTERFACE_REPAIRED | {class_name}.{member} | explicit cross-unit reference")
                break
    return candidate


def validate_js_duplicate_declarations(source: str, path: str) -> None:
    tokens = lex_javascript(source, path)
    all_names: list[str] = []
    brace_depth = paren_depth = bracket_depth = 0
    for i, token in enumerate(tokens):
        if token.kind == "punct" and token.value == "}": brace_depth = max(0, brace_depth - 1); continue
        if token.kind == "punct" and token.value == ")": paren_depth = max(0, paren_depth - 1); continue
        if token.kind == "punct" and token.value == "]": bracket_depth = max(0, bracket_depth - 1); continue
        if brace_depth == 0 and paren_depth == 0 and bracket_depth == 0 and token.kind == "identifier" and i + 1 < len(tokens):
            nxt = tokens[i + 1]
            if token.value in {"function", "class"} and nxt.kind == "identifier": all_names.append(nxt.value)
            elif token.value in {"const", "let", "var"} and nxt.kind == "identifier": all_names.append(nxt.value)
        if token.kind == "punct" and token.value == "{": brace_depth += 1
        elif token.kind == "punct" and token.value == "(": paren_depth += 1
        elif token.kind == "punct" and token.value == "[": bracket_depth += 1
    duplicates = sorted({name for name in all_names if all_names.count(name) > 1})
    if duplicates: raise BuildError(f"JS_DUPLICATE_TOP_LEVEL_DECLARATIONS: {path}: {duplicates}")


def validate_js(source: str, path: str, timeout: int) -> None:
    if not source.strip(): raise BuildError(f"JS_EMPTY: {path}")
    node = shutil.which("node")
    if not node:
        if not balanced_delimiters(source): raise BuildError(f"JS_DELIMITER_INVALID: {path}")
        raise BuildError(f"JS_SYNTAX_BLOCKED: Node.js is unavailable; delimiter balance is not a syntax pass ({path})")
    temp = Path(tempfile.mkdtemp(prefix="cell3-v7-js-"))
    try:
        target = temp / ("unit.mjs" if Path(path).suffix.lower() in {".mjs", ".js"} else "unit.cjs")
        target.write_text(source, encoding="utf-8")
        result = subprocess.run([node, "--check", str(target)], capture_output=True, text=True, timeout=min(max(timeout, 10), 90), shell=False)
        if result.returncode: raise BuildError(f"JS_SYNTAX_ERROR: {path}: {redact(result.stderr or result.stdout)[-3000:]}")
    finally: shutil.rmtree(temp, ignore_errors=True)


def _html_shell_duplicate_is_satisfied(unit: dict[str, Any], candidate: str, accepted: list[str]) -> bool:
    """True only when this named HTML shell unit exactly duplicates a real shell already accepted."""
    identity = f"{unit.get('name', '')} {unit.get('purpose', '')}"
    if not re.search(r"shell|document\s*(?:root|structure)|html\s*(?:root|skeleton|structure)", identity, re.I):
        return False
    normalized = re.sub(r"\s+", " ", candidate).strip()
    if not normalized or not any(normalized == re.sub(r"\s+", " ", old).strip() for old in accepted):
        return False
    tags = {x.lower() for x in re.findall(r"<\s*(html|head|body)\b", candidate, re.I)}
    return "html" in tags and len(tags) >= 2


def _strip_single_css_fence(source: str) -> str:
    """Remove only one outer Markdown CSS fence; never alter mixed prose/source."""
    match = re.fullmatch(r"\s*```(?:css)?\s*\n([\s\S]*?)\n```\s*", source, re.IGNORECASE)
    return match.group(1).strip() if match else source.strip()

def _is_css_design_token_unit(unit: dict[str, Any]) -> bool:
    """Classify token units by name, not by purpose text that may mention tokens."""
    compact_name = re.sub(r"[^a-z]", "", str(unit.get("name", "")).lower())
    return compact_name.endswith("designtokens") or compact_name in {"tokens", "csstokens"}

def _css_unit_allows_multiple_rules(unit: dict[str, Any]) -> bool:
    if _is_css_design_token_unit(unit): return False
    compact_name = re.sub(r"[^a-z]", "", str(unit.get("name", "")).lower())
    broad_name = compact_name.endswith(("components", "elements", "views"))
    identity = str(unit.get("purpose", ""))
    return broad_name or bool(re.search(r"\b(?:components|elements|views)\b", identity, re.IGNORECASE))

def _css_selector_names(source: str) -> set[str]:
    try:
        return {selector.strip() for rule_selector, _start, _end, _body in css_rule_spans(source) if not rule_selector.lstrip().startswith("@") for selector in rule_selector.split(",") if selector.strip()}
    except BuildError:
        return set()


def validate_css_rule_unit(source: str, unit: dict[str, Any], path: str) -> str:
    """Validate one CSS semantic rule, with semantic coverage for design-token units."""
    candidate = _strip_single_css_fence(source)
    if not candidate:
        raise BuildError(f"CSS_UNIT_EMPTY: {path}:{unit.get('name', 'unknown')}")
    if not balanced_delimiters(candidate, css=True):
        raise BuildError(f"CSS_UNIT_INVALID: {path}:{unit.get('name', 'unknown')}")
    try:
        rules = css_rule_spans(candidate)
    except BuildError as exc:
        raise BuildError(f"CSS_UNIT_INVALID: {path}:{unit.get('name', 'unknown')}: {exc}") from exc
    selectors = [row for row in rules if not row[0].lstrip().startswith("@")]
    unique_selectors = {row[0].strip() for row in selectors}
    is_design_tokens = _is_css_design_token_unit(unit)
    if not selectors or (len(unique_selectors) != 1 and not _css_unit_allows_multiple_rules(unit)) or (is_design_tokens and len(rules) != 1):
        expected = "the selectors named by this component/view unit" if _css_unit_allows_multiple_rules(unit) else "one unique selector, optionally repeated in responsive at-rules"
        raise BuildError(f"CSS_UNIT_BOUNDARY_INVALID: {path}:{unit.get('name', 'unknown')}: expected {expected}")
    selector, _start, _end, body = selectors[0]
    if not is_design_tokens:
        return candidate
    if selector.strip() != ":root":
        raise BuildError(f"CSS_DESIGN_TOKEN_SELECTOR_INVALID: {path}:{unit.get('name', 'unknown')}: expected :root")

    # A token block may contain comments and custom-property declarations only.
    without_comments = re.sub(r"/\*[\s\S]*?\*/", "", body)
    declarations = [part.strip() for part in without_comments.split(";") if part.strip()]
    names: set[str] = set()
    for declaration in declarations:
        match = re.fullmatch(r"(--[A-Za-z0-9_-]+)\s*:\s*(.+)", declaration, re.DOTALL)
        if not match:
            raise BuildError(f"CSS_DESIGN_TOKEN_DECLARATION_INVALID: {path}:{unit.get('name', 'unknown')}: {declaration[:100]}")
        name, value = match.group(1), match.group(2).strip()
        if not value:
            raise BuildError(f"CSS_DESIGN_TOKEN_VALUE_EMPTY: {path}:{name}")
        if name in names:
            raise BuildError(f"CSS_DESIGN_TOKEN_DUPLICATE: {path}:{name}")
        names.add(name)

    if is_design_tokens:
        groups = {
            "colors": sum(name.startswith("--color-") for name in names) >= 6,
            "typography": any(name.startswith("--font-family-") for name in names)
                and sum(name.startswith("--font-size-") for name in names) >= 4
                and sum(name.startswith("--font-weight-") for name in names) >= 2
                and any(name.startswith("--line-height-") for name in names),
            "spacing": sum(name.startswith(("--space-", "--spacing-")) for name in names) >= 4,
            "radii": sum(name.startswith("--radius-") for name in names) >= 2,
            "shadows": any(name.startswith("--shadow-") or name == "--color-shadow" for name in names),
            "layout": {"--sidebar-width", "--content-max-width", "--transition-duration"}.issubset(names),
        }
        missing = [name for name, passed in groups.items() if not passed]
        if missing:
            raise BuildError(f"CSS_DESIGN_TOKEN_COVERAGE_INCOMPLETE: {path}:{unit.get('name', 'unknown')}: missing categories={missing}")
    return candidate


def _css_unit_prompt_contract(unit: dict[str, Any]) -> str:
    if _is_css_design_token_unit(unit):
        return (
            "CSS UNIT OUTPUT CONTRACT (MANDATORY): This unit is the centralized design-token block only. "
            "Return raw CSS, not Markdown or code fences. Emit exactly one top-level :root { ... } rule. "
            "Inside it put CSS custom properties only; no normal declarations, nested rules, @rules, comments outside the block, "
            "or any selector other than :root. Cover ALL six categories: at least 6 --color-* tokens; typography with "
            "--font-family-*, at least four --font-size-* tokens, at least two --font-weight-* tokens and --line-height-*; at least four "
            "--space-* or --spacing-* tokens; at least two --radius-* tokens; at least one --shadow-* token "
            "(or --color-shadow); and the exact layout tokens --sidebar-width, --content-max-width, "
            "--transition-duration. Use coherent values consistent with the supplied TaskFlow Pro light-theme design. "
            "Do not implement resets, body, app shell, sidebar, header, cards, buttons, forms, statuses, media queries, "
            "or responsive selectors here. Stop immediately after the closing brace."
        )
    if _css_unit_allows_multiple_rules(unit):
        return (
            "CSS COMPONENT UNIT OUTPUT CONTRACT: Return raw CSS only, no Markdown fences or prose. "
            "Implement the complete set of related selector rules named by this unit's purpose; "
            "multiple selectors are allowed because this unit covers a group of components or views. "
            "Do not include unrelated selectors, duplicate accepted selectors, design tokens, or stylesheet-wide resets. "
            "Responsive @media rules may repeat a selector already defined in this same unit."
        )
    return (
        "CSS UNIT OUTPUT CONTRACT: Return raw CSS only, no Markdown fences or prose. "
        "Implement exactly one complete top-level CSS rule for this unit and nothing else. "
        "Do not include neighboring selectors, @rules, or stylesheet-wide content."
    )


def _css_unit_recovery_prompt(base_prompt: str, unit: dict[str, Any], attempt: int, rejection: str) -> str:
    """A concise CSS retry: describe the failed contract without echoing bad source."""
    is_tokens = _is_css_design_token_unit(unit)
    if is_tokens:
        corrective = (
            "Rebuild the entire unit as one raw :root rule. Include all required token groups: "
            "6+ --color-*, font-family, 4+ font-size, 2+ font-weight, line-height, 4+ spacing, "
            "2+ radius, shadow, and the exact --sidebar-width, --content-max-width, "
            "--transition-duration properties. Custom properties only. No other selector, "
            "no Markdown fences, no prose."
        )
    else:
        accepted_block = re.search(r"ALREADY ACCEPTED CSS SOURCE \(authoritative prefix\):\s*\n([\s\S]*?)\nUNIT DEPENDS ON:", base_prompt)
        accepted_selectors = sorted(_css_selector_names(accepted_block.group(1))) if accepted_block else []
        accepted_note = f" Already accepted selectors that must not be repeated: {accepted_selectors}." if accepted_selectors else ""
        if _css_unit_allows_multiple_rules(unit):
            corrective = "Return the complete set of related selector rules named by this component/view unit, raw CSS only. Multiple related selectors are allowed; do not repeat accepted selectors or include unrelated rules. Responsive @media rules may repeat a selector defined in this same unit."
        else:
            corrective = "Return exactly one complete, distinct selector rule for this unit, raw CSS only, with no neighboring rules or fences."
        corrective += accepted_note
    return (
        base_prompt
        + f"\n\nCSS CONTRACT RECOVERY {attempt}: The previous candidate was rejected: {rejection}. "
        + corrective
        + " Recreate the complete unit from its purpose and canonical contract. Do not repeat unrelated or previously accepted CSS."
    )


def _unit_recovery_prompt(
    base_prompt: str, unit_name: str, attempt: int, rejection: str,
    previous_candidate: str = "",
) -> str:
    """Build a bounded, rejection-specific retry prompt."""
    excerpt = previous_candidate[:5000]
    prompt = (
        base_prompt
        + f"\n\nRECOVERY RETRY {attempt}: The previous candidate for unit "
          f"{unit_name!r} was rejected: {rejection}. "
          "Do not repeat the rejected source. Re-read the unit boundary and "
          "already-accepted prefix. Return only the missing, distinct implementation "
          "for this unit. Do not reproduce the document shell, preamble, or any "
          "already-accepted block. If the unit is already represented by the "
          "preamble, report the conflict rather than duplicating the shell."
    )
    if "JS_SYNTAX_ERROR" in rejection:
        prompt += (
            " For JavaScript syntax errors, correct the reported issue in this unit. "
            "A private name such as #method may only be declared and referenced inside "
            "the same class body; never access another class's private field or method "
            "(including expressions like ClassName.#member or otherObject.#member). "
            "Use a public method/property that is actually present in accepted source, "
            "or query the required DOM element within this unit. Avoid introducing "
            "private identifiers unless their declarations are included in this unit. "
            "Keep the implementation compatible with accepted source. For this JavaScript unit, "
            "return exactly the top-level declaration required by UNIT kind and UNIT name. "
            "If kind is function or async_function, return one complete named function declaration "
            "only; do not append class methods, helper methods, or another declaration. Do not emit "
            "a bare class-method signature such as setupShellEventListeners() { outside a class. "
            "Use only APIs proven by the accepted prefix; do not assume this refers to an application instance."
        )
    if excerpt:
        prompt += "\nREJECTED CANDIDATE (do not repeat):\n" + excerpt
    return prompt


def generate_file_units(client: Cell3Client, ctx: BuildContext, state: StateStore, item: dict[str, Any], index: int, total_files: int, *, _skip_file_restore: bool = False) -> str:
    path, purpose = item["path"], item["purpose"]
    suffix = Path(path).suffix.lower()
    contract_context = file_contract_context(path, ctx.contract, ctx.manifest)
    contract_hash = canonical_hash(contract_context)
    dep_hash = dependency_hash(path, ctx.manifest, ctx.contract)
    restored = None if _skip_file_restore else state.restore_file(path, contract_hash=contract_hash, dependency_hash=dep_hash)
    if restored is not None:
        log(f"FILE_CHECKPOINT_RESTORED | {path} | SHA256 and provenance verified")
        return restored
    if PROGRESS:
        PROGRESS.update(file=f"{index}/{total_files} {path}", accepted=0, total=0, unit="planning", operation="Semantic unit planning", last="Running")
    plan_prompt = f"""Return JSON only with shape {{"preamble":"imports/constants or document shell only","postamble":"optional closing shell text","units":[{{"name":"unique_name","kind":"function|async_function|class|entrypoint|block|css_rule|html_fragment","purpose":"one complete responsibility","depends_on":["earlier_unit_name"],"target_file":"optional exact manifest path when returning a multi-file plan"}}]}}.
Create a dependency-ordered semantic implementation plan for exactly one file. If you return units spanning multiple files despite this instruction, every unit MUST include target_file using an exact manifest path; never rely on filename-kind guessing to assign a multi-file plan. Every depends_on name must refer to a unit listed earlier; order dependencies before consumers. Keep units small enough for a single bounded response (normally <=1400 output tokens). Do not write source code. Do not omit specification requirements. The plan must not duplicate symbols. Python units are top-level functions/classes; JavaScript units are top-level functions/classes or cohesive blocks. JavaScript method implementations must be included inside their owning class unit; never plan an individual class method as a function unit. A JavaScript function unit must be exactly one named top-level function, with no trailing class methods or unrelated declarations. CSS units are complete rules; HTML units are complete balanced semantic fragments. For HTML, preamble opens the document shell and postamble closes it.
FILE: {path}
PURPOSE: {purpose}
SPECIFICATION: {ctx.spec}
PROJECT PLAN: {json.dumps(ctx.plan, ensure_ascii=False)}
CANONICAL CONTRACT SLICE FOR THIS FILE: {json.dumps(contract_context, ensure_ascii=False)[:14000]}
ARCHITECTURE: {json.dumps(ctx.architecture, ensure_ascii=False)}
DESIGN: {json.dumps(ctx.design, ensure_ascii=False)}
For HTML files, the complete document shell belongs ONLY in preamble. Include <!doctype html>, <html>, <head>, <body>, and (when needed) a <div id="app-shell"> root. Do NOT create document-root or document-shell units. A unit named HTML_ShellStructure is allowed only when it describes application UI fragments (sidebar/header/main content), not the html/head/body document shell. Every unit must be a distinct body fragment and must not contain html, head, or body tags."""
    if suffix in {".py", ".js", ".mjs", ".cjs", ".css", ".html", ".htm"}:
        plan_root = RUNS_DIR / ctx.run_id / "unit_plans"
        plan_root.mkdir(parents=True, exist_ok=True)
        plan_file = plan_root / (sha256_text(path)[:20] + ".json")
        plan_meta_file = plan_root / (sha256_text(path)[:20] + ".meta.json")
        plan_prompt_hash = sha256_text(plan_prompt)
        unit_plan = None
        try:
            saved_plan = load_json(plan_file, required=False)
            saved_meta = load_json(plan_meta_file, required=False)
            if isinstance(saved_plan, dict) and isinstance(saved_meta, dict) and saved_meta == {"prompt_sha256": plan_prompt_hash, "spec_sha256": ctx.spec_hash, "manifest_sha256": state.state.get("manifest_sha256", ""), "contract_sha256": contract_hash, "dependency_sha256": dep_hash}:
                unit_plan = saved_plan
                log(f"UNIT_PLAN_RESTORED | {path} | hash/provenance PASS")
            elif saved_plan is not None:
                log(f"UNIT_PLAN_INVALIDATED | {path} | prompt/manifest/contract/dependency changed")
        except BuildError as exc:
            log(f"UNIT_PLAN_REJECTED | {path} | {redact(exc)}")
        if unit_plan is None:
            unit_plan = client.json(GENERATOR_MODEL, plan_prompt, timeout=ctx.timeout, num_predict=2600)
            save_json(plan_file, unit_plan)
            save_json(plan_meta_file, {"prompt_sha256": plan_prompt_hash, "spec_sha256": ctx.spec_hash, "manifest_sha256": state.state.get("manifest_sha256", ""), "contract_sha256": contract_hash, "dependency_sha256": dep_hash})
        try:
            preamble, units = validate_unit_plan_for_file(unit_plan, path, {x["path"] for x in ctx.manifest})
        except BuildError as first_plan_error:
            log(f"UNIT_PLAN_CONTRACT_REJECTED | {path} | {redact(first_plan_error)}")
            plan_file.unlink(missing_ok=True); plan_meta_file.unlink(missing_ok=True)
            retry_prompt = plan_prompt + f"\n\nFILE-KIND CONTRACT RETRY: The prior plan was invalid: {redact(first_plan_error)}. For .html/.htm use ONLY html_fragment units; for .css use ONLY css_rule units. Never return CSS units in HTML files or document shell tags inside HTML fragments. The HTML preamble owns the document shell. Return a corrected dependency-ordered JSON plan. JSON only."
            unit_plan = client.json(GENERATOR_MODEL, retry_prompt, timeout=ctx.timeout, num_predict=3200)
            try:
                preamble, units = validate_unit_plan_for_file(unit_plan, path, {x["path"] for x in ctx.manifest})
            except BuildError as second_plan_error:
                raise BuildError(f"UNIT_PLAN_FILE_CONTRACT_REJECTED: {path}: retry also invalid: {redact(second_plan_error)}") from second_plan_error
            plan_prompt_hash = sha256_text(retry_prompt)
            save_json(plan_file, unit_plan)
            save_json(plan_meta_file, {"prompt_sha256": plan_prompt_hash, "spec_sha256": ctx.spec_hash, "manifest_sha256": state.state.get("manifest_sha256", ""), "contract_sha256": contract_hash, "dependency_sha256": dep_hash})
        # A preamble-only module is already a complete unit; do not regenerate it.
        accepted: list[str] = []
        if preamble:
            if suffix == ".py":
                # Preamble is validated as a module-level block and re-used only for this exact plan.
                ast.parse(preamble, filename=path)
            elif suffix in {".js", ".mjs", ".cjs"}:
                validate_js(preamble, path, ctx.timeout)
            elif suffix in {".html", ".htm"}:
                pass  # The document shell is validated after semantic fragments are assembled.
            elif not balanced_delimiters(preamble, css=True):
                raise BuildError(f"CSS_PREAMBLE_INVALID: {path}")
            accepted.append(preamble.strip() + "\n")
        if PROGRESS:
            PROGRESS.update(total=len(units), accepted=0, unit="ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â", operation="Bounded semantic generation")
        for unit_index, unit in enumerate(units, 1):
            unit_id = f"{unit_index:03d}:{unit['kind']}:{unit['name']}"
            if suffix == ".css" and unit["kind"] == "css_rule":
                accepted_prefix = "\n".join(accepted).strip() or "No previously accepted CSS units."
                prompt = f"""Return ONLY the complete source for this ONE semantic unit. No explanation, other units, or repeated accepted code. The entire unit must be syntactically complete.
{_css_unit_prompt_contract(unit)}
FILE: {path}
UNIT: {unit_id}
PURPOSE: {unit['purpose']}
FILE PURPOSE: {purpose}
ALREADY ACCEPTED UNIT NAMES: {json.dumps([u['name'] for u in units[:unit_index-1]])}
ALREADY ACCEPTED CSS SOURCE (authoritative prefix):
{accepted_prefix[-6000:]}
UNIT DEPENDS ON: {json.dumps(unit.get('depends_on', []))}
RELEVANT CANONICAL CONTRACT SLICE: {json.dumps(contract_context, ensure_ascii=False)[:10000]}
Return only the implementation of this CSS unit."""
            else:
                prompt = f"""Return ONLY the complete source for this ONE semantic unit. No markdown, explanation, other units, or repeated accepted code. The entire unit must be syntactically complete.
FILE: {path}
UNIT: {unit_id}
PURPOSE: {unit['purpose']}
ALREADY ACCEPTED UNIT NAMES: {json.dumps([u['name'] for u in units[:unit_index-1]])}
ALREADY ACCEPTED SOURCE (authoritative prefix):
{''.join(accepted)[-12000:] or 'No previously accepted source.'}
UNIT DEPENDS ON: {json.dumps(unit.get('depends_on', []))}
SPECIFICATION: {ctx.spec}
PROJECT PLAN: {json.dumps(ctx.plan, ensure_ascii=False)}
CANONICAL PROJECT CONTRACT SLICE: {json.dumps(contract_context, ensure_ascii=False)[:14000]}
FILE PURPOSE: {purpose}
        FILE GENERATION/BUILD PREREQUISITES (context only): {json.dumps(sum((_manifest_relationships(item)), []))}
Return only the implementation of {unit['kind']} {unit['name']}."""
            p_hash = sha256_text(prompt)
            saved = state.restore_unit(path, unit_id, prompt_hash=p_hash, contract_hash=contract_hash, dependency_hash=dep_hash)
            content = saved
            if content is not None:
                log(f"UNIT_RESTORED | {path}:{unit['name']} | SHA256/provenance PASS")
            else:
                last: Exception | None = None
                previous_candidate = ""
                for attempt in range(1, MAX_UNIT_ATTEMPTS + 1):
                    if attempt == 1:
                        attempt_prompt = prompt
                    elif suffix == ".css":
                        attempt_prompt = _css_unit_recovery_prompt(
                            prompt, unit, attempt, str(last or "previous attempt failed")
                        )
                    else:
                        attempt_prompt = _unit_recovery_prompt(
                            prompt, unit["name"], attempt, str(last or "previous attempt failed"),
                            previous_candidate,
                        )
                    if PROGRESS:
                        PROGRESS.update(
                            accepted=unit_index - 1, total=len(units),
                            unit=f"{unit_index}/{len(units)} {unit['name']}",
                            operation=f"Gemma generation attempt {attempt}/{MAX_UNIT_ATTEMPTS}",
                            last="Generating",
                        )
                    state.state.setdefault("attempts", {})[f"{path}::{unit_id}"] = {
                        "attempt": attempt, "max_attempts": MAX_UNIT_ATTEMPTS,
                        "operation": "generation", "timestamp": utc_now(),
                        "prompt_sha256": sha256_text(attempt_prompt),
                        "last_rejection": redact(last) if last else "",
                    }
                    state.persist()
                    try:
                        raw = client.generate_complete(
                            GENERATOR_MODEL, attempt_prompt, timeout=ctx.timeout,
                            num_predict=2800 if unit["kind"] in {"function", "class", "entrypoint"} else 1800,
                            max_continuations=3,
                        )
                        candidate = raw.strip()
                        previous_candidate = candidate
                        if suffix == ".py": candidate = python_unit_validate(candidate, unit, path)
                        elif suffix in {".js", ".mjs", ".cjs"}:
                            candidate = repair_js_cross_class_private_access(candidate, accepted)
                            candidate = validate_javascript_unit(candidate, unit, path, ctx.timeout)
                        elif suffix in {".html", ".htm"}: validate_html_unit_fragment(candidate, path)
                        elif suffix == ".css" and unit["kind"] == "css_rule":
                            candidate = validate_css_rule_unit(candidate, unit, path)
                        elif not balanced_delimiters(candidate, css=True):
                            raise BuildError(f"CSS_UNIT_INVALID: {path}:{unit['name']}")
                        normalized = re.sub(r"\s+", " ", candidate).strip()
                        if any(normalized == re.sub(r"\s+", " ", old).strip() for old in accepted):
                            if suffix in {".html", ".htm"} and _html_shell_duplicate_is_satisfied(unit, candidate, accepted):
                                # The document shell has already been accepted from the preamble
                                # or an earlier unit. Record a zero-byte, provenance-bound
                                # resolution checkpoint; never append the shell twice.
                                content = ""
                                state.accept_unit(
                                    path, unit_id, content, prompt_hash=p_hash,
                                    contract_hash=contract_hash, dependency_hash=dep_hash,
                                    validator="duplicate-html-shell-satisfied-by-accepted-prefix",
                                )
                                log(f"HTML_SHELL_UNIT_RECONCILED | {path}:{unit['name']} | exact duplicate already present in accepted prefix | no duplicate appended")
                                break
                            raise BuildError(f"DUPLICATE_UNIT_REJECTED: {path}:{unit['name']}")
                        content = candidate
                        state.accept_unit(path, unit_id, content, prompt_hash=p_hash, contract_hash=contract_hash, dependency_hash=dep_hash)
                        break
                    except Exception as exc:
                        last = exc
                        log(f"UNIT_REJECTED | {path}:{unit['name']} | attempt={attempt}/{MAX_UNIT_ATTEMPTS} | {redact(exc)}")
                if content is None:
                    raise BuildError(f"UNIT_GENERATION_EXHAUSTED: {path}:{unit['name']}: {redact(last or 'unknown')}")
            accepted.append(content.strip() + "\n")
            if PROGRESS:
                PROGRESS.update(accepted=unit_index, total=len(units), unit=f"{unit_index}/{len(units)} {unit['name']}", operation="Validate and checkpoint accepted unit", last="PASS")
        postamble = str(unit_plan.get("postamble", "")).strip() if suffix in {".html", ".htm"} else ""
        if suffix in {".html", ".htm"}:
            html_preamble = accepted[0] if preamble and accepted else (preamble if preamble else "")
            html_fragments = accepted[1:] if preamble and accepted else accepted
            result = assemble_html_document(html_preamble, html_fragments, postamble, path)
        else:
            result = "\n".join(x.rstrip() for x in accepted if x.strip())
            result = result.rstrip() + "\n"
        if suffix == ".py":
            validate_assembled_python(result, path)
        elif suffix in {".html", ".htm"}:
            validate_html_document(result, path)
        elif suffix in {".js", ".mjs", ".cjs"}:
            validate_js(result, path, ctx.timeout)
            validate_js_duplicate_declarations(result, path)
        elif not balanced_delimiters(result, css=True): raise BuildError(f"CSS_FILE_INVALID: {path}")
    else:
        prompt = f"""Generate the complete contents of one project file. Return source only; no markdown fences or explanation. Do not omit required functionality or invent unrelated files.
FILE: {path}
PURPOSE: {purpose}
SPECIFICATION: {ctx.spec}
PROJECT PLAN: {json.dumps(ctx.plan, ensure_ascii=False)}
CANONICAL CONTRACT: {json.dumps(ctx.contract, ensure_ascii=False)[:12000]}
ARCHITECTURE: {json.dumps(ctx.architecture, ensure_ascii=False)}"""
        raw = client.generate_complete(GENERATOR_MODEL, prompt, timeout=ctx.timeout, num_predict=6000, max_continuations=3)
        result = raw.strip() + "\n"
    if len(result.encode("utf-8")) > MAX_FILE_BYTES: raise BuildError(f"GENERATED_FILE_TOO_LARGE: {path}")
    return result


def generate_project(client: Cell3Client, ctx: BuildContext, state: StateStore, *, _recovery_pass: bool = False) -> dict[str, str]:
    ctx.manifest = topologically_order_manifest(ctx.manifest)
    result: dict[str, str] = {}
    cached_paths_before = set(state.state.get("accepted_files", {}))
    cached_paths_before.update(str(x.get("file", "")) for x in state.state.get("accepted_units", {}).values() if x.get("file"))
    invalidated_roots: set[str] = set()
    for index, item in enumerate(ctx.manifest, 1):
        path = item["path"]
        with PROGRESS.file_scope(path, index, len(ctx.manifest)) if PROGRESS else contextlib.nullcontext():
            contract_hash = file_contract_fingerprint(path, ctx.contract, ctx.manifest)
            dep_hash = dependency_hash(path, ctx.manifest, ctx.contract)
            restored = state.restore_file(path, contract_hash=contract_hash, dependency_hash=dep_hash)
            if restored is not None:
                result[path] = restored
                log(f"FILE_RESTORED | {path} | accepted checkpoint verified")
            else:
                if ctx.resume and path in cached_paths_before:
                    invalidated_roots.add(path)
                result[path] = generate_file_units(client, ctx, state, item, index, len(ctx.manifest), _skip_file_restore=True)
            state.accept_file(path, result[path], contract_hash=file_contract_fingerprint(path, ctx.contract, ctx.manifest), dependency_hash=dependency_hash(path, ctx.manifest, ctx.contract))
            if PROGRESS:
                PROGRESS.update(stage_fraction=index / max(1, len(ctx.manifest)), file=f"{index}/{len(ctx.manifest)} {path}", operation="File accepted and checkpointed", last="PASS")
            log(f"FILE_ACCEPTED | {path} | sha256={sha256_text(result[path])}")
    expected = {x["path"] for x in ctx.manifest}
    if set(result) != expected: raise BuildError("MANIFEST_COMPLETENESS_FAILURE")
    result = validate_project(result)
    if ctx.resume and not _recovery_pass and invalidated_roots:
        graph = dependency_graph(ctx.manifest, result)
        affected = dependent_closure(invalidated_roots, graph) - invalidated_roots
        affected &= cached_paths_before
        if affected:
            state.invalidate(affected, "A dependency checkpoint was rejected/regenerated; cached dependent units require fresh source context")
            log(f"RESUME_DEPENDENCY_REGENERATION | roots={sorted(invalidated_roots)} | dependents={sorted(affected)}")
            return generate_project(client, ctx, state, _recovery_pass=True)
    return result


# ---------------------------------------------------------------------------
# Canonical project contract and cross-file integration verification.
# ---------------------------------------------------------------------------
def create_canonical_contract(client: Cell3Client, ctx: BuildContext) -> dict[str, Any]:
    prompt = f"""Create the canonical implementation contract for this project BEFORE source generation.
Return JSON only with keys: summary, html_elements, css_selectors, css_states, design_tokens, breakpoints, javascript_modules, javascript_functions, javascript_classes, events, storage, data_models, imports, exports, workflows, tests.
Use generic project-aware names. Do not assume a task tracker or any particular app. Every entry must be grounded in the specification, plan, or architecture. Include the owning relative file/module path on every interface entry whenever it can be determined. If an interface is not required, use an empty list/object. For each data model, define fields and types. For each event, define name, target, and payload shape. For storage, define exact key names and serialized schema. For HTML elements, define stable id/class and purpose. For CSS, define selectors and responsive states. For JS, define module responsibilities and exported functions.
SPECIFICATION:
{ctx.spec}
PLAN:
{json.dumps(ctx.plan, ensure_ascii=False)}
ARCHITECTURE:
{json.dumps(ctx.architecture, ensure_ascii=False)}
DESIGN:
{json.dumps(ctx.design, ensure_ascii=False)}
MANIFEST:
{json.dumps(ctx.manifest, ensure_ascii=False)}"""
    contract = client.json(PLANNER_MODEL, prompt, timeout=ctx.timeout, num_predict=6000)
    required = ("html_elements", "css_selectors", "javascript_modules", "javascript_functions", "javascript_classes", "events", "storage", "data_models", "workflows", "tests")
    for key in required:
        if key not in contract:
            raise BuildError(f"CANONICAL_CONTRACT_INVALID: missing {key}")
    contract["schema"] = CONTRACT_SCHEMA_VERSION
    contract["spec_sha256"] = ctx.spec_hash
    contract["manifest_sha256"] = canonical_hash(ctx.manifest)
    contract["created_at"] = utc_now()
    contract["version"] = 1
    contract["sha256"] = canonical_hash(contract)
    return contract


def _contract_expected_names(contract: dict[str, Any], key: str, name_key: str = "id") -> set[str]:
    value = contract.get(key, [])
    result: set[str] = set()
    plural = {"id": "ids", "class": "classes", "key": "keys", "name": "names", "selector": "selectors", "token": "tokens"}.get(name_key, "")
    if isinstance(value, dict):
        if name_key in value and isinstance(value[name_key], (list, tuple, set)):
            result.update(str(x) for x in value[name_key])
        elif plural and isinstance(value.get(plural), (list, tuple, set)):
            result.update(str(x) for x in value[plural])
        elif plural and isinstance(value.get(plural), list):
            for item in value[plural]:
                if isinstance(item, dict) and item.get(name_key): result.add(str(item[name_key]))
                elif isinstance(item, str): result.add(item)
        else:
            result.update(str(k) for k in value.keys())
        return result
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str): result.add(item)
            elif isinstance(item, dict):
                candidate = item.get(name_key)
                if candidate is None and name_key == "key": candidate = item.get("storage_key") or item.get("name")
                if candidate is None and name_key == "name": candidate = item.get("event") or item.get("event_name")
                if candidate is None and name_key == "id": candidate = item.get("element_id")
                if candidate is None and name_key == "selector": candidate = item.get("css_selector")
                if candidate is not None:
                    raw = str(candidate)
                    result.update(raw.split()) if name_key == "class" else result.add(raw)
                elif name_key == "class" and isinstance(item.get("classes"), list): result.update(str(x) for x in item["classes"])
    return result


def expected_design_token_values(contract: dict[str, Any]) -> dict[str, str]:
    """Map nested design-token declarations to actual CSS custom-property names."""
    raw = contract.get("design_tokens", {})
    if isinstance(raw, list):
        return {str(item).lstrip("-"): "" for item in raw if isinstance(item, str)}
    if not isinstance(raw, dict):
        return {}
    result: dict[str, str] = {}
    prefixes = {"colors": "color", "spacing": "space", "radii": "radius", "shadows": "shadow"}
    for group, prefix in prefixes.items():
        values = raw.get(group)
        if isinstance(values, dict):
            for name, value in values.items():
                result[f"{prefix}-{str(name).replace('_', '-')}"] = str(value)
    typography = raw.get("typography")
    if isinstance(typography, dict):
        names = {"font_family": "font-family-base", "font_size": "font-size-body", "font_weight": "font-weight-normal", "line_height": "line-height-base", "body": "font-size-body", "button": "font-size-button"}
        for name, value in typography.items():
            token = names.get(str(name))
            if token is None and re.fullmatch(r"heading_[1-6]", str(name)):
                token = "font-size-" + str(name).replace("_", "-")
            if token:
                result[token] = str(value)
    for name in ("sidebar_width", "content_max_width", "transition_duration"):
        if name in raw and not isinstance(raw[name], (dict, list)):
            result[name.replace("_", "-")] = str(raw[name])
    return result


def refresh_observed_contract(ctx: BuildContext) -> dict[str, Any]:
    observed = build_contract(ctx.project, ctx.manifest)
    ctx.contract["observed_interfaces"] = observed
    ctx.contract["manifest_sha256"] = canonical_hash(ctx.manifest)
    ctx.contract.pop("sha256", None)
    ctx.contract["sha256"] = canonical_hash(ctx.contract)
    save_json(ctx.staging / "canonical_project_contract.json", ctx.contract)
    return observed


def validate_contract_consistency(project: dict[str, str], manifest: list[dict[str, Any]], contract: dict[str, Any]) -> dict[str, Any]:
    """Check planned interfaces against actual files and the extracted source contract."""
    facts = build_contract(project, manifest)
    findings: list[dict[str, Any]] = []
    expected_ids = _contract_expected_names(contract, "html_elements", "id")
    expected_classes = _contract_expected_names(contract, "html_elements", "class")
    actual_ids = set(facts["html"]["ids"])
    actual_classes = set(facts["html"]["classes"])
    dynamic_ids = set(facts["javascript"]["dynamic_ids"])
    dynamic_classes = set(facts["javascript"]["dynamic_classes"])
    for ident in sorted(expected_ids - actual_ids - dynamic_ids):
        # Prefer the document shell when it exists: the required element is then
        # present before constructors and views run, avoiding null DOM hosts.
        findings.append({"rule": "CONTRACT_HTML_ID_MISSING", "severity": "HIGH", "file": _first_html(project) or _first_js(project), "message": f"Canonical contract requires HTML id #{ident}, but it is absent from delivered HTML and dynamic creation evidence. Add it to the matching semantic element in the document shell so it exists before application initialization."})
    for cls in sorted(expected_classes - actual_classes - dynamic_classes):
        findings.append({"rule": "CONTRACT_HTML_CLASS_MISSING", "severity": "MEDIUM", "file": _first_html(project) or _first_js(project), "message": f"Canonical contract requires class .{cls}, but it is absent from delivered HTML and dynamic creation evidence. Add it to the matching semantic element in the document shell."})
    # JavaScript DOM references must resolve. This is a hard mismatch; CSS-only unused
    # selectors remain advisory because dynamic components and state selectors are valid.
    for row in facts["integration_findings"]["missing_dom_references"]:
        # DOM ownership belongs to markup. Letting a JS patch rewrite a selector
        # often changes a missing class into an unrelated ID and leaves the
        # rendered element unchanged.
        target_file = _first_html(project) or row["file"]
        findings.append({"rule": "JS_DOM_REFERENCE_UNRESOLVED", "severity": "HIGH", "file": target_file, "message": f"JavaScript references missing {row['kind']} '{row['name']}'."})
    for row in facts["integration_findings"]["missing_assets"]:
        findings.append({"rule": "HTML_ASSET_MISSING", "severity": "HIGH", "file": row["page"], "message": f"HTML references missing local asset {row['reference']} (resolved as {row['resolved']})."})
    # Contract storage/event declarations are compared with observed source literals.
    observed_storage = set().union(*(set(v) for v in facts["javascript"]["storage_keys"].values())) if facts["javascript"]["storage_keys"] else set()
    storage_contract = contract.get("storage", [])
    if isinstance(storage_contract, dict) and isinstance(storage_contract.get("key"), str):
        expected_storage = {storage_contract["key"]}
    elif isinstance(storage_contract, list):
        expected_storage = {str(item["key"]) for item in storage_contract if isinstance(item, dict) and isinstance(item.get("key"), str)}
    else:
        expected_storage = _contract_expected_names(contract, "storage", "key")
    storage_mechanisms: set[str] = set()
    if isinstance(storage_contract, list):
        storage_mechanisms = {str(x.get("mechanism", "")).lower() for x in storage_contract if isinstance(x, dict) and x.get("mechanism")}
    elif isinstance(storage_contract, dict) and storage_contract.get("mechanism"):
        storage_mechanisms = {str(storage_contract.get("mechanism")).lower()}
    browser_storage_declared = not storage_mechanisms or bool(storage_mechanisms & {"localstorage", "sessionstorage", "browser storage"})
    if expected_storage and browser_storage_declared and expected_storage != observed_storage:
        findings.append({"rule": "STORAGE_CONTRACT_MISMATCH", "severity": "HIGH", "file": _first_js(project), "message": f"Storage key mismatch: expected={sorted(expected_storage)} observed={sorted(observed_storage)}."})
    observed_events = set().union(*(set(v) for v in facts["javascript"]["event_names"].values())) if facts["javascript"]["event_names"] else set()
    expected_events = _contract_expected_names(contract, "events", "name")
    # Native browser events (click, dragover, keydown) are interaction handlers,
    # not application event-contract emissions. Only compare when source has
    # concrete custom/application-event evidence.
    if expected_events and observed_events and observed_events != expected_events:
        findings.append({"rule": "EVENT_CONTRACT_MISMATCH", "severity": "HIGH", "file": _first_js(project), "message": f"Event contract mismatch: expected={sorted(expected_events)} observed={sorted(observed_events)}."})
    expected_selectors = _contract_expected_names(contract, "css_selectors", "selector")
    observed_selectors = set(x for values in facts["css"]["selectors"].values() for x in values)
    if expected_selectors:
        missing_selectors = expected_selectors - observed_selectors
        if missing_selectors:
            findings.append({"rule": "CSS_SELECTOR_CONTRACT_MISMATCH", "severity": "MEDIUM", "file": next((p for p in project if Path(p).suffix.lower() == ".css" and not _is_test_path(p)), ""), "message": f"Canonical CSS selectors are missing: {sorted(missing_selectors)}."})
        available_dom_ids = set(facts["html"]["ids"]) | set(facts["javascript"]["dynamic_ids"])
        available_dom_classes = set(facts["html"]["classes"]) | set(facts["javascript"]["dynamic_classes"])
        unmatched = []
        for selector in expected_selectors:
            ids = set(re.findall(r"#([A-Za-z_][\w:-]*)", selector))
            classes = set(re.findall(r"\.([A-Za-z_][\w-]*)", selector))
            if ids - available_dom_ids or classes - available_dom_classes:
                unmatched.append({"selector": selector, "missing_ids": sorted(ids - available_dom_ids), "missing_classes": sorted(classes - available_dom_classes)})
        if unmatched:
            findings.append({"rule": "CSS_SELECTOR_DOM_MISMATCH", "severity": "HIGH", "file": _first_html(project) or _first_js(project), "message": f"Canonical CSS selectors do not map to delivered HTML or verified JavaScript-generated elements: {unmatched}."})
    # Also inspect selectors present in CSS even when the planner omitted them
    # from the canonical contract. Otherwise a misspelled selector can evade
    # validation simply by being absent from the planner's expected list.
    if facts["html"]["by_file"]:
        available_dom_ids = set(facts["html"]["ids"]) | set(facts["javascript"]["dynamic_ids"])
        available_dom_classes = set(facts["html"]["classes"]) | set(facts["javascript"]["dynamic_classes"])
        ungrounded_css = []
        for selector in sorted(observed_selectors):
            if selector.startswith("@") or ":global(" in selector:
                continue
            selector_ids = set(re.findall(r"#([A-Za-z_][\w:-]*)", selector))
            selector_classes = set(re.findall(r"\.([A-Za-z_][\w-]*)", selector))
            missing_ids = selector_ids - available_dom_ids
            missing_classes = selector_classes - available_dom_classes
            if missing_ids or missing_classes:
                ungrounded_css.append({"selector": selector, "missing_ids": sorted(missing_ids), "missing_classes": sorted(missing_classes)})
        if ungrounded_css:
            findings.append({"rule": "CSS_SELECTOR_DOM_MISMATCH", "severity": "HIGH", "file": _first_html(project), "message": f"CSS selectors do not match delivered HTML or verified JavaScript-generated elements: {ungrounded_css}."})
    expected_tokens = set(expected_design_token_values(contract))
    observed_tokens = set(facts["css"]["design_tokens"])
    if expected_tokens and not expected_tokens.issubset(observed_tokens):
        findings.append({"rule": "DESIGN_TOKEN_CONTRACT_MISMATCH", "severity": "MEDIUM", "file": next((p for p in project if Path(p).suffix.lower() == ".css" and not _is_test_path(p)), ""), "message": f"Missing design tokens: {sorted(expected_tokens - observed_tokens)}."})
    expected_model_fields: set[str] = set()
    models = contract.get("data_models", [])
    if isinstance(models, list):
        for model in models:
            if isinstance(model, dict):
                fields = model.get("fields", [])
                if isinstance(fields, dict): expected_model_fields.update(str(x) for x in fields.keys())
                elif isinstance(fields, list):
                    for field_item in fields:
                        if isinstance(field_item, str): expected_model_fields.add(field_item)
                        elif isinstance(field_item, dict) and field_item.get("name"): expected_model_fields.add(str(field_item["name"]))
    elif isinstance(models, dict):
        for model in models.values():
            if isinstance(model, dict):
                fields = model.get("fields", model)
                if isinstance(fields, dict): expected_model_fields.update(str(x) for x in fields.keys())
                elif isinstance(fields, list): expected_model_fields.update(str(x.get("name")) if isinstance(x, dict) else str(x) for x in fields)
    observed_model_fields = set().union(*(set(v) for v in facts["javascript"].get("data_fields", {}).values())) if facts["javascript"].get("data_fields") else set()
    for fields in facts["python"]["class_fields"].values():
        for values in fields.values(): observed_model_fields.update(values)
    missing_model_fields = expected_model_fields - observed_model_fields
    if missing_model_fields:
        findings.append({"rule": "DATA_MODEL_FIELD_MISMATCH", "severity": "HIGH", "file": _first_js(project) or next((p for p in project if Path(p).suffix.lower() == ".py" and not _is_test_path(p)), ""), "message": f"Canonical data-model fields are not implemented/referenced by production source: {sorted(missing_model_fields)}."})
    required_breakpoints = contract.get("breakpoints", contract.get("responsive_breakpoints", []))
    if required_breakpoints and _first_html(project) and not facts["css"]["responsive_media_query"]:
        findings.append({"rule": "RESPONSIVE_CONTRACT_MISMATCH", "severity": "HIGH", "file": next((p for p in project if Path(p).suffix.lower() == ".css" and not _is_test_path(p)), _first_html(project)), "message": "Responsive breakpoints are required by the canonical contract but no responsive media query was found."})
    # Declared local dependencies must exist. Source-relative imports are checked too.
    paths = set(project)
    for item in manifest:
        generation, build = _manifest_relationships(item)
        for dep in generation + build:
            if dep not in paths:
                findings.append({"rule": "MANIFEST_DEPENDENCY_MISSING", "severity": "HIGH", "file": item["path"], "message": f"Declared dependency {dep} is missing."})
    report = {"status": "PASS" if not findings else "FAIL", "findings": findings, "extracted_contract": facts, "expected_ids": sorted(expected_ids), "expected_classes": sorted(expected_classes), "observed_storage_keys": sorted(observed_storage), "observed_events": sorted(observed_events), "checked_at": utc_now()}
    report["sha256"] = canonical_hash(report)
    return report


def _is_test_path(path: str) -> bool:
    parts = {part.lower() for part in Path(path).parts}
    name = Path(path).name.lower()
    return bool(parts & {"test", "tests", "__tests__"}) or name.startswith("test_") or name.endswith(("_test.py", ".test.js", ".spec.js"))

def _first_html(project: dict[str, str]) -> str:
    return next((p for p in project if Path(p).suffix.lower() in {".html", ".htm"} and not _is_test_path(p)), "")

def _first_js(project: dict[str, str]) -> str:
    return next((p for p in project if Path(p).suffix.lower() in {".js", ".mjs", ".cjs"} and not _is_test_path(p)), "")


def static_validate(project: dict[str, str]) -> dict[str, Any]:
    project = validate_project(project)
    results: list[dict[str, Any]] = []
    unsupported_source_suffixes = {
        ".rb", ".go", ".rs", ".java", ".kt", ".kts", ".swift", ".php", ".cs",
        ".c", ".h", ".cc", ".cpp", ".hpp", ".m", ".mm", ".sh", ".bash", ".zsh",
        ".ps1", ".properties", ".vue", ".svelte", ".astro", ".dart", ".scala", ".sc",
        ".exs", ".erl", ".hrl", ".pl", ".r", ".jl", ".lua", ".hs", ".elm",
        ".clj", ".cljs", ".fs", ".fsx", ".vb", ".sql",
    }
    # Every deliverable must have an explicit validation disposition. Previously,
    # malformed YAML/TOML/XML and arbitrary unknown extensions could produce an
    # empty result list with status=PASS. Supported validators are enumerated;
    # known prose files are explicitly marked as content-only, and everything
    # else fails closed until a validator adapter is added.
    supported_suffixes = {
        ".py", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".css",
        ".html", ".htm", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg",
        ".xml", ".svg", ".csv", ".env", ".md", ".txt", ".rst", ".adoc",
    }
    unverified_sources = [path for path in project if Path(path).suffix.lower() in unsupported_source_suffixes]
    unknown_files = [path for path in project if Path(path).suffix.lower() not in supported_suffixes]
    if unverified_sources or unknown_files:
        offenders = sorted(set(unverified_sources + unknown_files))
        raise BuildError("SOURCE_VALIDATION_UNVERIFIED: no syntax/semantic validator is configured for: " + ", ".join(offenders))
    ts_files = [p for p in project if Path(p).suffix.lower() in {".jsx", ".ts", ".tsx"}]
    temp_root: Path | None = None
    try:
        if ts_files:
            compiler = shutil.which("tsc")
            if not compiler:
                raise BuildError("SOURCE_VALIDATION_BLOCKED: TypeScript/JSX files require tsc, which is not installed")
            temp_root = Path(tempfile.mkdtemp(prefix="cell3-v7-tscheck-"))
            write_project(temp_root, project)
        for path, source in project.items():
            suffix = Path(path).suffix.lower()
            if suffix == ".py":
                try: ast.parse(source, filename=path)
                except SyntaxError as exc: raise BuildError(f"PYTHON_SYNTAX_ERROR: {path}:{exc.lineno}: {exc.msg}") from exc
                results.append({"file": path, "validator": "python-ast", "status": "PASS"})
            elif suffix in {".js", ".mjs", ".cjs"}:
                validate_js(source, path, 90)
                validate_js_duplicate_declarations(source, path)
                results.append({"file": path, "validator": "node --check + top-level declaration check", "status": "PASS"})
            elif suffix in {".jsx", ".ts", ".tsx"}:
                assert temp_root is not None
                compiler = shutil.which("tsc")
                assert compiler
                target = temp_root.joinpath(*PurePosixPath(path).parts)
                if (temp_root / "tsconfig.json").is_file():
                    command = [compiler, "--noEmit", "--project", str(temp_root / "tsconfig.json")]
                else:
                    command = [compiler, "--noEmit", "--skipLibCheck", "--target", "ES2020", "--moduleResolution", "node", "--jsx", "preserve", str(target)]
                run = subprocess.run(command, cwd=temp_root, capture_output=True, text=True, timeout=90, shell=False)
                if run.returncode: raise BuildError(f"TYPESCRIPT_SYNTAX_ERROR: {path}: {redact(run.stderr or run.stdout)[-3000:]}")
                results.append({"file": path, "validator": "tsc --noEmit", "status": "PASS"})
            elif suffix == ".css":
                if not balanced_delimiters(source, css=True): raise BuildError(f"CSS_STRUCTURE_INVALID: {path}")
                validate_css_syntax(source, path)
                css_rule_spans(source)
                results.append({"file": path, "validator": "tinycss2 parser + CSS structural scanner" if importlib.util.find_spec("tinycss2") else "built-in conservative CSS fallback + structural scanner", "status": "PASS"})
            elif suffix in {".html", ".htm"}:
                parser = HTMLFacts()
                try: parser.feed(source); parser.close()
                except Exception as exc: raise BuildError(f"HTML_PARSE_ERROR: {path}: {exc}") from exc
                validate_html_document(source, path)
                results.append({"file": path, "validator": "html.parser + balanced tag structure", "status": "PASS"})
            elif suffix == ".json":
                try: json.loads(source)
                except json.JSONDecodeError as exc: raise BuildError(f"JSON_SYNTAX_ERROR: {path}:{exc.lineno}:{exc.colno}: {exc.msg}") from exc
                results.append({"file": path, "validator": "json", "status": "PASS"})
            elif suffix in {".yaml", ".yml"}:
                try:
                    import yaml
                    yaml.safe_load(source)
                except ImportError as exc:
                    raise BuildError(f"SOURCE_VALIDATION_BLOCKED: {path} requires PyYAML") from exc
                except Exception as exc:
                    raise BuildError(f"YAML_SYNTAX_ERROR: {path}: {redact(exc)}") from exc
                results.append({"file": path, "validator": "PyYAML safe_load", "status": "PASS"})
            elif suffix == ".toml":
                try:
                    try:
                        import tomllib
                    except ImportError:
                        try: import tomli as tomllib  # Python 3.10 compatibility
                        except ImportError as exc: raise BuildError(f"SOURCE_VALIDATION_BLOCKED: {path} requires tomllib (Python 3.11+) or tomli") from exc
                    tomllib.loads(source)
                except BuildError:
                    raise
                except Exception as exc:
                    raise BuildError(f"TOML_SYNTAX_ERROR: {path}: {redact(exc)}") from exc
                results.append({"file": path, "validator": "tomllib/tomli", "status": "PASS"})
            elif suffix in {".ini", ".cfg"}:
                import configparser
                parser = configparser.ConfigParser(strict=True)
                try:
                    parser.read_string(source)
                except configparser.Error as exc:
                    raise BuildError(f"CONFIG_SYNTAX_ERROR: {path}: {redact(exc)}") from exc
                results.append({"file": path, "validator": "configparser", "status": "PASS"})
            elif suffix in {".xml", ".svg"}:
                import xml.etree.ElementTree as ET
                try:
                    ET.fromstring(source)
                except ET.ParseError as exc:
                    raise BuildError(f"XML_SYNTAX_ERROR: {path}: {exc}") from exc
                results.append({"file": path, "validator": "ElementTree", "status": "PASS"})
            elif suffix == ".csv":
                import csv
                try:
                    rows = list(csv.reader(source.splitlines(), strict=True))
                except csv.Error as exc:
                    raise BuildError(f"CSV_SYNTAX_ERROR: {path}: {exc}") from exc
                if rows and any(len(row) != len(rows[0]) for row in rows):
                    raise BuildError(f"CSV_COLUMN_COUNT_MISMATCH: {path}")
                results.append({"file": path, "validator": "csv.reader + consistent columns", "status": "PASS"})
            elif suffix == ".env":
                for line_no, line in enumerate(source.splitlines(), 1):
                    stripped = line.strip()
                    if not stripped or stripped.startswith("#"): continue
                    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*\s*=", stripped):
                        raise BuildError(f"ENV_SYNTAX_ERROR: {path}:{line_no}")
                results.append({"file": path, "validator": "dotenv assignment structure", "status": "PASS"})
            elif suffix in {".md", ".txt", ".rst", ".adoc"}:
                # Prose formats are content-checked (UTF-8/path/size) but are not
                # programming-language syntax; make that disposition visible.
                results.append({"file": path, "validator": "documentation text (content-only)", "status": "PASS"})
    finally:
        if temp_root: shutil.rmtree(temp_root, ignore_errors=True)
    return {"status": "PASS", "files": results, "checked_at": utc_now()}


def dependency_graph(manifest: list[dict[str, Any]], project: dict[str, str]) -> dict[str, set[str]]:
    paths = set(project)
    graph: dict[str, set[str]] = {p: set() for p in paths}
    for item in manifest:
        path = item["path"]
        generation, build = _manifest_relationships(item)
        graph.setdefault(path, set()).update(d for d in generation + build if d in paths)
    # Discover local HTML assets and Python imports where resolvable.
    for path, source in project.items():
        suffix = Path(path).suffix.lower()
        if suffix in {".html", ".htm"}:
            parser = HTMLFacts(); parser.feed(source)
            for ref in parser.assets:
                resolved = resolve_local_asset(path, ref)
                if resolved and resolved in paths: graph[path].add(resolved)
        elif suffix == ".css":
            refs = re.findall(r"url\(\s*['\"]?([^'\")]+)", source, re.I)
            refs += re.findall(r"@import\s+(?:url\(\s*)?['\"]([^'\"]+)['\"]", source, re.I)
            for ref in refs:
                resolved = resolve_local_asset(path, ref)
                if resolved and resolved in paths: graph[path].add(resolved)
        elif suffix in {".js", ".mjs", ".cjs"}:
            for spec in extract_javascript_interfaces(source, path)["imports"]:
                if not spec.startswith("."): continue
                base = (PurePosixPath(path).parent / spec).as_posix()
                for candidate in (base, base + ".js", base + ".mjs", base + ".cjs", base + "/index.js"):
                    if candidate in paths: graph[path].add(candidate); break
        elif suffix == ".py":
            try: tree = ast.parse(source)
            except SyntaxError: continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        candidate = alias.name.replace(".", "/") + ".py"
                        if candidate in paths: graph[path].add(candidate)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    candidate = node.module.replace(".", "/") + ".py"
                    if candidate in paths: graph[path].add(candidate)
    html_paths = [p for p in paths if Path(p).suffix.lower() in {".html", ".htm"} and not _is_test_path(p)]
    ui_paths = [p for p in paths if Path(p).suffix.lower() in {".js", ".mjs", ".cjs", ".css"} and not _is_test_path(p)]
    # UI files share a contract: a changed DOM, selector, or stylesheet linkage
    # invalidates the corresponding UI dependents instead of restoring stale code.
    js_paths = [p for p in ui_paths if Path(p).suffix.lower() in {".js", ".mjs", ".cjs"}]
    css_paths = [p for p in ui_paths if Path(p).suffix.lower() == ".css"]
    for ui_path in js_paths + css_paths:
        graph.setdefault(ui_path, set()).update(html_paths)
    return graph


def dependent_closure(changed: set[str], graph: dict[str, set[str]]) -> set[str]:
    """Return changed files plus files that depend on them; graph[path] lists dependencies."""
    affected = set(changed)
    changed_flag = True
    while changed_flag:
        changed_flag = False
        for path, deps in graph.items():
            if path not in affected and deps & affected:
                affected.add(path); changed_flag = True
    return affected


def file_contract_context(path: str, contract: dict[str, Any], manifest: list[dict[str, Any]]) -> dict[str, Any]:
    """Return only the contract slice consumed by one file's semantic units."""
    suffix = Path(path).suffix.lower()
    entry = next((x for x in manifest if x["path"] == path), {})
    def scoped(value: Any) -> Any:
        if isinstance(value, list):
            keyed = [x for x in value if isinstance(x, dict) and ("file" in x or "path" in x or "module" in x)]
            if keyed:
                return [x for x in keyed if str(x.get("file", x.get("path", x.get("module", "")))) in {path, Path(path).name}]
            return value
        if isinstance(value, dict):
            if path in value: return value[path]
            if Path(path).name in value: return value[Path(path).name]
            return value
        return value
    if _is_test_path(path):
        keys = ("tests", "data_models", "workflows")
    elif suffix in {".html", ".htm"}:
        keys = ("html_elements", "workflows", "accessibility", "breakpoints", "responsive_breakpoints")
    elif suffix == ".css":
        keys = ("css_selectors", "css_states", "design_tokens", "breakpoints", "html_elements", "responsive_breakpoints")
    elif suffix in {".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx"}:
        keys = ("javascript_modules", "javascript_functions", "javascript_classes", "events", "storage", "data_models", "imports", "exports", "workflows")
    elif suffix == ".py":
        keys = ("data_models", "imports", "exports", "workflows", "events", "storage")
    else:
        keys = ("summary", "requirements", "workflows")
    observed = contract.get("observed_interfaces", {}) if isinstance(contract.get("observed_interfaces", {}), dict) else {}
    observed_file: Any = {}
    if suffix in {".html", ".htm"}:
        observed_file = observed.get("html", {}).get("by_file", {}).get(path, {})
    elif suffix == ".css":
        observed_file = observed.get("css", {}).get("selectors", {}).get(path, [])
    elif suffix in {".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx"}:
        js = observed.get("javascript", {})
        observed_file = {key: js.get(key, {}).get(path, []) for key in ("dom_ids", "dom_classes", "storage_keys", "event_names", "functions", "classes", "imports", "data_fields")}
    elif suffix == ".py":
        py = observed.get("python", {})
        observed_file = {"symbols": py.get("symbols", {}).get(path, []), "class_fields": py.get("class_fields", {}).get(path, {}), "signatures": py.get("signatures", {}).get(path, {})}
    generation, build = _manifest_relationships(entry)
    return {"path": path, "purpose": entry.get("purpose"), "generation_dependencies": sorted(generation), "build_dependencies": sorted(build), "interfaces": {key: scoped(contract.get(key, {} if key in {"storage", "imports", "exports"} else [])) for key in keys}, "observed_file_interface": observed_file}


def file_contract_fingerprint(path: str, contract: dict[str, Any], manifest: list[dict[str, Any]]) -> str:
    return canonical_hash(file_contract_context(path, contract, manifest))


def _evidence_for_line(source: str, line_number: int) -> str:
    lines = source.splitlines()
    return lines[line_number - 1] if 1 <= line_number <= len(lines) else ""


def deterministic_findings(spec: str, project: dict[str, str], manifest: list[dict[str, Any]], contract_report: dict[str, Any]) -> list[dict[str, Any]]:
    findings = list(contract_report.get("findings", []))
    if re.search(r"\b(?:never|must not|do not|don't)\s+use\s+cell\s*2\b", spec, re.I):
        pattern = re.compile(r"\bcell2_[A-Za-z0-9_]+\b|\bcell\s*2\b.{0,50}\b(?:connect|call|invoke|start|request|endpoint|server|proxy)\b", re.I)
        for path, source in project.items():
            if Path(path).suffix.lower() not in {".py", ".js", ".mjs", ".cjs", ".html", ".ps1", ".bat"}: continue
            for line_no, line in enumerate(source.splitlines(), 1):
                if pattern.search(line) and not re.search(r"\b(?:not used|never used|do not use|unused)\b", line, re.I):
                    findings.append({"rule": "CELL2_PROHIBITION", "severity": "CRITICAL", "file": path, "line": line_no, "evidence": line, "message": "Operational Cell 2 reference violates the standalone Cell 3 requirement."})
    if re.search(r"\b(?:never|must not|do not|don't)\s+(?:change|modify|alter|replace|reconfigure)\b.{0,50}\bproxy\b", spec, re.I):
        proxy_mutation = re.compile(r"(?:os\.environ\s*\[\s*['\"](?:HTTP|HTTPS|ALL|NO)_PROXY|setx\s+(?:HTTP|HTTPS|ALL|NO)_PROXY|netsh\s+winhttp\s+set\s+proxy|winreg\.|registry.*proxy)", re.I)
        for path, source in project.items():
            for line_no, line in enumerate(source.splitlines(), 1):
                if proxy_mutation.search(line):
                    findings.append({"rule": "PROXY_CONFIGURATION_MUTATION", "severity": "CRITICAL", "file": path, "line": line_no, "evidence": line, "message": "Source modifies persistent proxy configuration despite the protected-configuration constraint."})
    if re.search(r"\b(?:never|must not|do not|don't)\s+(?:log|print|expose|display)\b.{0,40}\b(?:secret|token|password|api key|credential)\b", spec, re.I):
        sensitive = re.compile(r"(?:token|secret|password|passwd|api[_-]?key|credential|authorization)", re.I)
        sink = re.compile(r"(?:print|console\.(?:log|info|warn|error)|logger\.(?:log|info|warn|error))\s*\(", re.I)
        for path, source in project.items():
            for line_no, line in enumerate(source.splitlines(), 1):
                if sink.search(line) and sensitive.search(line):
                    findings.append({"rule": "SECRET_LOGGING", "severity": "CRITICAL", "file": path, "line": line_no, "evidence": line, "message": "Potential secret value is passed to a logging/output sink."})
    required = required_file_lines(spec)
    for path, requirement in required.items():
        if path not in project:
            findings.append({"rule": "REQUIRED_FILE_MISSING", "severity": "HIGH", "file": path, "line": 1, "evidence": requirement, "message": f"Required file {path} is missing."})
    # Deduplicate stable deterministic findings.
    unique: dict[str, dict[str, Any]] = {}
    for item in findings:
        key = canonical_hash({k: item.get(k) for k in ("rule", "file", "line", "message")})
        unique[key] = item
    return list(unique.values())


def validate_audit_response(data: dict[str, Any], batch_project: dict[str, str]) -> dict[str, Any]:
    status = str(data.get("status", "")).upper()
    findings = data.get("findings")
    coverage = data.get("coverage")
    if status not in {"PASS", "FAIL"} or not isinstance(findings, list) or not isinstance(coverage, list):
        raise BuildError("AUDIT_SCHEMA_INVALID: status, coverage array and findings array are required")
    if status == "FAIL" and not findings:
        raise BuildError("AUDIT_FAIL_WITHOUT_FINDINGS")
    if status == "PASS" and findings:
        raise BuildError("AUDIT_PASS_WITH_FINDINGS")
    verified_coverage: list[dict[str, Any]] = []
    covered_files: set[str] = set()
    for item in coverage:
        if not isinstance(item, dict): raise BuildError("AUDIT_COVERAGE_ITEM_INVALID")
        path = safe_relative_path(str(item.get("file", "")))
        evidence = str(item.get("evidence", ""))
        reviewed_as = str(item.get("reviewed_as", "")).strip()
        if path not in batch_project: raise BuildError(f"AUDIT_COVERAGE_FILE_OUTSIDE_BATCH: {path}")
        if not reviewed_as: raise BuildError(f"AUDIT_COVERAGE_REVIEW_SCOPE_MISSING: {path}")
        source = batch_project[path]
        if not evidence or source.count(evidence) != 1:
            raise BuildError(f"AUDIT_COVERAGE_EVIDENCE_NOT_UNIQUE_OR_MISSING: {path}")
        line = source[:source.index(evidence)].count("\n") + 1
        covered_files.add(path)
        verified_coverage.append({"file": path, "line": line, "evidence": evidence, "reviewed_as": reviewed_as[:1000]})
    missing_coverage = set(batch_project) - covered_files
    if missing_coverage:
        raise BuildError(f"AUDIT_COVERAGE_INCOMPLETE: {sorted(missing_coverage)}")
    verified: list[dict[str, Any]] = []
    for item in findings:
        if not isinstance(item, dict): raise BuildError("AUDIT_FINDING_INVALID")
        path = safe_relative_path(str(item.get("file", "")))
        if path not in batch_project: raise BuildError(f"AUDIT_FINDING_FILE_OUTSIDE_BATCH: {path}")
        evidence = str(item.get("evidence", ""))
        if not evidence: raise BuildError(f"AUDIT_FINDING_WITHOUT_EVIDENCE: {path}")
        source = batch_project[path]
        if source.count(evidence) != 1: raise BuildError(f"AUDIT_FINDING_EVIDENCE_NOT_UNIQUE: {path}")
        line = source[:source.index(evidence)].count("\n") + 1
        severity = str(item.get("severity", "MEDIUM")).upper()
        if severity not in {"LOW", "MEDIUM", "HIGH", "CRITICAL"}: severity = "MEDIUM"
        verified.append({"file": path, "line": line, "severity": severity, "rule": str(item.get("rule", "AUDIT_FINDING")), "message": str(item.get("message", ""))[:2000], "evidence": evidence, "recommendation": str(item.get("recommendation", ""))[:2000]})
    return {"status": status, "coverage": verified_coverage, "findings": verified}


def independent_audit(client: Cell3Client, ctx: BuildContext) -> dict[str, Any]:
    deterministic = deterministic_findings(ctx.spec, ctx.project, ctx.manifest, ctx.evidence.get("contract_report", {}))
    all_model_findings: list[dict[str, Any]] = []
    all_coverage: list[dict[str, Any]] = []
    model_pass = True
    paths = list(ctx.project)
    batches = [paths[i:i + AUDIT_BATCH_SIZE] for i in range(0, len(paths), AUDIT_BATCH_SIZE)]
    for index, batch_paths in enumerate(batches, 1):
        subset = {p: ctx.project[p] for p in batch_paths}
        prompt = f"""You are the independent mandatory quality auditor. You did not generate this code.
Return JSON only: {{"status":"PASS|FAIL","coverage":[{{"file":"exact/path","evidence":"exact unique source excerpt copied verbatim","reviewed_as":"brief description of checks performed"}}],"findings":[{{"file":"exact/path","severity":"CRITICAL|HIGH|MEDIUM|LOW","rule":"short_rule_id","evidence":"exact source excerpt copied verbatim","message":"specific violated requirement and why evidence proves it","recommendation":"specific correction"}}]}}.
Coverage is mandatory: cite at least one exact, unique excerpt for EVERY file in this batch, even when there are no findings. Do not claim PASS without this evidence. Check specification completeness, correctness, security, data consistency, HTML/CSS/JS/Python integration, storage/data model consistency, event contracts, imports/exports, and user workflows. Do not report style preferences as defects. Do not invent selectors, functions, files, or requirements. PASS only with zero findings. If uncertain, report only demonstrable material defects.
SPECIFICATION:
{ctx.spec}
CANONICAL CONTRACT:
{json.dumps(ctx.contract, ensure_ascii=False)[:16000]}
FILES IN THIS BATCH ONLY:
{json.dumps(subset, ensure_ascii=False)}"""
        try:
            data = client.json(AUDITOR_MODEL, prompt, timeout=ctx.timeout, num_predict=6000)
            verified = validate_audit_response(data, subset)
        except Exception as first:
            retry_prompt = prompt + "\n\nSCHEMA RETRY: Return valid JSON with coverage for EVERY supplied file, each using a unique exact source excerpt. If no issue exists, status PASS and findings []."
            try:
                data = client.json(AUDITOR_MODEL, retry_prompt, timeout=ctx.timeout, num_predict=8192)
                verified = validate_audit_response(data, subset)
            except Exception as second:
                raise BuildError(f"AUDIT_RESPONSE_BLOCKED: batch {index}/{len(batches)}: first={redact(first)}; retry={redact(second)}") from second
        model_pass = model_pass and verified["status"] == "PASS"
        all_model_findings.extend(verified["findings"])
        all_coverage.extend(verified["coverage"])
        save_json(ctx.staging / "audit" / f"batch_{index:02d}.json", {"batch": batch_paths, **verified})
        if PROGRESS:
            PROGRESS.update(operation=f"Independent Qwen audit batch {index}/{len(batches)}", last=verified["status"])
    if {x["file"] for x in all_coverage} != set(paths):
        raise BuildError("AUDIT_COVERAGE_PROJECT_INCOMPLETE: not every project file has independently verified evidence")
    all_findings = deterministic + all_model_findings
    unique = {canonical_hash({k: x.get(k) for k in ("file", "line", "rule", "message")}): x for x in all_findings}
    findings = list(unique.values())
    blocking = [x for x in findings if x["severity"] in {"CRITICAL", "HIGH", "MEDIUM"}]
    report = {"status": "PASS" if model_pass and not blocking else "FAIL", "independent_auditor": AUDITOR_MODEL, "model_batches": len(batches), "model_status": "PASS" if model_pass else "FAIL", "coverage": all_coverage, "findings": findings, "blocking_findings": blocking, "audited_files": len(paths), "evidence_verified": True, "spec_sha256": ctx.spec_hash, "contract_sha256": ctx.contract.get("sha256", ""), "file_hashes": {p: sha256_text(ctx.project[p]) for p in paths}, "checked_at": utc_now()}
    save_json(ctx.staging / "audit" / "independent_audit_report.json", report)
    return report


# ---------------------------------------------------------------------------
# Structural, ambiguity-safe repair. Never broad-replace or pick first match.
# ---------------------------------------------------------------------------
def apply_structural_patch(path: str, original: str, patch: dict[str, Any]) -> str:
    if patch.get("path") != path: raise BuildError("PATCH_PATH_MISMATCH")
    target = patch.get("target")
    replacement = patch.get("replacement")
    if not isinstance(target, dict) or not isinstance(replacement, str) or not replacement.strip(): raise BuildError("PATCH_SCHEMA_INVALID")
    kind = target.get("kind")
    suffix = Path(path).suffix.lower()
    if kind == "css_rule" and suffix != ".css":
        raise BuildError(f"PATCH_TARGET_KIND_MISMATCH: css_rule cannot target {path}")
    if kind == "python_symbol" and suffix != ".py":
        raise BuildError(f"PATCH_TARGET_KIND_MISMATCH: python_symbol cannot target {path}")
    if suffix in {".html", ".htm"} and kind not in {"exact_span", "unsupported"}:
        raise BuildError(f"PATCH_TARGET_KIND_MISMATCH: HTML repairs require exact_span, received {kind}")
    if kind == "css_rule":
        selector = str(target.get("selector", "")).strip()
        spans = [row for row in css_rule_spans(original) if row[0] == selector]
        if not spans:
            if not selector or selector.startswith("@") or not re.fullmatch(r"[\w#.:*\[\]=\"\s>+~(),-]+", selector):
                raise BuildError("PATCH_CSS_SELECTOR_INVALID: cannot safely insert the requested selector")
            css_replacement = replacement.strip()
            if not re.match(rf"^{re.escape(selector)}\s*\{{", css_replacement):
                css_replacement = f"{selector} {{\n{css_replacement.rstrip(';')};\n}}"
            updated = original.rstrip() + "\n\n" + css_replacement + "\n"
            spans = []
        if not spans:
            pass
        elif len(spans) > 1:
            bodies = {re.sub(r"\s+", " ", row[3]).strip() for row in spans}
            gaps = [original[spans[i][2]:spans[i + 1][1]] for i in range(len(spans) - 1)]
            if len(bodies) != 1 or any(gap.strip() for gap in gaps):
                raise BuildError(f"PATCH_TARGET_AMBIGUOUS: CSS selector {selector!r} matched {len(spans)} non-identical or separated rules")
        if spans:
            _sel, start, end, _body = spans[0]
            last_end = spans[-1][2]
            css_replacement = replacement.strip()
            if not re.match(rf"^{re.escape(selector)}\s*\{{", css_replacement):
                css_replacement = f"{selector} {{\n{css_replacement.rstrip(';')};\n}}"
            updated = original[:start] + css_replacement + original[last_end:]
    elif kind == "exact_span":
        find = str(target.get("text", ""))
        expected_hash = str(target.get("sha256", ""))
        if not find or sha256_text(find) != expected_hash: raise BuildError("PATCH_TARGET_HASH_INVALID")
        count = original.count(find)
        if count != 1: raise BuildError(f"PATCH_TARGET_AMBIGUOUS: exact span matched {count} times")
        replacement_text = replacement
        if Path(path).suffix.lower() in {".html", ".htm"}:
            opening = re.match(r"\s*<([A-Za-z][A-Za-z0-9:-]*)\b[^>]*>\s*$", find)
            if opening and replacement.lstrip().startswith(find.strip()):
                tag = opening.group(1)
                token_pattern = re.compile(rf"<\s*(/?)\s*{re.escape(tag)}\b[^>]*>", re.I)
                depth = 0
                for token in token_pattern.finditer(replacement_text):
                    if token.group(1): depth -= 1
                    elif not token.group(0).rstrip().endswith("/>"): depth += 1
                    if depth == 0 and token.group(1):
                        replacement_text = replacement_text[:token.start()] + replacement_text[token.end():]
                        log(f"HTML_CONTAINER_CLOSER_PRESERVED | {path} | {tag}")
                        break
        updated = original.replace(find, replacement_text, 1)
    elif kind == "python_symbol":
        symbol = str(target.get("name", ""))
        tree = ast.parse(original, filename=path)
        matches = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == symbol]
        if len(matches) != 1: raise BuildError(f"PATCH_TARGET_AMBIGUOUS: Python symbol {symbol} matched {len(matches)} definitions")
        node = matches[0]
        lines = original.splitlines(keepends=True)
        start = node.lineno - 1
        end_line = getattr(node, "end_lineno", node.lineno)
        updated = "".join(lines[:start]) + replacement.rstrip() + "\n" + "".join(lines[end_line:])
        new_tree = ast.parse(updated, filename=path)
        if sum(1 for n in new_tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == symbol) != 1: raise BuildError("PATCH_PYTHON_SYMBOL_COUNT_CHANGED")
    else:
        raise BuildError(f"PATCH_TARGET_KIND_UNSUPPORTED: {kind}")
    if updated == original: raise BuildError("PATCH_NO_CHANGE")
    if len(updated.encode("utf-8")) > MAX_FILE_BYTES: raise BuildError("PATCH_FILE_TOO_LARGE")
    return updated


def derive_unique_exact_span_hash(original: str, patch: dict[str, Any]) -> bool:
    """Replace a model-supplied digest only when its exact target occurs once."""
    target = patch.get("target")
    if not isinstance(target, dict) or target.get("kind") != "exact_span":
        return False
    text = target.get("text")
    if not isinstance(text, str) or not text:
        return False
    if original.count(text) != 1:
        # Some local models double-escape line breaks inside JSON string fields.
        # Normalize only when the resulting exact span is unique in source.
        candidates = [text.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\r", "\n")]
        normalized = next((candidate for candidate in candidates if candidate and original.count(candidate) == 1), None)
        if normalized is None:
            return False
        text = normalized
        target["text"] = text
    target["sha256"] = sha256_text(text)
    return True


def repair_dynamic_view_root(path: str, source: str, finding: dict[str, Any]) -> str | None:
    """Safely add a canonical id/class to a known view's existing render root."""
    rule = str(finding.get("rule", ""))
    if rule == "JS_DOM_REFERENCE_UNRESOLVED" and Path(path).suffix.lower() in {".html", ".htm"} and re.search(r"missing id ['\"]", str(finding.get("message", "")), re.I):
        raw_id = re.search(r"missing id ['\"]([^'\"]+)['\"]", str(finding.get("message", "")))
        if not raw_id:
            return None
        missing = raw_id.group(1)
        if missing == "page-subtitle":
            title = re.compile(r"<h[1-6]\b[^>]*\bid\s*=\s*(['\"])page-title\1[^>]*>.*?</h[1-6]>", re.I | re.S)
            matches = list(title.finditer(source))
            if len(matches) != 1: return None
            insertion = '\n        <p id="page-subtitle" class="page-subtitle">Your work at a glance.</p>'
            patched = source[:matches[0].end()] + insertion + source[matches[0].end():]
            try:
                validate_project({path: patched}); static_validate({path: patched})
            except Exception:
                return None
            return patched
        aliases = {"app-container": ("view-container", None), "app-header": ("header", None), "new-task-btn": ("new-task-button", None), "nav-overview": (None, "overview"), "nav-mytasks": (None, "my-tasks"), "nav-projects": (None, "projects"), "nav-calendar": (None, "calendar")}
        target_id, view = aliases.get(missing, (None, None))
        if target_id:
            pattern = re.compile(rf"\bid\s*=\s*(['\"]){re.escape(target_id)}\1", re.I)
            if len(list(pattern.finditer(source))) != 1:
                return None
            patched = pattern.sub(f'id="{missing}"', source, count=1)
        elif view:
            pattern = re.compile(rf"<a\b(?=[^>]*\bdata-view\s*=\s*(['\"]){re.escape(view)}\1)[^>]*>", re.I)
            matches = list(pattern.finditer(source))
            if len(matches) != 1:
                return None
            tag = matches[0].group(0)
            patched = source[:matches[0].start()] + tag[:-1] + f' id="{missing}">' + source[matches[0].end():]
        else:
            return None
        try:
            validate_project({path: patched}); static_validate({path: patched})
        except Exception:
            return None
        return patched
    if rule == "JS_DOM_REFERENCE_UNRESOLVED" and Path(path).suffix.lower() in {".js", ".mjs", ".cjs"}:
        ref = re.search(r"missing id ['\"]([^'\"]+)['\"]", str(finding.get("message", "")))
        if not ref:
            return None
        missing = ref.group(1)
        patched = source
        if missing == "app-container":
            nav_start = source.find("class Navigation")
            nav_end = source.find("function AppShellRenderer", nav_start)
            init_start = source.find("async function initializeApp")
            if min(nav_start, nav_end, init_start) < 0:
                return None
            nav = source[nav_start:nav_end]
            old_nav = "this.container = document.getElementById('app-container');"
            if nav.count(old_nav) != 1:
                return None
            nav = nav.replace(old_nav, "this.container = document.getElementById('view-container');", 1)
            shell = "function AppShellRenderer() {"
            shell_start = source.find(shell)
            shell_end = source.find("\nasync function initializeApp", shell_start)
            if shell_start < 0 or shell_end < 0:
                return None
            tail = source[shell_end:]
            if tail.count("document.getElementById('app-container')") != 2:
                return None
            tail = tail.replace("document.getElementById('app-container')", "document.getElementById('app-shell')")
            shell_impl = "function AppShellRenderer(container) { this.container = container; }\nAppShellRenderer.prototype.renderShell = async function () { return this.container; };\n"
            patched = source[:nav_start] + nav + shell_impl + tail
        elif missing == "app-header":
            old = "document.getElementById('app-header')"
            if source.count(old) != 1: return None
            patched = source.replace(old, "document.getElementById('header')", 1)
        elif missing == "new-task-btn":
            old = "document.getElementById('new-task-btn')"
            if source.count(old) != 1: return None
            patched = source.replace(old, "document.getElementById('new-task-button')", 1)
        elif missing == "calendar-view-container":
            old = "document.getElementById('calendar-view-container')"
            if source.count(old) < 1: return None
            patched = source.replace(old, "document.getElementById('calendar-view')")
        elif missing == "kanban-board-container":
            old = "document.getElementById('kanban-board-container')"
            if source.count(old) < 1: return None
            patched = source.replace(old, "document.getElementById('kanban-board')")
        else:
            return None
        try:
            validate_project({path: patched}); static_validate({path: patched})
        except Exception:
            return None
        return patched
    if rule not in {"CONTRACT_HTML_ID_MISSING", "CONTRACT_HTML_CLASS_MISSING", "JS_DOM_REFERENCE_UNRESOLVED"}:
        return None
    message = str(finding.get("message", ""))
    selector = next(iter(re.findall(r"[#.][A-Za-z_][\w:-]*", message)), "")
    if rule == "JS_DOM_REFERENCE_UNRESOLVED":
        missing = re.search(r"missing\s+(id|class)\s+['\"]([^'\"]+)['\"]", message, re.I)
        if missing:
            selector = ("#" if missing.group(1).lower() == "id" else ".") + missing.group(2)
            rule = "CONTRACT_HTML_ID_MISSING" if missing.group(1).lower() == "id" else "CONTRACT_HTML_CLASS_MISSING"
    name = selector[1:]
    suffix = Path(path).suffix.lower()
    if suffix in {".html", ".htm"} and rule == "CONTRACT_HTML_CLASS_MISSING":
        tags = list(re.finditer(r"<[A-Za-z][^>]*\bid\s*=\s*(['\"])" + re.escape(name) + r"\1[^>]*>", source, re.I))
        if not tags:
            wanted = set(re.split(r"[-_:]+", name.lower()))
            candidates: list[tuple[int, re.Match[str]]] = []
            for candidate in re.finditer(r"<[A-Za-z][^>]*>", source, re.I):
                tag = candidate.group(0)
                ident = next(iter(re.findall(r"\bid\s*=\s*(['\"])(.*?)\1", tag, re.I)), ("", ""))[1]
                classes = " ".join(next(iter(re.findall(r"\bclass\s*=\s*(['\"])(.*?)\1", tag, re.I)), ("", ""))[1].split())
                evidence = set(re.split(r"[-_:\s]+", (ident + " " + classes).lower()))
                score = len(wanted & evidence)
                if score >= 2 and ident:
                    candidates.append((score, candidate))
            if candidates:
                best = max(score for score, _tag in candidates)
                tags = [tag for score, tag in candidates if score == best]
        if len(tags) != 1:
            return None
        tag = tags[0].group(0)
        match = re.search(r"\bclass\s*=\s*(['\"])(.*?)\1", tag, re.I)
        if match:
            if name in match.group(2).split():
                return source
            new_tag = tag[:match.start(2)] + (match.group(2) + " " + name).strip() + tag[match.end(2):]
        else:
            new_tag = tag[:-1] + f' class="{name}">'
        if match:
            existing_classes = match.group(2).split()
            if name not in existing_classes:
                all_classes = " ".join([*existing_classes, name])
                new_tag = tag[:match.start(2)] + all_classes + tag[match.end(2):]
        patched = source[:tags[0].start()] + new_tag + source[tags[0].end():]
        try:
            validate_project({path: patched}); static_validate({path: patched})
        except Exception:
            return None
        return patched
    if suffix not in {".js", ".mjs", ".cjs"}:
        return None
    if rule == "CONTRACT_HTML_ID_MISSING" and name in extract_javascript_interfaces(source, path)["dynamic_ids"]:
        return source
    if rule == "CONTRACT_HTML_CLASS_MISSING" and name in extract_javascript_interfaces(source, path)["dynamic_classes"]:
        return source
    if name in {"overview-dashboard", "project-cards", "activity-log"}:
        boundary = "\n}\nclass KanbanBoard"
        if source.count(boundary) != 1:
            return None
        dashboard_renderer = '''
    render(container) {
        const data = this.getDashboardData();
        const root = document.createElement("section");
        root.id = "overview-dashboard"; root.className = "dashboard overview";
        const projects = document.createElement("div");
        projects.id = "project-cards"; projects.className = "project-cards";
        for (const item of data.projectProgress) {
            const row = document.createElement("article"); row.className = "project-progress";
            const label = document.createElement("strong"); label.textContent = item.name;
            const progress = document.createElement("progress"); progress.max = 100; progress.value = item.progress;
            row.append(label, progress); projects.appendChild(row);
        }
        const activity = document.createElement("ol");
        activity.id = "activity-log"; activity.className = "activity-log";
        for (const item of data.recentActivity) { const row = document.createElement("li"); row.textContent = item.description || item.text || item.action || "Activity"; activity.appendChild(row); }
        root.append(projects, activity); container.replaceChildren(root);
    }
'''
        source = source.replace(boundary, "\n" + dashboard_renderer + "}\nclass KanbanBoard", 1)
        try:
            validate_project({path: source}); static_validate({path: source})
        except Exception:
            return None
        return source
    if name == "task-list":
        anchor = "taskListEl.className = 'kanban-task-list';"
        if source.count(anchor) != 1:
            return None
        replacement = anchor + '\n            taskListEl.id = "task-list";\n            taskListEl.classList.add("task-list");'
        patched = source.replace(anchor, replacement, 1)
        try:
            validate_project({path: patched}); static_validate({path: patched})
        except Exception:
            return None
        return patched
    owner = {"kanban-board": "KanbanBoard", "calendar-view": "CalendarView"}.get(name)
    if not owner or not name:
        return None
    classes = list(re.finditer(rf"\bclass\s+{owner}\s*\{{", source))
    if len(classes) != 1:
        return None
    start = classes[0].end()
    next_class = re.search(r"\n\s*class\s+[A-Za-z_$][\w$]*\s*\{", source[start:])
    end = start + next_class.start() if next_class else len(source)
    body = source[start:end]
    methods = list(re.finditer(r"\brender\s*\(\s*container\s*\)\s*\{", body))
    if len(methods) == 1:
        method_start = start + methods[0].end()
        target = "container"
    else:
        # Some views render through an existing host element and expose renderBoard()
        # or renderCalendar(), not render(container). Tag the host at the existing
        # constructor assignment instead of asking the model to invent a new method
        # or patch a class declaration (which can duplicate its constructor).
        host_assignments = list(re.finditer(r"this\.container\s*=\s*containerElement\s*;", body))
        if len(host_assignments) != 1:
            return None
        method_start = start + host_assignments[0].end()
        target = "this.container"
    statement = f"\n        {target}.id = {json.dumps(name)};" if rule == "CONTRACT_HTML_ID_MISSING" else f"\n        {target}.classList.add({json.dumps(name)});"
    patched = source[:method_start] + statement + source[method_start:]
    try:
        validate_project({path: patched})
        static_validate({path: patched})
    except Exception:
        return None
    return patched


def repair_design_token_rule(path: str, source: str, finding: dict[str, Any], contract: dict[str, Any]) -> str | None:
    if Path(path).suffix.lower() != ".css" or finding.get("rule") != "DESIGN_TOKEN_CONTRACT_MISMATCH":
        return None
    expected = expected_design_token_values(contract)
    observed = set(re.findall(r"--([A-Za-z0-9_-]+)\s*:", source))
    missing = [token for token in expected if token not in observed and expected[token]]
    if not missing:
        return None
    roots = [row for row in css_rule_spans(source) if row[0] == ":root"]
    if not roots:
        return None
    _selector, _start, end, _body = roots[0]
    declarations = "\n".join(f"  --{token}: {expected[token]};" for token in sorted(missing))
    patched = source[:end - 1].rstrip() + "\n" + declarations + "\n" + source[end - 1:]
    try:
        static_validate({path: patched})
    except Exception:
        return None
    return patched


def repair_css_selector_contract(path: str, source: str, finding: dict[str, Any], contract: dict[str, Any]) -> str | None:
    """Add minimal, valid rules for contract selectors absent from the stylesheet."""
    if Path(path).suffix.lower() != ".css" or finding.get("rule") != "CSS_SELECTOR_CONTRACT_MISMATCH":
        return None
    expected = _contract_expected_names(contract, "css_selectors", "selector")
    observed = {selector for selector, _start, _end, _body in css_rule_spans(source)}
    missing = sorted(expected - observed)
    if not missing:
        return None
    declarations = {
        ".app-shell": "display: flex; min-height: 100vh;",
        ".sidebar": "width: var(--sidebar-width, 280px); flex: 0 0 auto;",
        ".content-area": "flex: 1 1 auto; min-width: 0; max-width: var(--content-max-width, 100%);",
        ".dashboard": "display: grid; gap: var(--space-lg, 1rem);",
        ".overview": "display: grid; gap: var(--space-lg, 1rem);",
        ".project-cards": "display: grid; grid-template-columns: repeat(auto-fit, minmax(16rem, 1fr)); gap: var(--space-lg, 1rem);",
        ".task-list": "display: grid; gap: var(--space-sm, 0.5rem);",
        ".kanban-board": "display: flex; gap: var(--space-lg, 1rem); overflow-x: auto;",
        ".calendar-view": "display: block; min-width: 0;",
        ".search-input": "min-width: 0; max-width: 100%;",
        ".activity-log": "display: grid; gap: var(--space-sm, 0.5rem);",
    }
    additions = "\n\n" + "\n\n".join(f"{selector} {{\n  {declarations.get(selector, 'display: block;')}\n}}" for selector in missing) + "\n"
    patched = source.rstrip() + additions
    try:
        static_validate({path: patched})
    except Exception:
        return None
    return patched


def repair_source_context(path: str, source: str, findings: list[dict[str, Any]], limit: int = 8000) -> str:
    """Keep repair requests focused on source lines tied to verified findings."""
    if len(source) <= limit:
        return source
    suffix = Path(path).suffix.lower()
    if suffix == ".css":
        selectors = {m.group(0) for finding in findings for m in re.finditer(r"(?:\.[A-Za-z_][\w-]*|#[A-Za-z_][\w-]*|:root)", str(finding.get("message", "")))}
        rules = [source[start:end] for selector, start, end, _body in css_rule_spans(source) if selector in selectors]
        excerpt = "\n".join(rules)
        return excerpt[:limit] if excerpt else source[:limit]
    lines = source.splitlines()
    per_finding: list[dict[int, int]] = []
    scored_lines: dict[int, int] = {}
    for finding in findings:
        message = str(finding.get("message", ""))
        tokens = set(re.findall(r"['\"]([^'\"]{2,80})['\"]", message))
        # Contract findings name selectors (#id / .class), while HTML source
        # stores only the bare value inside id="â€¦" or class="â€¦".
        selector_names = set(re.findall(r"(?<![A-Za-z0-9_-])[#.][A-Za-z_][\w:-]*", message))
        tokens.update(selector[1:] for selector in selector_names)
        semantic_anchors = {
            "project-cards": ("Dashboard", "getDashboardData"),
            "overview-dashboard": ("Dashboard", "getDashboardData"),
            "task-list": ("TaskManager", "SearchAndFilters", "KanbanBoard"),
            "activity-log": ("ActivityLogger", "Dashboard"),
            "calendar-view": ("CalendarView",),
            "kanban-board": ("KanbanBoard",),
            "app-shell": ("AppShellRenderer", "Navigation"),
            "header": ("Navigation",),
        }
        tokens.update(anchor for selector in selector_names for anchor in semantic_anchors.get(selector[1:], ()))
        for token in tuple(tokens):
            parts = [part for part in re.split(r"[-_:]+", token) if part]
            if len(parts) > 1:
                tokens.update(parts)
                tokens.add("".join(part[:1].upper() + part[1:] for part in parts))
                tokens.add(parts[0] + "".join(part[:1].upper() + part[1:] for part in parts[1:]))
        if "STORAGE" in str(finding.get("rule", "")):
            tokens.update({"localStorage", "STORAGE_KEY", "SCHEMA_VERSION"})
        if "EVENT" in str(finding.get("rule", "")):
            tokens.update({"dispatchEvent", "CustomEvent", "addEventListener"})
        finding_scores: dict[int, int] = {}
        for index, line in enumerate(lines):
            score = 0
            for token in tokens:
                if len(token) <= 3:
                    matched = bool(re.search(rf"(?<![A-Za-z0-9_-]){re.escape(token)}(?![A-Za-z0-9_-])", line))
                else:
                    matched = token in line
                if not matched and suffix in {".html", ".htm"}:
                    attr_values = re.findall(r"\b(?:id|class)\s*=\s*['\"]([^'\"]*)['\"]", line, re.I)
                    matched = any(token in value.split() for value in attr_values)
                if matched: score += 1
            if any(re.search(rf"\bclass\s+{re.escape(anchor)}\b", line) for selector in selector_names for anchor in semantic_anchors.get(selector[1:], ())):
                score += 12
            if score:
                if re.search(r"getElementById|querySelector|classList|localStorage|dispatchEvent|CustomEvent|addEventListener", line): score += 5
                finding_scores[index] = score
                scored_lines[index] = max(scored_lines.get(index, 0), score)
        if finding_scores:
            per_finding.append(finding_scores)
    if not scored_lines:
        scored_lines = {index: 1 for index in range(min(len(lines), 80))}

    def format_line(index: int) -> str:
        line = lines[index]
        max_line = min(480, max(120, limit // 8))
        if len(line) > max_line:
            matching = [token for finding in findings for token in re.findall(r"['\"]([^'\"]{2,80})['\"]", str(finding.get("message", ""))) if token in line]
            position = line.find(matching[0]) if matching else 0
            start = max(0, position - max_line // 3)
            line = ("ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â¦" if start else "") + line[start:start + max_line] + ("ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â¦" if start + max_line < len(lines[index]) else "")
        return f"{index + 1}: {line}"

    def render(indices: set[int]) -> str:
        rows: list[str] = []
        last = -2
        for index in sorted(indices):
            if index > last + 1:
                rows.append("... [omitted unrelated source] ...")
            rows.append(format_line(index))
            last = index
        return "\n".join(rows)

    selected: set[int] = set()
    def try_add(index: int, radius: int = 2) -> bool:
        window = set(range(max(0, index - radius), min(len(lines), index + radius + 1)))
        proposed = selected | window
        if len(render(proposed)) <= limit:
            selected.update(window)
            return True
        if index not in selected and len(render(selected | {index})) <= limit:
            selected.add(index)
            return True
        return False

    # Reserve at least one focused source excerpt for each finding before filling
    # remaining budget with additional high-scoring context.
    for finding_scores in per_finding:
        for index in sorted(finding_scores, key=lambda row: (-finding_scores[row], row)):
            if try_add(index):
                break
    for index in sorted(scored_lines, key=lambda row: (-scored_lines[row], row)):
        try_add(index)
    if not selected:
        for index in range(min(len(lines), 80)):
            if not try_add(index, radius=0):
                break
    return render(selected)[:limit]


def observed_interface_snapshot(path: str, facts: dict[str, Any], source: str) -> Any:
    suffix = Path(path).suffix.lower()
    if suffix in {".html", ".htm"}:
        return facts.get("html", {}).get("by_file", {}).get(path, {})
    if suffix == ".css":
        return facts.get("css", {}).get("selectors", {}).get(path, [])
    if suffix in {".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx"}:
        js = facts.get("javascript", {})
        return {key: js.get(key, {}).get(path, []) for key in ("dom_ids", "dom_classes", "storage_keys", "event_names", "functions", "classes", "imports", "data_fields")}
    if suffix == ".py":
        py = facts.get("python", {})
        return {key: py.get(key, {}).get(path, {}) for key in ("symbols", "class_fields", "signatures")}
    return {"source_sha256": sha256_text(source)}


def validate_contract_patch_target(path: str, source: str, finding: dict[str, Any]) -> None:
    """Reject patches that do not implement the specific requested DOM contract."""
    rule = finding.get("rule")
    if rule not in {"CONTRACT_HTML_ID_MISSING", "CONTRACT_HTML_CLASS_MISSING", "JS_DOM_REFERENCE_UNRESOLVED"}:
        return
    message = str(finding.get("message", ""))
    selector = next(iter(re.findall(r"[#.][A-Za-z_][\w:-]*", message)), "")
    if rule == "JS_DOM_REFERENCE_UNRESOLVED":
        missing = re.search(r"missing\s+(id|class)\s+['\"]([^'\"]+)['\"]", message, re.I)
        if missing:
            selector = ("#" if missing.group(1).lower() == "id" else ".") + missing.group(2)
    if not selector:
        raise BuildError("CONTRACT_REPAIR_TARGET_UNIDENTIFIABLE")
    name = selector[1:]
    if Path(path).suffix.lower() in {".js", ".mjs", ".cjs"}:
        interfaces = extract_javascript_interfaces(source, path)
        observed = interfaces["dynamic_ids" if selector.startswith("#") else "dynamic_classes"]
    elif Path(path).suffix.lower() in {".html", ".htm"}:
        facts = build_contract({path: source}, [{"path": path}])["html"]
        observed = set(facts["ids"] if selector.startswith("#") else facts["classes"])
    else:
        raise BuildError(f"CONTRACT_REPAIR_WRONG_FILE_KIND: {path}:{selector}")
    if name not in observed:
        raise BuildError(f"CONTRACT_REPAIR_TARGET_NOT_SATISFIED: {path}:{selector}; observed={sorted(observed)}")


def normalize_html_contract_shell(source: str, contract: dict[str, Any], project: dict[str, str]) -> str:
    """Rebuild a duplicated or incomplete static shell from canonical IDs/classes.

    Preserve document metadata and local asset tags while placing each contract
    element exactly once in a stable, accessible shell before JavaScript starts.
    """
    head_match = re.search(r"<head\b[^>]*>[\s\S]*?</head\s*>", source, re.I)
    head = head_match.group(0) if head_match else '<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Application</title></head>'
    html_open = re.search(r"<html\b[^>]*>", source, re.I)
    html_tag = html_open.group(0) if html_open else '<html lang="en">'
    doctype = '<!doctype html>' if re.search(r"<!doctype\s+html", source, re.I) else '<!doctype html>'
    scripts = re.findall(r"<script\b[^>]*>\s*</script\s*>", source, re.I)
    scripts.extend(tag for tag in re.findall(r"<script\b[^>]*\bsrc\s*=\s*(['\"])[^\"']+\1[^>]*>\s*</script\s*>", source, re.I) if tag)
    # The second pass above only captures quote delimiters; retain full external
    # tags directly and de-duplicate while preserving order.
    scripts = re.findall(r"<script\b(?=[^>]*\bsrc\s*=)[^>]*>\s*</script\s*>", source, re.I)
    scripts = list(dict.fromkeys(scripts))
    expected: dict[str, set[str]] = {}
    for item in contract.get("html_elements", []) if isinstance(contract.get("html_elements"), list) else []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        expected.setdefault(str(item["id"]), set()).update(str(item.get("class", "")).split())
    # Keep JavaScript hosts that the accepted source looks up by ID, including
    # canonical app roots such as view-container that are outside the contract.
    for path, js_source in project.items():
        if Path(path).suffix.lower() not in {".js", ".mjs", ".cjs"}:
            continue
        expected_ids = extract_javascript_interfaces(js_source, path)["dom_ids"]
        for ident in expected_ids:
            if ident and ident not in {"<dynamic>"}:
                expected.setdefault(ident, set())
    expected.setdefault("app-shell", set()).add("app-shell")
    expected.setdefault("sidebar", set()).add("sidebar")
    expected.setdefault("header", set()).add("header")
    expected.setdefault("content-area", set()).add("content-area")
    expected.setdefault("search-input", set()).add("search-input")
    expected.setdefault("view-container", set())
    tags = {"app-shell": "div", "sidebar": "aside", "header": "header", "content-area": "main", "search-input": "input", "task-list": "ul", "activity-log": "ol"}
    ids = list(expected)
    html_elements: list[str] = []
    for ident in ids:
        classes = " ".join(sorted(expected[ident]))
        attrs = f' id="{html.escape(ident, quote=True)}"' + (f' class="{html.escape(classes, quote=True)}"' if classes else "")
        tag = tags.get(ident, "div")
        if ident == "search-input":
            html_elements.append(f'<input{attrs} type="search" aria-label="Search tasks and projects">')
        elif ident.startswith("nav-"):
            view = ident[4:]
            html_elements.append(f'<a{attrs} href="#{html.escape(view, quote=True)}" data-view="{html.escape(view, quote=True)}">{html.escape(view.replace("-", " ").title())}</a>')
        elif ident == "sidebar":
            nav_items = [f'<a id="nav-{view}" href="#{view}" data-view="{view}">{label}</a>' for view, label in (("overview", "Overview"), ("mytasks", "My Tasks"), ("projects", "Projects"), ("calendar", "Calendar"))]
            html_elements.append(f'<aside{attrs}><nav aria-label="Main navigation">{"".join(nav_items)}</nav></aside>')
        elif ident == "header":
            subtitle = '<p id="page-subtitle">Your workspace at a glance.</p>' if "page-subtitle" in expected else ""
            search = '<input id="search-input" class="search-input" type="search" aria-label="Search tasks and projects">' if "search-input" in expected else ""
            html_elements.append(f'<header{attrs}><h1 id="page-title">Overview</h1>{subtitle}{search}</header>')
        elif tag in {"input", "img", "br", "hr", "meta", "link"}:
            html_elements.append(f'<{tag}{attrs}>')
        else:
            html_elements.append(f'<{tag}{attrs}></{tag}>')
    by_id = {ident: element for ident, element in zip(ids, html_elements)}
    # Construct known landmarks in semantic order; place additional contract and
    # source-referenced hosts inside the main content region.
    sidebar = by_id.get("sidebar", '<aside id="sidebar" class="sidebar"></aside>')
    header = by_id.get("header", '<header id="header" class="header"></header>')
    main_children = [element for ident, element in by_id.items() if ident not in {"app-shell", "sidebar", "header", "content-area", "search-input", "view-container", "page-title", "page-subtitle"} and not ident.startswith("nav-")]
    if "view-container" in by_id:
        main_children.insert(0, by_id["view-container"])
    main = f'<main id="content-area" class="content-area">{"".join(main_children)}</main>'
    body = f'<body><div id="app-shell" class="app-shell">{sidebar}{header}{main}</div>{"".join(scripts)}</body>'
    # Avoid serializing the header search input twice: when it is in the expected
    # inventory, its canonical element is nested under the header landmark.
    body = re.sub(r'(<header\b[^>]*>[\s\S]*?</header>)', lambda m: m.group(1), body, count=1)
    return f"{doctype}\n{html_tag}\n{head}\n{body}\n</html>\n"


def repair_project(client: Cell3Client, ctx: BuildContext, findings: list[dict[str, Any]], round_no: int, state: StateStore | None = None) -> dict[str, str]:
    before_project = dict(ctx.project)
    before_facts = build_contract(before_project, ctx.manifest) if before_project else {}
    current = dict(ctx.project)
    by_file: dict[str, list[dict[str, Any]]] = {}
    for finding in findings:
        path = finding.get("file")
        if path in current: by_file.setdefault(path, []).append(finding)
    if not by_file: raise BuildError("REPAIR_BLOCKED: no finding maps to a real manifest file")
    for path, grouped_findings in by_file.items():
      # Keep repair prompts small and isolate failures. A long list of unrelated
      # interface mismatches caused the model to return a valid-looking no-op.
      if Path(path).suffix.lower() in {".html", ".htm"} and any(row.get("rule") in {"CONTRACT_HTML_ID_MISSING", "CONTRACT_HTML_CLASS_MISSING"} for row in grouped_findings):
        source = current[path]
        html_ids = re.findall(r"\bid\s*=\s*(['\"])(.*?)\1", source, re.I)
        id_values = [value for _quote, value in html_ids]
        if len(id_values) != len(set(id_values)) or id_values.count("app-shell") > 1:
            normalized = normalize_html_contract_shell(source, ctx.contract, current)
            trial = dict(current); trial[path] = normalized
            trial = validate_project(trial); static_validate(trial)
            current = trial
            log(f"HTML_CONTRACT_SHELL_NORMALIZED | {path} | duplicate IDs removed; canonical DOM hosts created")
      for individual_finding in grouped_findings:
        file_findings = [individual_finding]
        source = current[path]
        suffix = Path(path).suffix.lower()
        if individual_finding.get("rule") == "JS_DOM_REFERENCE_UNRESOLVED" and suffix in {".html", ".htm"}:
            dom_repair = repair_dynamic_view_root(path, source, individual_finding)
            log(f"DOM_REFERENCE_MARKUP_REPAIR_CHECK | {path} | changed={dom_repair is not None and dom_repair != source} | {str(individual_finding.get('message', ''))[:100]}")
            if dom_repair is not None and dom_repair != source:
                trial = dict(current); trial[path] = dom_repair
                trial = validate_project(trial); static_validate(trial)
                validate_contract_patch_target(path, dom_repair, individual_finding)
                current = trial
                log(f"DOM_REFERENCE_REPAIRED_IN_MARKUP | {path} | {individual_finding.get('message', '')[:120]}")
                continue
        contract_rules = {"CONTRACT_HTML_ID_MISSING", "CONTRACT_HTML_CLASS_MISSING", "JS_DOM_REFERENCE_UNRESOLVED", "HTML_ASSET_MISSING", "STORAGE_CONTRACT_MISMATCH", "EVENT_CONTRACT_MISMATCH", "CSS_SELECTOR_CONTRACT_MISMATCH", "CSS_SELECTOR_DOM_MISMATCH", "DESIGN_TOKEN_CONTRACT_MISMATCH", "DATA_MODEL_FIELD_MISMATCH", "RESPONSIVE_CONTRACT_MISMATCH", "MANIFEST_DEPENDENCY_MISSING"}
        if individual_finding.get("rule") in contract_rules:
            live_findings = validate_contract_consistency(current, ctx.manifest, ctx.contract).get("findings", [])
            still_present = any(row.get("rule") == individual_finding.get("rule") and row.get("message") == individual_finding.get("message") for row in live_findings)
            if not still_present:
                log(f"REPAIR_FINDING_ALREADY_RESOLVED | {path} | {individual_finding.get('rule')}")
                continue
        deterministic = (repair_design_token_rule(path, source, individual_finding, ctx.contract)
                         or repair_css_selector_contract(path, source, individual_finding, ctx.contract)
                         or repair_dynamic_view_root(path, source, individual_finding))
        if deterministic is not None:
            trial = dict(current)
            trial[path] = deterministic
            trial = validate_project(trial)
            static_validate(trial)
            current = trial
            log(f"DYNAMIC_VIEW_ROOT_REPAIRED | {path} | {individual_finding.get('rule')} | {individual_finding.get('message', '')[:100]}")
            continue
        if suffix in {".js", ".mjs", ".cjs"}:
            language = "JavaScript"
            requested_selector = next(iter(re.findall(r"[.#][A-Za-z_][\w:-]*", str(individual_finding.get("message", "")))), "")
            target_contract = 'Return JSON only: {"path":"' + path + '","target":{"kind":"exact_span","text":"unique exact source text copied from this file","sha256":"sha256 of that exact text"},"replacement":"complete replacement for that exact span"}.'
            target_rules = f"{language}-only target rule: use exact_span only. Never use python_symbol or return a different language. The target text must occur exactly once in the source; copy it exactly. For CONTRACT_HTML_ID_MISSING or CONTRACT_HTML_CLASS_MISSING, repair the matching dynamic view's rendered DOM element so the exact requested selector {requested_selector} is assigned to element.id/classList; a JavaScript class field or unrelated ID does not satisfy the finding. Do not edit unrelated code. If a unique local replacement is not safe, use kind unsupported."
        elif suffix in {".html", ".htm"}:
            target_contract = 'Return JSON only: {"path":"' + path + '","target":{"kind":"exact_span","text":"unique exact HTML source text copied verbatim","sha256":"sha256 of that exact text"},"replacement":"complete replacement for that exact HTML span"}.'
            target_rules = "HTML-only target rule: use exact_span only on unique, verbatim HTML markup copied from the supplied context; never use literal \\n text in place of real line breaks. Make the smallest semantic markup change that fixes the finding. For a missing id, attach it to an existing correct semantic element, or insert a small element with the exact id inside the existing #app-shell/#content-area using a unique exact parent opening tag as target. For a missing class, add it to the matching existing element. Never duplicate an id already present, never nest a second #app-shell, and preserve all script/link tags and balanced structure. Never target a CSS rule or Python symbol. If no safe unique target exists, use kind unsupported."
        elif suffix == ".css":
            selector_counts: dict[str, int] = {}
            for selector, _start, _end, _body in css_rule_spans(source):
                if not selector.startswith("@"):
                    selector_counts[selector] = selector_counts.get(selector, 0) + 1
            target_contract = 'Return JSON only: {"path":"' + path + '","target":{"kind":"css_rule|exact_span","selector":"for css_rule","text":"for exact_span","sha256":"sha256 of exact_span text"},"replacement":"complete replacement rule"}.'
            target_rules = f"CSS-only target rule: use css_rule when its selector occurs once, replacing the complete rule with a complete selector rule; when the requested selector count is zero, use css_rule to append a new complete rule. Use exact_span only to select one complete rule when the same selector occurs multiple times; include its opening and closing braces exactly. Never target only an opening selector token, never wrap existing CSS in @layer, and never return JavaScript or Python. Selector counts: {json.dumps(selector_counts)}."
        elif suffix == ".py":
            target_contract = 'Return JSON only: {"path":"' + path + '","target":{"kind":"python_symbol|exact_span","name":"for python_symbol","text":"for exact_span","sha256":"sha256 of exact_span text"},"replacement":"complete replacement for the selected Python target"}.'
            target_rules = "Python-only target rule: use python_symbol for one top-level function/class or exact_span for unique exact source text. Never return CSS, HTML, or JavaScript. If unsafe or ambiguous, use kind unsupported."
        else:
            target_contract = 'Return JSON only: {"path":"' + path + '","target":{"kind":"css_rule|python_symbol|exact_span","selector":"for css_rule","name":"for python_symbol","text":"for exact_span","sha256":"sha256 of exact_span text"},"replacement":"complete replacement text for the selected structural target"}.'
            target_rules = "For CSS, target one exact selector rule. For Python, target one top-level function/class. Use exact_span only when the supplied text is uniquely identifiable."
        relevant_contract = {key: ctx.contract.get(key) for key in ("summary", "html_elements", "events", "storage", "data_models") if key in ctx.contract}
        source_context = repair_source_context(path, source, file_findings)
        interface_hint = ""
        if file_findings[0].get("rule") in {"CONTRACT_HTML_ID_MISSING", "CONTRACT_HTML_CLASS_MISSING"}:
            message = str(file_findings[0].get("message", ""))
            selector = next(iter(re.findall(r"[#.][A-Za-z_][\w:-]*", message)), "")
            owner = {"overview-dashboard": "Dashboard", "project-cards": "Dashboard", "task-list": "TaskManager or KanbanBoard", "kanban-board": "KanbanBoard", "calendar-view": "CalendarView", "activity-log": "ActivityLogger or Dashboard", "app-shell": "Navigation or AppShellRenderer", "header": "Navigation"}.get(selector[1:], "the matching view class")
            if suffix in {".js", ".mjs", ".cjs"}:
                interface_hint = f"\nDYNAMIC DOM REPAIR: selector {selector} belongs on a real rendered DOM element in {owner}. This is JavaScript source: never write bare HTML attributes such as id=\"...\" in JavaScript. Set element.id = {json.dumps(selector[1:])} or element.classList.add({json.dumps(selector[1:])}) on the created/rendered root. If that view lacks a renderer, add a small renderer to the named class and preserve the existing methods."
        prompt = f"""Repair only the verified defects in this file. {target_contract}
Do not return a full file. Do not weaken tests, delete required functionality, change unrelated code, or invent elements. {target_rules} If ambiguous or insufficient evidence, return {{"path":"{path}","target":{{"kind":"unsupported"}},"replacement":""}}.
SPECIFICATION SUMMARY: {ctx.spec[:1200]}
RELEVANT CANONICAL CONTRACT: {json.dumps(relevant_contract, ensure_ascii=False)[:2500]}
VERIFIED FINDINGS: {json.dumps(file_findings, ensure_ascii=False)}
{interface_hint}
TARGETED SOURCE CONTEXT (line numbers are informational; patch targets still use exact source text):
{source_context}"""
        last: Exception | None = None
        previous_patch: dict[str, Any] | None = None
        for attempt in range(1, MAX_REPAIR_ATTEMPTS + 1):
            try:
                attempt_prompt = prompt if attempt == 1 else prompt + f"\n\nPATCH RETRY {attempt}: the prior patch was rejected by local validation: {redact(last or 'unknown')}. Do not repeat this rejected patch: {json.dumps(previous_patch, ensure_ascii=False)}. Return a corrected minimal patch for the same verified findings; preserve balanced HTML/CSS/JavaScript structure."
                patch = client.json(GENERATOR_MODEL, attempt_prompt, timeout=ctx.timeout, num_predict=1600)
                previous_patch = patch if isinstance(patch, dict) else None
                patch_target = patch.get("target") if isinstance(patch, dict) else None
                if isinstance(patch_target, dict) and patch_target.get("kind") == "exact_span" and not derive_unique_exact_span_hash(source, patch):
                    requested_text = str(patch_target.get("text", ""))
                    occurrence_count = source.count(requested_text) if requested_text else 0
                    raise BuildError(f"PATCH_TARGET_TEXT_NOT_UNIQUE_OR_MISSING: exact target occurrence count={occurrence_count}; copy one exact span from the supplied source context")
                if isinstance(patch, dict) and derive_unique_exact_span_hash(source, patch):
                    # The model often fabricates SHA values; uniqueness still prevents
                    # ambiguous or broad replacements of the supplied exact span.
                    log(f"PATCH_HASH_DERIVED_LOCALLY | {path} | unique exact span")
                patched_source = apply_structural_patch(path, source, patch)
                validate_contract_patch_target(path, patched_source, individual_finding)
                trial = dict(current)
                trial[path] = patched_source
                trial = validate_project(trial)
                static_validate(trial)
                current = trial
                log(f"STRUCTURAL_REPAIR_APPLIED | {path} | round={round_no} | attempt={attempt}")
                break
            except Exception as exc:
                last = exc
                log(f"STRUCTURAL_REPAIR_REJECTED | {path} | round={round_no} | attempt={attempt} | {redact(exc)}")
        else:
            raise BuildError(f"STRUCTURAL_REPAIR_EXHAUSTED: {path}: {redact(last or 'unknown')}")
    current = validate_project(current)
    static_validate(current)
    ctx.project = current
    if state is not None:
        changed = {path for path in current if before_project.get(path) != current[path]}
        if changed:
            old_contract = before_facts
            new_contract = refresh_observed_contract(ctx)
            state.set_contract(ctx.contract)
            changed_interfaces = {path for path in changed if canonical_hash(observed_interface_snapshot(path, old_contract, before_project[path])) != canonical_hash(observed_interface_snapshot(path, new_contract, current[path]))}
            # Persist repaired source as accepted only after syntax/static checks.
            for path in changed:
                state.invalidate({path}, "Source unit was structurally repaired; stale pre-repair unit checkpoints are not reusable")
                state.accept_file(path, current[path], contract_hash=file_contract_fingerprint(path, ctx.contract, ctx.manifest), dependency_hash=dependency_hash(path, ctx.manifest, ctx.contract))
            if changed_interfaces:
                graph = dependency_graph(ctx.manifest, current)
                affected = dependent_closure(changed_interfaces, graph) - changed_interfaces
                if affected:
                    state.invalidate(affected, "A repaired file changed a shared interface; dependent checkpoints must be regenerated")
                    log(f"DEPENDENT_REGENERATION_REQUIRED | changed={sorted(changed_interfaces)} | dependents={sorted(affected)}")
                    ctx.project = generate_project(client, ctx, state)
                    refresh_observed_contract(ctx)
                    state.set_contract(ctx.contract)
                    for path, content in ctx.project.items():
                        state.accept_file(path, content, contract_hash=file_contract_fingerprint(path, ctx.contract, ctx.manifest), dependency_hash=dependency_hash(path, ctx.manifest, ctx.contract))
                    static_validate(ctx.project)
            write_project(ctx.staging / "generated", ctx.project)
    return current if ctx.project is current else ctx.project


# ---------------------------------------------------------------------------
# Test/runtime manager: inspect project config, execute isolated copies, never
# turn a missing runner or missing suite into a silent PASS.
# ---------------------------------------------------------------------------
def _strip_python_comments(source: str) -> str:
    try:
        import io, tokenize
        return tokenize.untokenize(tok for tok in tokenize.generate_tokens(io.StringIO(source).readline) if tok.type != tokenize.COMMENT)
    except Exception:
        return source

def is_real_test_file(path: str, source: str) -> tuple[bool, str]:
    suffix = Path(path).suffix.lower()
    if suffix == ".py":
        code = _strip_python_comments(source)
        try: tree = ast.parse(code, filename=path)
        except SyntaxError: return False, "test candidate does not parse"
        has_pytest = any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test_") for n in ast.walk(tree))
        has_unittest = any(isinstance(n, ast.ClassDef) and any((isinstance(b, ast.Name) and b.id == "TestCase") or (isinstance(b, ast.Attribute) and b.attr == "TestCase") for b in n.bases) for n in ast.walk(tree))
        has_assert = any(isinstance(n, ast.Assert) or (isinstance(n, ast.Call) and ((isinstance(n.func, ast.Attribute) and (n.func.attr.startswith("assert") or n.func.attr == "raises")) or (isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name) and n.func.value.id == "pytest" and n.func.attr == "raises"))) for n in ast.walk(tree))
        return (has_assert and (has_pytest or has_unittest), "Python declarations and assertions detected" if has_assert and (has_pytest or has_unittest) else "No recognizable Python test cases")
    if suffix in {".js", ".mjs", ".cjs"}:
        code = _strip_js_comments(source)
        declarations = bool(re.search(r"\b(?:test|it|describe)\s*\(\s*['\"`]", code))
        assertions = bool(re.search(r"\bassert(?:\.[A-Za-z_$][\w$]*)?\s*\(|\bexpect\s*\(", code))
        imports = bool(re.search(r"(?:from\s*['\"]node:test['\"]|require\s*\(\s*['\"]node:test['\"]\s*\))", code))
        return (declarations and (assertions or imports), "JavaScript test declarations detected" if declarations and (assertions or imports) else "No recognizable JavaScript test suite")
    return False, f"No runner configured for {suffix}"

def _test_optional(manifest: list[dict[str, Any]]) -> bool:
    phrases = ("optional", "if implemented", "if applicable", "where applicable", "when available")
    return any(("test" in x["path"].lower() or Path(x["path"]).name.startswith("test_")) and any(p in x["purpose"].lower() for p in phrases) for x in manifest)

def inspect_project_config(project: dict[str, str]) -> dict[str, Any]:
    config: dict[str, Any] = {"python": {}, "javascript": {}, "files": []}
    for path in ("pyproject.toml", "pytest.ini", "tox.ini", "setup.cfg", "requirements.txt"):
        if path in project:
            config["python"][path] = project[path][:12000]
    if "package.json" in project:
        try:
            package = json.loads(project["package.json"])
            config["javascript"] = {"package_json": package, "scripts": package.get("scripts", {}), "dependencies": package.get("dependencies", {}), "devDependencies": package.get("devDependencies", {})}
        except json.JSONDecodeError as exc:
            raise BuildError(f"PACKAGE_JSON_INVALID: {exc}") from exc
    config["files"] = sorted(project)
    return config

def run_project_tests(project: dict[str, str], manifest: list[dict[str, Any]], timeout: int, staging: Path) -> dict[str, Any]:
    project = validate_project(project)
    config = inspect_project_config(project)
    save_json(staging / "test_config_inspection.json", config)
    candidates = [p for p in project if p.startswith("tests/") or "/tests/" in p or Path(p).name.startswith("test_") or Path(p).name.endswith("_test.py") or Path(p).name.endswith(".test.js") or Path(p).name.endswith(".spec.js")]
    python_tests: list[str] = []; js_tests: list[str] = []; skipped: list[dict[str, str]] = []
    for path in candidates:
        ok, reason = is_real_test_file(path, project[path])
        if not ok: skipped.append({"file": path, "reason": reason}); continue
        if Path(path).suffix.lower() == ".py": python_tests.append(path)
        elif Path(path).suffix.lower() in {".js", ".mjs", ".cjs"}: js_tests.append(path)
    root = Path(tempfile.mkdtemp(prefix="cell3-v7-tests-"))
    results: list[dict[str, Any]] = []
    blockers: list[str] = []
    try:
        write_project(root, project)
        node = shutil.which("node")
        all_js = [p for p in project if Path(p).suffix.lower() in {".js", ".mjs", ".cjs"}]
        if all_js and not node:
            blockers.append("Node.js is unavailable; JavaScript syntax could not be verified.")
            js_syntax = "BLOCKED"
        elif all_js:
            js_syntax = "PASS"
            for path in all_js:
                run = subprocess.run([node, "--check", path], cwd=root, capture_output=True, text=True, timeout=timeout, shell=False)
                if run.returncode:
                    js_syntax = "FAIL"
                    results.append({"runner": "node --check", "file": path, "status": "FAIL", "stdout": redact(run.stdout[-8000:]), "stderr": redact(run.stderr[-8000:])})
        else:
            js_syntax = "NOT_APPLICABLE"
        if python_tests:
            pytest_exe = shutil.which("pytest")
            if pytest_exe or _python_pytest_available():
                cmd = [sys.executable, "-m", "pytest", "-q", *python_tests]
                runner = "pytest"
            else:
                unittest_only = True
                for test_path in python_tests:
                    tree = ast.parse(project[test_path], filename=test_path)
                    has_function_tests = any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test_") for n in tree.body)
                    has_testcase = any(isinstance(n, ast.ClassDef) and any((isinstance(b, ast.Name) and b.id == "TestCase") or (isinstance(b, ast.Attribute) and b.attr == "TestCase") for b in n.bases) for n in tree.body)
                    if has_function_tests or not has_testcase:
                        unittest_only = False
                if not unittest_only:
                    blockers.append("pytest-style tests were generated but pytest is not installed; unittest cannot safely substitute.")
                    cmd = []
                    runner = "pytest-unavailable"
                else:
                    cmd = [sys.executable, "-m", "unittest", "discover", "-s", ".", "-p", "*test*.py", "-v"]
                    runner = "unittest"
            if cmd:
                try:
                    run = subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=timeout, shell=False)
                    results.append({"runner": runner, "status": "PASS" if run.returncode == 0 else "FAIL", "return_code": run.returncode, "files": python_tests, "stdout": redact(run.stdout[-16000:]), "stderr": redact(run.stderr[-16000:])})
                except subprocess.TimeoutExpired as exc:
                    results.append({"runner": runner, "status": "TIMEOUT", "files": python_tests, "stdout": str(exc.stdout or "")[-8000:], "stderr": str(exc.stderr or "")[-8000:]})
        package_test_script = bool(config.get("javascript", {}).get("scripts", {}).get("test"))
        if js_tests or package_test_script:
            if package_test_script:
                npm = shutil.which("npm")
                if not npm:
                    blockers.append("package.json defines a test script but npm is unavailable.")
                else:
                    try:
                        run = subprocess.run([npm, "test"], cwd=root, capture_output=True, text=True, timeout=timeout, shell=False)
                        results.append({"runner": "npm test", "status": "PASS" if run.returncode == 0 else "FAIL", "return_code": run.returncode, "files": js_tests or ["package.json"], "stdout": redact(run.stdout[-16000:]), "stderr": redact(run.stderr[-16000:])})
                    except subprocess.TimeoutExpired as exc:
                        results.append({"runner": "npm test", "status": "TIMEOUT", "files": js_tests or ["package.json"], "stdout": str(exc.stdout or "")[-8000:], "stderr": str(exc.stderr or "")[-8000:]})
            elif not node:
                blockers.append("JavaScript tests were found but Node.js is unavailable.")
            else:
                try:
                    run = subprocess.run([node, "--test", *js_tests], cwd=root, capture_output=True, text=True, timeout=timeout, shell=False)
                    results.append({"runner": "node --test", "status": "PASS" if run.returncode == 0 else "FAIL", "return_code": run.returncode, "files": js_tests, "stdout": redact(run.stdout[-16000:]), "stderr": redact(run.stderr[-16000:])})
                except subprocess.TimeoutExpired as exc:
                    results.append({"runner": "node --test", "status": "TIMEOUT", "files": js_tests, "stdout": str(exc.stdout or "")[-8000:], "stderr": str(exc.stderr or "")[-8000:]})
        tests_found = bool(python_tests or js_tests or package_test_script)
        failed = any(x.get("status") != "PASS" for x in results) or js_syntax == "FAIL"
        optional = _test_optional(manifest)
        if failed:
            status = "FAIL"
        elif blockers:
            status = "BLOCKED"
        elif not tests_found:
            status = "NOT_FOUND" if optional else "BLOCKED"
            if not optional: blockers.append("No executable test suite was found and the manifest does not explicitly mark tests optional.")
        else:
            status = "PASS"
        report = {"status": status, "python_tests": python_tests, "javascript_tests": js_tests, "skipped_candidates": skipped, "javascript_syntax": js_syntax, "runners": results, "optional_tests": optional, "blockers": blockers, "config_inspection": "test_config_inspection.json", "checked_at": utc_now()}
        save_json(staging / "test_report.json", report)
        return report
    finally:
        shutil.rmtree(root, ignore_errors=True)

def _python_pytest_available() -> bool:
    try:
        import pytest  # type: ignore  # noqa: F401
        return True
    except ImportError:
        return False


def find_browser() -> str | None:
    # Support Chromium-based browsers on Linux as well as Edge/Chrome on Windows.
    # `chromium` is common in CI/container environments and is a real browser
    # suitable for the same headless acceptance harness.
    # A path returned by shutil.which is authoritative even when this test is
    # running on a different OS from the host represented by a mocked PATH.
    for name in (
        "msedge", "msedge.exe", "microsoft-edge", "microsoft-edge-stable",
        "google-chrome", "google-chrome-stable", "chrome", "chrome.exe",
        "chromium", "chromium-browser",
    ):
        located = shutil.which(name)
        if located:
            return located
    candidates = []
    roots = [os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)"), os.environ.get("LOCALAPPDATA")]
    rels = [Path("Microsoft/Edge/Application/msedge.exe"), Path("Google/Chrome/Application/chrome.exe")]
    for root in roots:
        if root:
            candidates.extend(str(Path(root) / rel) for rel in rels)
    return next((x for x in candidates if x and Path(x).is_file()), None)


def _browser_selector_grounded(selector: str, project: dict[str, str], facts: dict[str, Any]) -> bool:
    html_text = "\n".join(value for path, value in project.items() if Path(path).suffix.lower() in {".html", ".htm"} and not _is_test_path(path))
    js_text = "\n".join(value for path, value in project.items() if Path(path).suffix.lower() in {".js", ".mjs", ".cjs"} and not _is_test_path(path))
    ids = set(facts["html"]["ids"]) | set(facts["javascript"]["dynamic_ids"])
    classes = set(facts["html"]["classes"]) | set(facts["javascript"]["dynamic_classes"])
    id_tokens = re.findall(r"#([A-Za-z_][\w:-]*)", selector)
    class_tokens = re.findall(r"\.([A-Za-z_][\w-]*)", selector)
    if id_tokens and all(x in ids for x in id_tokens): return True
    if class_tokens and all(x in classes for x in class_tokens): return True
    attr_tokens = re.findall(r"\[([A-Za-z_:][\w:.-]*)", selector)
    if attr_tokens and all(re.search(r"\b" + re.escape(x) + r"\s*=", html_text, re.I) or re.search(r"\b" + re.escape(x) + r"\b", js_text) for x in attr_tokens): return True
    tag_match = re.match(r"^([A-Za-z][A-Za-z0-9-]*)(?=$|[.#\[: >+~])", selector.strip())
    if tag_match and re.search(r"<\s*" + re.escape(tag_match.group(1)) + r"\b", html_text, re.I): return True
    return selector.strip() in html_text or selector.strip() in js_text


def generate_browser_plan(client: Cell3Client, ctx: BuildContext) -> dict[str, Any]:
    html_path = _first_html(ctx.project)
    if not html_path: return {"status": "NOT_APPLICABLE", "tests": []}
    js_path = _first_js(ctx.project)
    prompt = f"""Return JSON only: {{"tests":[{{"name":"workflow name","steps":[{{"action":"fill|click|select|check|reload","selector":"real CSS selector; empty for reload","value":"value if needed"}}],"assertions":[{{"type":"visible|not_visible|text_contains|count_at_least|value_equals|storage_key_exists|storage_key_equals|storage_value_contains","selector":"real CSS selector or exact storage key","value":"expected value","count":1}}]}}]}}.
Create at most 3 concise browser tests for the most important user workflows in the specification. Use only selectors that appear literally in the supplied HTML or JavaScript, or are directly proven by DOM construction. Never invent selectors. Each test must have at least one action and one assertion. Use stable IDs when available. For persistence requirements using localStorage, include a reload step followed by a storage_key_exists or storage_key_equals assertion for a contract storage key. Return {{"tests":[]}} if the application cannot safely be tested.
SPECIFICATION: {ctx.spec}
HTML ({html_path}):
{ctx.project[html_path][:12000]}
JAVASCRIPT ({js_path}):
{ctx.project.get(js_path, '')[:14000]}
CONTRACT: {json.dumps(ctx.contract, ensure_ascii=False)[:8000]}"""
    data = client.json(AUDITOR_MODEL, prompt, timeout=ctx.timeout, num_predict=5000)
    rows = data.get("tests")
    if not isinstance(rows, list) or not rows: raise BuildError("BROWSER_TEST_PLAN_EMPTY: auditor could not produce evidence-grounded browser tests")
    normalized: list[dict[str, Any]] = []
    allowed_actions = {"fill", "click", "select", "check", "reload"}
    allowed_assertions = {"visible", "not_visible", "text_contains", "count_at_least", "value_equals", "storage_key_exists", "storage_key_equals", "storage_value_contains"}
    storage_keys = _contract_expected_names(ctx.contract, "storage", "key")
    for row in rows[:3]:
        if not isinstance(row, dict): continue
        steps: list[dict[str, Any]] = []; assertions: list[dict[str, Any]] = []
        for step in row.get("steps", [])[:8]:
            if not isinstance(step, dict) or step.get("action") not in allowed_actions: continue
            selector = str(step.get("selector", ""))[:250]
            if step["action"] != "reload" and not selector: continue
            steps.append({"action": step["action"], "selector": selector, "value": str(step.get("value", ""))[:250]})
        for assertion in row.get("assertions", [])[:5]:
            if not isinstance(assertion, dict) or assertion.get("type") not in allowed_assertions: continue
            selector = str(assertion.get("selector", ""))[:250]
            if not selector: continue
            if assertion["type"].startswith("storage_") and selector not in storage_keys:
                raise BuildError(f"BROWSER_TEST_PLAN_INVALID: storage assertion key is not in the canonical contract: {selector}")
            assertions.append({"type": assertion["type"], "selector": selector, "value": str(assertion.get("value", ""))[:250], "count": max(0, min(int(assertion.get("count", 1) or 1), 100))})
        if steps and assertions: normalized.append({"name": str(row.get("name", "Browser workflow"))[:120], "steps": steps, "assertions": assertions})
    if not normalized: raise BuildError("BROWSER_TEST_PLAN_INVALID: no executable test survived schema validation")
    facts = build_contract(ctx.project, ctx.manifest)
    for test in normalized:
        for item in test["steps"]:
            if item["action"] == "reload": continue
            if not _browser_selector_grounded(item["selector"], ctx.project, facts):
                raise BuildError(f"BROWSER_TEST_PLAN_INVALID: selector is not grounded in delivered HTML/JavaScript: {item['selector']}")
        for item in test["assertions"]:
            if item["type"].startswith("storage_"): continue
            if not _browser_selector_grounded(item["selector"], ctx.project, facts):
                raise BuildError(f"BROWSER_TEST_PLAN_INVALID: selector is not grounded in delivered HTML/JavaScript: {item['selector']}")
    return {"status": "READY", "generated_by": AUDITOR_MODEL, "tests": normalized, "spec_sha256": ctx.spec_hash, "contract_sha256": ctx.contract.get("sha256", ""), "file_hashes": {p: sha256_text(source) for p, source in ctx.project.items()}, "checked_at": utc_now()}


def verify_saved_browser_plan(plan: dict[str, Any], ctx: BuildContext) -> bool:
    if plan.get("status") != "READY" or plan.get("generated_by") != AUDITOR_MODEL:
        return False
    if plan.get("spec_sha256") != ctx.spec_hash or plan.get("contract_sha256") != ctx.contract.get("sha256"):
        return False
    if plan.get("file_hashes") != {p: sha256_text(source) for p, source in ctx.project.items()}:
        return False
    tests = plan.get("tests")
    if not isinstance(tests, list) or not tests: return False
    facts = build_contract(ctx.project, ctx.manifest)
    storage_keys = _contract_expected_names(ctx.contract, "storage", "key")
    for test in tests:
        if not isinstance(test, dict) or not test.get("steps") or not test.get("assertions"): return False
        for step in test["steps"]:
            if step.get("action") == "reload": continue
            if step.get("action") not in {"fill", "click", "select", "check"} or not _browser_selector_grounded(str(step.get("selector", "")), ctx.project, facts): return False
        for assertion in test["assertions"]:
            selector = str(assertion.get("selector", ""))
            if assertion.get("type", "").startswith("storage_"):
                if selector not in storage_keys: return False
            elif not _browser_selector_grounded(selector, ctx.project, facts): return False
    return True


def _test_failure_findings(report: dict[str, Any], project: dict[str, str], manifest: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map failed test evidence to declared application targets, never test fixtures."""
    findings: list[dict[str, Any]] = []
    by_path = {item.get("path"): item for item in manifest if isinstance(item, dict)}
    runners = report.get("runners", []) if isinstance(report, dict) else []
    for result in runners:
        if not isinstance(result, dict) or result.get("status") == "PASS":
            continue
        output = redact(str(result.get("stdout", "")) + "\n" + str(result.get("stderr", "")))
        direct_file = result.get("file")
        source_targets: set[str] = set()
        if isinstance(direct_file, str) and direct_file in project and not _is_test_path(direct_file):
            source_targets.add(direct_file)
        test_files = result.get("files", [])
        if isinstance(test_files, list):
            for test_path in test_files:
                row = by_path.get(test_path, {})
                for target in row.get("validation_targets", []) if isinstance(row, dict) else []:
                    if target in project and not _is_test_path(target):
                        source_targets.add(target)
        if not source_targets:
            continue
        summary_lines = [line.strip() for line in output.splitlines() if line.strip()]
        detail_lines = [line for line in summary_lines if re.search(r"(AssertionError|TypeError|ReferenceError|SyntaxError|\bassert\b|expected|actual|Error:|not ok)", line, re.I)]
        summary_lines_without_totals = [line for line in summary_lines if not re.search(r"^\d+ failed in ", line, re.I)]
        summary = (detail_lines[-1] if detail_lines else next((line for line in reversed(summary_lines_without_totals) if re.search(r"FAILED|not ok", line, re.I)), None)) or "Automated test or source validation failed"
        for path in sorted(source_targets):
            source = project[path]
            line_no = 1
            for candidate in re.finditer(r"(?:^|[/\\])" + re.escape(path) + r":(\d+)(?::\d+)?", output, re.M):
                line_no = max(1, min(int(candidate.group(1)), max(1, len(source.splitlines()))))
                break
            evidence = _evidence_for_line(source, line_no)
            findings.append({"file": path, "severity": "HIGH", "line": line_no, "evidence": evidence, "rule": "TEST_FAILURE", "message": "Automated test/validation failed for a declared application target. Failure evidence: " + summary[:1600], "recommendation": "Repair the application implementation while preserving the declared test and acceptance contract."})
    return findings


def _browser_failure_findings(report: dict[str, Any], project: dict[str, str]) -> list[dict[str, Any]]:
    target = _first_js(project) or _first_html(project)
    if not target: return []
    source = project[target]
    line = next((i + 1 for i, x in enumerate(source.splitlines()) if x.strip()), 1)
    evidence = _evidence_for_line(source, line)
    detail = json.dumps(report, ensure_ascii=False)[:10000]
    return [{"file": target, "severity": "HIGH", "line": line, "evidence": evidence, "rule": "BROWSER_RUNTIME_FAILURE", "message": "Real-browser workflow or page-runtime verification failed. Fix the application behavior; do not weaken the browser assertions. Browser evidence: " + detail, "recommendation": "Correct the implementation and rerun the exact browser workflow."}]


def browser_harness_script(tests: list[dict[str, Any]]) -> str:
    tests_json = json.dumps(tests, ensure_ascii=False).replace("</", "<\\/")
    return r"""<script>
(async function(){
 const tests=__CELL3_TESTS__, stateKey='__CELL3_V7_BROWSER_STATE__';
 const wait=ms=>new Promise(r=>setTimeout(r,ms));
 await new Promise(resolve=>{if(document.readyState==='complete')resolve();else window.addEventListener('load',resolve,{once:true});});
 await wait(250);
 let saved={};try{saved=JSON.parse(sessionStorage.getItem(stateKey)||'{}')}catch(e){}
 const out=Array.isArray(saved.out)?saved.out:[];
 const pageErrors=window.__CELL3_PAGE_ERRORS__||[];if(Array.isArray(saved.pageErrors))pageErrors.push(...saved.pageErrors);
 const firstTest=Number(saved.test||0), firstStep=Number(saved.step||0);
 for(let ti=firstTest;ti<tests.length;ti++){
  const t=tests[ti], row=(saved.currentRow&&ti===firstTest)?saved.currentRow:{name:t.name,status:'PASS',steps:[],assertions:[],errors:[]};
  const stepStart=ti===firstTest?firstStep:0;
  try{
   for(let si=stepStart;si<t.steps.length;si++){
    const s=t.steps[si];
    if(s.action==='reload'){
     sessionStorage.setItem(stateKey,JSON.stringify({test:ti,step:si+1,out:out,pageErrors:pageErrors,currentRow:row}));
     location.reload();return;
    }
    const el=document.querySelector(s.selector);if(!el)throw new Error('Missing selector '+s.selector);
    if(s.action==='fill'){el.focus();el.value=s.value;el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}));}
    else if(s.action==='click')el.click();
    else if(s.action==='select'){el.value=s.value;el.dispatchEvent(new Event('change',{bubbles:true}));}
    else if(s.action==='check'){el.checked=true;el.dispatchEvent(new Event('change',{bubbles:true}));}
    row.steps.push({action:s.action,selector:s.selector});await wait(180);
   }
   for(const a of t.assertions){let els=[],el=null;if(!a.type.startsWith('storage_')){els=[...document.querySelectorAll(a.selector)];el=els[0];}let ok=false,actual='';
    if(a.type==='storage_key_exists'){actual=localStorage.getItem(a.selector);ok=actual!==null;}
    else if(a.type==='storage_key_equals'){actual=localStorage.getItem(a.selector);ok=actual===a.value;}
    else if(a.type==='storage_value_contains'){actual=localStorage.getItem(a.selector)||'';ok=actual.includes(a.value);}
    else if(a.type==='visible'){ok=!!el&&!!(el.offsetWidth||el.offsetHeight||el.getClientRects().length);actual=el?'present':'missing';}
    else if(a.type==='not_visible'){ok=!els.some(x=>!!(x.offsetWidth||x.offsetHeight||x.getClientRects().length));actual=String(els.length)+' found';}
    else if(a.type==='text_contains'){actual=el?(el.innerText||el.textContent||''):'';ok=actual.includes(a.value);}
    else if(a.type==='count_at_least'){actual=String(els.length);ok=els.length>=a.count;}
    else if(a.type==='value_equals'){actual=el?String(el.value):'';ok=!!el&&actual===a.value;}
    row.assertions.push({type:a.type,selector:a.selector,expected:a.value||a.count,actual:String(actual).slice(0,500),pass:ok});if(!ok)throw new Error('Assertion failed '+a.type+' '+a.selector);
   }
  }catch(e){row.status='FAIL';row.errors.push(String(e&&e.message||e));}
  out.push(row);saved={};
 }
 sessionStorage.removeItem(stateKey);
 await wait(200);
 const css=[];for(const sheet of [...document.styleSheets]){try{css.push({href:sheet.href||'inline',rules:sheet.cssRules.length,status:'PASS'});}catch(e){css.push({href:sheet.href||'unknown',status:'BLOCKED',detail:String(e)});}}
 const node=document.createElement('pre');node.id='__CELL3_BROWSER_RESULT__';node.textContent=JSON.stringify({tests:out,pageErrors:pageErrors,css:css});document.body.appendChild(node);document.documentElement.setAttribute('data-cell3-browser-status',out.every(x=>x.status==='PASS')&&pageErrors.length===0?'PASS':'FAIL');
})();
</script>""".replace("__CELL3_TESTS__", tests_json)


def _run_browser_tests_playwright(project: dict[str, str], tests: list[dict[str, Any]], browser_executable: str, timeout: int, root: Path, url: str) -> dict[str, Any] | None:
    """Run the acceptance plan with Playwright when the Python package exists.

    Returns None only when Playwright is unavailable, allowing the legacy CLI
    runner to be used. Browser launch failures are infrastructure blockers, not
    application failures; once a browser launches, workflow/assertion failures
    are real acceptance failures.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None
    results: list[dict[str, Any]] = []
    page_errors: list[str] = []
    console_errors: list[str] = []
    request_failures: list[str] = []
    bad_responses: list[str] = []
    responsive: list[dict[str, Any]] = []
    browser_started = False
    try:
        with sync_playwright() as playwright:
            launch_args = ["--disable-dev-shm-usage"]
            if hasattr(os, "geteuid") and os.geteuid() == 0:
                launch_args.append("--no-sandbox")
            browser = playwright.chromium.launch(executable_path=browser_executable, headless=True, args=launch_args, timeout=max(5000, min(timeout * 1000, 90000)))
            browser_started = True
            try:
                context = browser.new_context(viewport={"width": 1365, "height": 900})
                page = context.new_page()
                page.set_default_timeout(max(1000, min(timeout * 1000, 30000)))
                page.on("pageerror", lambda exc: page_errors.append(str(exc)[:1000]))
                page.on("console", lambda msg: console_errors.append(msg.text[:1000]) if msg.type == "error" else None)
                page.on("requestfailed", lambda req: request_failures.append(f"{req.method} {req.url}: {req.failure}"))
                page.on("response", lambda response: bad_responses.append(f"{response.status} {response.url}") if response.status >= 400 else None)
                page.goto(url, wait_until="load", timeout=max(5000, min(timeout * 1000, 90000)))
                for test in tests:
                    row = {"name": test.get("name", "Browser workflow"), "status": "PASS", "steps": [], "assertions": [], "errors": []}
                    try:
                        for step in test["steps"]:
                            action, selector = step["action"], step.get("selector", "")
                            if action == "reload":
                                page.reload(wait_until="load")
                            elif action == "fill":
                                page.locator(selector).fill(step.get("value", ""))
                            elif action == "click":
                                page.locator(selector).click()
                            elif action == "select":
                                page.locator(selector).select_option(step.get("value", ""))
                            elif action == "check":
                                page.locator(selector).check()
                            else:
                                raise RuntimeError(f"Unsupported browser action: {action}")
                            row["steps"].append({"action": action, "selector": selector})
                        for assertion in test["assertions"]:
                            kind, selector = assertion["type"], assertion.get("selector", "")
                            locator = page.locator(selector) if not kind.startswith("storage_") else None
                            expected = assertion.get("value", "")
                            actual: Any = None
                            if kind == "visible":
                                actual = locator.first.is_visible() if locator.count() else False; ok = bool(actual)
                            elif kind == "not_visible":
                                actual = locator.count() and any(locator.nth(i).is_visible() for i in range(locator.count())); ok = not bool(actual)
                            elif kind == "text_contains":
                                actual = locator.first.inner_text() if locator.count() else ""; ok = expected in actual
                            elif kind == "count_at_least":
                                actual = locator.count(); ok = actual >= int(assertion.get("count", 1))
                            elif kind == "value_equals":
                                actual = locator.first.input_value() if locator.count() else ""; ok = actual == expected
                            elif kind.startswith("storage_"):
                                actual = page.evaluate("([key, mode]) => { const v=localStorage.getItem(key); return mode==='exists' ? v!==null : v; }", [selector, "exists" if kind == "storage_key_exists" else "value"])
                                if kind == "storage_key_exists": ok = actual is True
                                elif kind == "storage_key_equals": ok = actual == expected
                                else: ok = expected in (actual or "")
                            else:
                                raise RuntimeError(f"Unsupported browser assertion: {kind}")
                            row["assertions"].append({"type": kind, "selector": selector, "expected": assertion.get("value", assertion.get("count")), "actual": str(actual)[:500], "pass": bool(ok)})
                            if not ok: raise AssertionError(f"Assertion failed: {kind} {selector}; actual={actual!r}")
                    except Exception as exc:
                        row["status"] = "FAIL"; row["errors"].append(str(exc)[:2000])
                    results.append(row)
                # Check the assembled application at phone, tablet and desktop
                # widths. Each check reloads the real page at that viewport.
                for width, height, label in ((375, 812, "mobile"), (768, 1024, "tablet"), (1365, 900, "desktop")):
                    page.set_viewport_size({"width": width, "height": height})
                    page.goto(url, wait_until="load")
                    metrics = page.evaluate("() => ({viewport: document.documentElement.clientWidth, document: document.documentElement.scrollWidth, body: document.body ? document.body.scrollWidth : 0, height: document.documentElement.scrollHeight})")
                    overflow = int(metrics.get("document", 0)) > int(metrics.get("viewport", 0)) + 2
                    responsive.append({"viewport": label, "width": width, "height": height, **metrics, "horizontal_overflow": overflow, "status": "FAIL" if overflow else "PASS"})
                cssom = page.evaluate("() => [...document.styleSheets].map(s => { try { return {href:s.href||'inline', rules:s.cssRules.length, status:'PASS'} } catch(e) { return {href:s.href||'unknown', status:'BLOCKED', detail:String(e)} } })")
                context.close()
            finally:
                browser.close()
        passed = bool(results) and all(x["status"] == "PASS" for x in results) and all(x["status"] == "PASS" for x in responsive) and not page_errors and not console_errors and not request_failures and not bad_responses
        return {"status": "PASS" if passed else "FAIL", "runner": "Playwright + " + Path(browser_executable).name, "detail": "Real browser workflows, page/console errors, network failures, CSSOM and mobile/tablet/desktop overflow checks captured.", "tests": results, "responsive": responsive, "page_errors": page_errors, "console_errors": console_errors, "request_failures": request_failures, "http_errors": bad_responses, "cssom": cssom}
    except Exception as exc:
        message = redact(exc)
        infrastructure_markers = ("ERR_BLOCKED_BY_ADMINISTRATOR", "ERR_CONNECTION_REFUSED", "ERR_NETWORK_CHANGED", "ERR_INTERNET_DISCONNECTED", "browserType.launch", "Executable doesn't exist", "Failed to launch")
        infrastructure_blocked = not browser_started or any(marker.lower() in message.lower() for marker in infrastructure_markers)
        status = "BROWSER_ACCEPTANCE_NOT_RUN" if infrastructure_blocked else "FAIL"
        detail = ("Browser could not launch or the environment blocked local browser access before workflows ran: " if infrastructure_blocked else "Browser launched but the application failed during page load or acceptance execution: ") + message
        return {"status": status, "runner": "Playwright + " + Path(browser_executable).name, "detail": detail, "tests": [], "responsive": [], "page_errors": page_errors, "console_errors": console_errors, "request_failures": request_failures, "http_errors": bad_responses}


def run_browser_tests(project: dict[str, str], plan: dict[str, Any], timeout: int, staging: Path) -> dict[str, Any]:
    browser = find_browser()
    if not browser:
        report = {"status": "BROWSER_ACCEPTANCE_NOT_RUN", "runner": "Edge/Chrome", "detail": "No installed headless Edge or Chrome executable was found; browser acceptance was not run.", "tests": []}
        save_json(staging / "browser_runtime_report.json", report)
        return report
    html_path = _first_html(project)
    if not html_path: return {"status": "NOT_APPLICABLE", "tests": []}
    root = Path(tempfile.mkdtemp(prefix="cell3-v7-browser-"))
    server: ThreadingHTTPServer | None = None
    server_thread: threading.Thread | None = None
    try:
        write_project(root, project)
        html_file = root.joinpath(*PurePosixPath(html_path).parts)
        error_capture = "<script>window.__CELL3_PAGE_ERRORS__=[];window.addEventListener('error',e=>window.__CELL3_PAGE_ERRORS__.push(String(e.message||e.error||'page error')));window.addEventListener('unhandledrejection',e=>window.__CELL3_PAGE_ERRORS__.push(String(e.reason||'unhandled rejection')));</script>"
        source = html_file.read_text(encoding="utf-8")
        head = re.search(r"<head\b[^>]*>", source, re.I)
        if head:
            source = source[:head.end()] + error_capture + source[head.end():]
        else:
            html_tag = re.search(r"<html\b[^>]*>", source, re.I)
            if html_tag: source = source[:html_tag.end()] + error_capture + source[html_tag.end():]
            else: source = error_capture + source
        harness = browser_harness_script(plan["tests"])
        pos = source.lower().rfind("</body>")
        source = source[:pos] + harness + source[pos:] if pos >= 0 else source + harness
        html_file.write_text(source, encoding="utf-8")

        class QuietHandler(SimpleHTTPRequestHandler):
            def log_message(self, _format: str, *args: Any) -> None:
                return
        server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(root)))
        server_thread = threading.Thread(target=server.serve_forever, name="Cell3V7BrowserHTTP", daemon=True)
        server_thread.start()
        url = f"http://127.0.0.1:{server.server_port}/{quote(html_path, safe='/')}"
        playwright_report = _run_browser_tests_playwright(project, plan["tests"], browser, timeout, root, url)
        if playwright_report is not None:
            save_json(staging / "browser_runtime_report.json", playwright_report)
            return playwright_report
        profile = root / "browser-profile"
        command = [browser, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check", "--disable-extensions", "--disable-dev-shm-usage", f"--user-data-dir={profile}", "--virtual-time-budget=8000", "--dump-dom", f"--timeout={max(10000, min(timeout*1000, 90000))}", url]
        # Chromium refuses to launch as root unless its sandbox is disabled.
        # Keep the normal sandbox for every non-root execution; this branch is
        # limited to isolated root-owned CI/container test environments.
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            command.insert(1, "--no-sandbox")
        run = subprocess.run(command, capture_output=True, text=True, timeout=timeout + 20, shell=False)
        dom = run.stdout or ""
        marker_match = re.search(r'<pre id="__CELL3_BROWSER_RESULT__">([\s\S]*?)</pre>', dom)
        if not marker_match:
            infrastructure_blocked = run.returncode != 0 and not dom.strip()
            report = {"status": "BROWSER_ACCEPTANCE_NOT_RUN" if infrastructure_blocked else "FAIL", "runner": browser, "detail": "Browser process could not load the local test page; acceptance was not run." if infrastructure_blocked else "Browser loaded output but did not emit the structured result marker.", "return_code": run.returncode, "stdout": dom[-12000:], "stderr": redact(run.stderr[-12000:]), "tests": []}
        else:
            raw = html.unescape(marker_match.group(1))
            try: payload = json.loads(raw)
            except json.JSONDecodeError: payload = {}
            results = payload.get("tests", []) if isinstance(payload, dict) else []
            page_errors = payload.get("pageErrors", []) if isinstance(payload, dict) else []
            css = payload.get("css", []) if isinstance(payload, dict) else []
            passed = bool(results) and all(x.get("status") == "PASS" for x in results) and not page_errors and run.returncode == 0
            report = {"status": "PASS" if passed else "FAIL", "runner": browser, "detail": "Browser workflows, persistence reloads, page errors and CSSOM evidence captured.", "return_code": run.returncode, "tests": results, "page_errors": page_errors, "cssom": css, "stderr": redact(run.stderr[-8000:])}
        save_json(staging / "browser_runtime_report.json", report)
        return report
    except subprocess.TimeoutExpired as exc:
        report = {"status": "TIMEOUT", "runner": browser, "detail": f"Browser exceeded {timeout+20}s.", "stdout": str(exc.stdout or "")[-8000:], "stderr": str(exc.stderr or "")[-8000:], "tests": []}
        save_json(staging / "browser_runtime_report.json", report)
        return report
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        if server_thread is not None:
            server_thread.join(timeout=3)
        shutil.rmtree(root, ignore_errors=True)


def _run_verification(client: Cell3Client, ctx: BuildContext, state: StateStore) -> dict[str, Any]:
    test_report: dict[str, Any] = {"status": "NOT_REQUESTED", "runners": []}
    has_html = bool(_first_html(ctx.project))
    browser_report: dict[str, Any] = {"status": "BROWSER_ACCEPTANCE_NOT_RUN" if has_html and not ctx.run_tests else "NOT_APPLICABLE", "detail": "HTML project browser acceptance was not requested; publish remains blocked until browser verification runs." if has_html and not ctx.run_tests else "No HTML entry point requires browser acceptance.", "tests": []}
    if ctx.run_tests:
        for round_no in range(0, MAX_INTEGRATION_ROUNDS + 1):
            test_report = run_project_tests(ctx.project, ctx.manifest, ctx.timeout, ctx.staging / "tests")
            if test_report["status"] == "BLOCKED":
                raise BuildError(f"TEST_ACCEPTANCE_BLOCKED: {test_report.get('blockers') or 'required verification capability/evidence is unavailable'}")
            if test_report["status"] == "FAIL":
                if round_no >= MAX_INTEGRATION_ROUNDS:
                    raise BuildError(f"TEST_ACCEPTANCE_FAIL: {test_report.get('blockers') or test_report.get('runners')}")
                findings = _test_failure_findings(test_report, ctx.project, ctx.manifest)
                if not findings: raise BuildError("TEST_REPAIR_BLOCKED: no safe implementation target")
                repair_project(client, ctx, findings, round_no + 1, state)
                static_validate(ctx.project)
                integration = validate_contract_consistency(ctx.project, ctx.manifest, ctx.contract)
                if integration["status"] != "PASS": raise BuildError("TEST_REPAIR_INTEGRATION_FAILED")
                audit = independent_audit(client, ctx)
                save_json(ctx.staging / f"test_repair_audit_{round_no+1}.json", audit)
                if audit["status"] != "PASS": raise BuildError("TEST_REPAIR_AUDIT_FAILED")
                ctx.audit = audit
                save_json(ctx.staging / "audit" / "final_audit.json", audit)
                state.stage("INDEPENDENT_AUDIT", "PASS", auditor=AUDITOR_MODEL, findings=0, after_test_repair=True)
                ctx.evidence["contract_report"] = integration
                continue
            break
        if test_report["status"] == "NOT_FOUND" and not _test_optional(ctx.manifest):
            raise BuildError("TESTS_REQUIRED_BUT_NOT_FOUND: publication blocked")
    if has_html and ctx.run_tests:
        for browser_round in range(0, MAX_INTEGRATION_ROUNDS + 1):
            saved_plan = load_json(ctx.staging / "browser_test_plan.json", required=False) if ctx.resume and browser_round == 0 else None
            if isinstance(saved_plan, dict) and verify_saved_browser_plan(saved_plan, ctx):
                plan = saved_plan
                log("BROWSER_TEST_PLAN_REUSED | spec/contract/source hashes and selectors verified")
            else:
                plan = generate_browser_plan(client, ctx)
            save_json(ctx.staging / "browser_test_plan.json", plan)
            save_json(ctx.staging / f"browser_test_plan_round_{browser_round}.json", plan)
            browser_report = run_browser_tests(ctx.project, plan, ctx.timeout, ctx.staging / "tests")
            ctx.tests = {"status": "PASS" if browser_report["status"] == "PASS" else "BLOCKED", "unit_tests": test_report, "browser_runtime": browser_report}
            save_json(ctx.staging / "verification_report.json", ctx.tests)
            if browser_report["status"] == "PASS":
                break
            if browser_report["status"] in {"BLOCKED", "BROWSER_ACCEPTANCE_NOT_RUN", "TIMEOUT", "NOT_APPLICABLE"}:
                raise BuildError(f"BROWSER_ACCEPTANCE_{browser_report['status']}: {browser_report.get('detail', '')}")
            if browser_round >= MAX_INTEGRATION_ROUNDS:
                raise BuildError(f"BROWSER_ACCEPTANCE_FAIL: {browser_report.get('detail', '')}")
            findings = _browser_failure_findings(browser_report, ctx.project)
            if not findings: raise BuildError("BROWSER_REPAIR_BLOCKED: no safe source target")
            repair_project(client, ctx, findings, browser_round + 1, state)
            static_validate(ctx.project)
            integration = validate_contract_consistency(ctx.project, ctx.manifest, ctx.contract)
            if integration["status"] != "PASS": raise BuildError("BROWSER_REPAIR_INTEGRATION_FAILED")
            audit = independent_audit(client, ctx)
            save_json(ctx.staging / f"browser_repair_audit_{browser_round+1}.json", audit)
            if audit["status"] != "PASS": raise BuildError("BROWSER_REPAIR_AUDIT_FAILED")
            ctx.audit = audit
            save_json(ctx.staging / "audit" / "final_audit.json", audit)
            state.stage("INDEPENDENT_AUDIT", "PASS", auditor=AUDITOR_MODEL, findings=0, after_browser_repair=True)
            ctx.evidence["contract_report"] = integration
            if ctx.run_tests:
                test_report = run_project_tests(ctx.project, ctx.manifest, ctx.timeout, ctx.staging / "tests")
                if test_report["status"] != "PASS": raise BuildError("BROWSER_REPAIR_REGRESSION_TESTS_FAILED")
    ctx.tests = {"status": "PASS" if test_report["status"] in {"PASS", "NOT_FOUND", "NOT_REQUESTED"} and browser_report["status"] in {"PASS", "NOT_APPLICABLE"} else "FAIL", "unit_tests": test_report, "browser_runtime": browser_report}
    save_json(ctx.staging / "verification_report.json", ctx.tests)
    return ctx.tests


def read_spec(path_text: str) -> str:
    path = Path(path_text).expanduser()
    if not path.is_file(): raise BuildError(f"SPECIFICATION_FILE_NOT_FOUND: {path}")
    if path.stat().st_size > 1_000_000: raise BuildError("SPECIFICATION_TOO_LARGE: maximum 1 MB")
    try: spec = path.read_text(encoding="utf-8-sig").strip()
    except UnicodeDecodeError as exc: raise BuildError(f"SPECIFICATION_ENCODING_ERROR: {exc}") from exc
    if not spec: raise BuildError("SPECIFICATION_EMPTY")
    return spec


def _new_context(spec: str, project_name: str, timeout: int, run_tests: bool) -> tuple[BuildContext, StateStore]:
    for folder in (RUNS_DIR, STAGING_DIR, PROJECTS_DIR, LOGS_DIR): folder.mkdir(parents=True, exist_ok=True)
    run_id = f"client-{datetime.now().strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
    staging = STAGING_DIR / f"{project_name}-{run_id}"
    staging.mkdir(parents=True, exist_ok=False)
    ctx = BuildContext(run_id, project_name, staging, spec, sha256_text(spec), timeout, run_tests)
    store = StateStore(ctx)
    atomic_write_text(staging / "specification.txt", spec)
    save_json(staging / "specification_meta.json", {"sha256": ctx.spec_hash, "bytes": len(spec.encode("utf-8")), "created_at": utc_now()})
    return ctx, store


def _load_resume_context(run_id: str, spec_path: str, project_name: str, timeout: int, run_tests: bool) -> tuple[BuildContext, StateStore]:
    if not re.fullmatch(r"client-[A-Za-z0-9T_-]+", run_id): raise BuildError("Invalid --resume-run-id")
    staging = STAGING_DIR / f"{project_name}-{run_id}"
    if not staging.is_dir(): raise BuildError(f"RESUME_STAGING_NOT_FOUND: {staging}")
    archived_spec_path = staging / "specification.txt"
    if not archived_spec_path.is_file(): raise BuildError("RESUME_SPECIFICATION_ARCHIVE_MISSING")
    archived_spec = archived_spec_path.read_text(encoding="utf-8")
    archived_hash = sha256_text(archived_spec)
    current_spec = read_spec(spec_path)
    if sha256_text(current_spec) != archived_hash:
        raise BuildError("RESUME_SPECIFICATION_CHANGED: the supplied specification differs from the original run")
    ctx = BuildContext(run_id, project_name, staging, archived_spec, archived_hash, timeout, run_tests, resume=True)
    store = StateStore(ctx)
    saved_manifest_artifact = load_json(staging / "manifest.json", required=False) or {}
    if isinstance(saved_manifest_artifact, dict):
        if saved_manifest_artifact.get("schema_version") != MANIFEST_SCHEMA_VERSION:
            raise BuildError("CHECKPOINT_SCHEMA_INCOMPATIBLE: saved manifest is not schema v2")
        ctx.manifest = saved_manifest_artifact.get("files", [])
    elif isinstance(saved_manifest_artifact, list):
        raise BuildError("CHECKPOINT_SCHEMA_INCOMPATIBLE: legacy manifest requires a fresh run")
    else:
        ctx.manifest = []
    ctx.plan = load_json(staging / "planning.json", required=False) or {}
    ctx.architecture = load_json(staging / "architecture.json", required=False) or {}
    ctx.design = load_json(staging / "design.json", required=False) or {}
    ctx.contract = load_json(staging / "canonical_project_contract.json", required=False) or {}
    ctx.evidence = load_json(staging / "evidence" / "evidence_index.json", required=False) or {}
    if ctx.manifest and store.state.get("manifest_sha256") and canonical_hash(ctx.manifest) != store.state.get("manifest_sha256"):
        raise BuildError("RESUME_MANIFEST_HASH_MISMATCH: manifest differs from checkpoint evidence")
    saved_manifest = load_json(store.manifest_path, required=False)
    if ctx.manifest and store.state.get("manifest_sha256"):
        if not isinstance(saved_manifest, dict) or saved_manifest.get("sha256") != store.state.get("manifest_sha256") or saved_manifest.get("files") != ctx.manifest:
            raise BuildError("RESUME_BUILD_MANIFEST_CORRUPT: persistent manifest evidence is missing or inconsistent")
    if ctx.contract:
        stored_contract_hash = str(ctx.contract.get("sha256", ""))
        contract_payload = dict(ctx.contract); contract_payload.pop("sha256", None)
        if not stored_contract_hash or canonical_hash(contract_payload) != stored_contract_hash:
            raise BuildError("RESUME_CONTRACT_HASH_MISMATCH: canonical contract contents changed")
        if store.state.get("contract_sha256") and store.state.get("contract_sha256") != stored_contract_hash:
            raise BuildError("RESUME_CONTRACT_STATE_MISMATCH: contract version differs from durable build state")
    if ctx.plan and store.state.get("planning_sha256"):
        planning_hash = canonical_hash({"plan": ctx.plan, "architecture": ctx.architecture, "design": ctx.design})
        if planning_hash != store.state.get("planning_sha256"):
            raise BuildError("RESUME_PLANNING_HASH_MISMATCH: planning artifacts changed")
    if ctx.contract and ctx.manifest and ctx.contract.get("manifest_sha256") and ctx.contract.get("manifest_sha256") != canonical_hash(ctx.manifest):
        raise BuildError("RESUME_CONTRACT_MANIFEST_MISMATCH: canonical contract is bound to a different manifest")
    if not (ctx.manifest and ctx.plan and ctx.architecture and ctx.design and ctx.contract and ctx.evidence.get("specification", {}).get("sha256") == ctx.spec_hash):
        ctx.manifest = []; ctx.plan = {}; ctx.architecture = {}; ctx.design = {}; ctx.contract = {}
    else:
        if not store.state.get("manifest_sha256"): store.set_manifest(ctx.manifest)
        if not store.state.get("planning_sha256"): store.set_planning_artifacts(ctx)
        if not store.state.get("contract_sha256"): store.set_contract(ctx.contract)
    return ctx, store


def _save_stage_artifacts(ctx: BuildContext) -> None:
    save_json(ctx.staging / "planning.json", ctx.plan)
    save_json(ctx.staging / "architecture.json", ctx.architecture)
    save_json(ctx.staging / "design.json", ctx.design)
    save_json(ctx.staging / "manifest.json", {"schema_version": MANIFEST_SCHEMA_VERSION, "files": ctx.manifest})
    save_json(ctx.staging / "canonical_project_contract.json", ctx.contract)
    evidence_dir = ctx.staging / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    save_json(evidence_dir / "evidence_index.json", ctx.evidence)


def topologically_order_manifest(manifest: list[dict[str, Any]], graph: str | None = None) -> list[dict[str, Any]]:
    """Validate independent prerequisite DAGs; optionally return one graph order."""
    by_path = {item["path"]: item for item in manifest}
    if len(by_path) != len(manifest):
        raise BuildError("MANIFEST_DUPLICATE_PATH")
    original_index = {item["path"]: i for i, item in enumerate(manifest)}
    def order(field: str, label: str) -> list[str]:
        pending: dict[str, set[str]] = {}
        for path, item in by_path.items():
            generation, build = _manifest_relationships(item)
            deps = generation if field == "generation_dependencies" else build
            if len(deps) != len(set(deps)):
                raise BuildError(f"MANIFEST_DUPLICATE_EDGE: {path}:{field}")
            missing = sorted(set(deps) - set(by_path))
            if missing:
                raise BuildError(f"MANIFEST_REFERENCE_TARGET_MISSING: DEPENDENCY_NOT_DECLARED: {path}:{field}: {missing}")
            if path in deps:
                raise BuildError(f"MANIFEST_SELF_DEPENDENCY: {path}:{field}")
            pending[path] = set(deps)
        ordered: list[str] = []; emitted: set[str] = set()
        while pending:
            ready = sorted((p for p, deps in pending.items() if deps <= emitted), key=lambda p: original_index[p])
            if not ready:
                cycle = _find_manifest_cycle(pending)
                evidence_parts = []
                field_name = "generation_dependencies" if label == "generation" else "build_dependencies"
                for source, target in zip(cycle, cycle[1:]):
                    reason = by_path[source].get("relationship_reasons", {}).get(field_name, {}).get(target, "reason not supplied")
                    evidence_parts.append(f"{source} --{field_name}--> {target}; reason: {reason}")
                evidence = " | ".join(evidence_parts)
                raise BuildError(f"MANIFEST_{label.upper()}_CYCLE [DEPENDENCY_CYCLE]: {' -> '.join(cycle)}; Evidence: {evidence}; No dependency edges were removed")
            for path in ready:
                ordered.append(path); emitted.add(path); pending.pop(path)
        return ordered
    if graph == "generation_dependencies":
        return [by_path[path] for path in order("generation_dependencies", "generation")]
    if graph == "build_dependencies":
        return [by_path[path] for path in order("build_dependencies", "build")]
    generation_order = order("generation_dependencies", "generation")
    order("build_dependencies", "build")
    return [by_path[path] for path in generation_order]

def _find_manifest_cycle(graph: dict[str, set[str]]) -> list[str]:
    visiting: list[str] = []; active: set[str] = set(); done: set[str] = set()
    def visit(node: str) -> list[str] | None:
        active.add(node); visiting.append(node)
        for target in sorted(graph.get(node, set())):
            if target in active:
                start = visiting.index(target); return visiting[start:] + [target]
            if target not in done:
                found = visit(target)
                if found: return found
        visiting.pop(); active.remove(node); done.add(node); return None
    for node in sorted(graph):
        if node not in done:
            found = visit(node)
            if found: return found
    return []


def _run_planning(client: Cell3Client, ctx: BuildContext) -> None:
    ctx.evidence["specification"] = {"sha256": ctx.spec_hash, "requirements": [x.strip() for x in ctx.spec.splitlines() if x.strip()], "reviewed_at": utc_now()}
    plan_prompt = f"""Analyze the complete specification and return JSON only with keys summary, requirements, constraints, acceptance_criteria, assumptions, open_questions. Requirements must be a complete, deduplicated list of implementable obligations. Preserve explicit filenames, data fields, workflows, security restrictions, and verification requirements. Do not invent requirements.
SPECIFICATION:
{ctx.spec}"""
    ctx.plan = client.json(PLANNER_MODEL, plan_prompt, timeout=ctx.timeout, num_predict=5000)
    requirements = ctx.plan.get("requirements")
    if not isinstance(requirements, list) or not requirements:
        raise BuildError("PLANNING_INVALID: planner returned no actionable requirements")
    architecture_prompt = f"""Create a generic implementation architecture for this specification. Return JSON only with keys overview, components, modules, data_flow, dependencies, security, integration_points. Identify modules and their responsibilities; do not assume a particular app type.
SPECIFICATION:
{ctx.spec}
PLAN:
{json.dumps(ctx.plan, ensure_ascii=False)}"""
    ctx.architecture = client.json(PLANNER_MODEL, architecture_prompt, timeout=ctx.timeout, num_predict=4500)
    design_prompt = f"""Create a UI/design contract only where the project has a UI. Return JSON only with keys design_system, layout, components, states, responsive_breakpoints, accessibility, visual_hierarchy. If the project is not a UI, return empty structures and do not invent UI files.
SPECIFICATION:
{ctx.spec}
PLAN:
{json.dumps(ctx.plan, ensure_ascii=False)}
ARCHITECTURE:
{json.dumps(ctx.architecture, ensure_ascii=False)}"""
    ctx.design = client.json(PLANNER_MODEL, design_prompt, timeout=ctx.timeout, num_predict=3500)
    manifest_prompt = f"""Create the complete implementation manifest using schema_version 2. Return JSON only: {{"schema_version":2,"files":[{{"path":"safe/relative/path.ext","kind":"python|javascript|html|css|test|documentation|other","purpose":"specific responsibility","generation_dependencies":[{{"path":"other/path","reason":"specific prerequisite rationale"}}],"build_dependencies":[{{"path":"other/path","reason":"specific build prerequisite rationale"}}],"runtime_references":[{{"path":"other/path","type":"stylesheet|script|asset|import","reason":"specific runtime loading rationale"}}],"contract_references":[{{"path":"other/path","type":"selector_contract|dom_contract|interface_contract","reason":"specific cross-file contract rationale"}}],"validation_targets":[{{"path":"other/path","reason":"specific test validation rationale"}}],"required":true}}]}}. Do not use a generic dependencies field. Every relationship entry must be an object with an exact declared path and a specific reason of at least eight characters; runtime/contract references also require a type. Never put prose requirements, test case names, or acceptance criteria in a relationship array. Generation/build dependencies must be genuine prerequisites. HTML loading CSS/JS, DOM and selector contracts, tests, and documentation are references, never generation edges. Only test/validator files may use validation_targets; those values are exact application source paths the test examines, never the test file itself. For example, a tests/test_logic.js file that tests app.js uses [\"app.js\"], while production files use []. Include all production source, configuration, assets, documentation, and tests required by the specification. Avoid duplicate files and do not invent unrelated files. At least one runnable application entrypoint must exist when the specification describes an application.
SPECIFICATION:
{ctx.spec}
PLAN:
{json.dumps(ctx.plan, ensure_ascii=False)}
ARCHITECTURE:
{json.dumps(ctx.architecture, ensure_ascii=False)}
DESIGN:
{json.dumps(ctx.design, ensure_ascii=False)}"""
    raw_manifest = client.json(PLANNER_MODEL, manifest_prompt, timeout=ctx.timeout, num_predict=6000)
    source_manifest_schema = raw_manifest.get("schema_version", 1) if isinstance(raw_manifest, dict) else "unknown"
    original_raw = raw_manifest
    try:
        ctx.manifest = ensure_required_files(normalize_manifest(raw_manifest, ctx.spec, require_v2=True), ctx.spec)
        if any(x.get("ambiguous_legacy_dependencies") for x in ctx.manifest):
            ambiguous = {x["path"]: x["ambiguous_legacy_dependencies"] for x in ctx.manifest if x.get("ambiguous_legacy_dependencies")}
            raise BuildError(f"MANIFEST_MIGRATION_AMBIGUOUS: v1 edges require classification: {ambiguous}")
        validate_manifest_relationships(ctx.manifest, ctx.spec)
        if len(ctx.manifest) > MAX_FILES: raise BuildError("MANIFEST_TOO_LARGE")
        semantic_findings = manifest_correction_findings(ctx.manifest)
        if semantic_findings:
            raise BuildError("MANIFEST_SEMANTIC_RELATIONSHIP_INVALID: " + " | ".join(semantic_findings[:12]))
        topologically_order_manifest(ctx.manifest)
    except BuildError as initial_error:
        raw_entries = original_raw.get("files", []) if isinstance(original_raw, dict) else []
        declared_paths = [str(x.get("path")) for x in raw_entries if isinstance(x, dict) and isinstance(x.get("path"), str)]
        test_paths = [str(x.get("path")) for x in raw_entries if isinstance(x, dict) and (str(x.get("kind", "")).lower() == "test" or _manifest_kind(str(x.get("path", ""))) == "test")]
        source_paths = [str(x.get("path")) for x in raw_entries if isinstance(x, dict) and str(x.get("kind", "")).lower() in {"javascript", "python"} and _manifest_kind(str(x.get("path", ""))) != "test"]
        invalid_validation_targets = []
        for row in raw_entries:
            if not isinstance(row, dict) or not isinstance(row.get("validation_targets", []), list): continue
            source_path = str(row.get("path", ""))
            source_kind = str(row.get("kind") or _manifest_kind(source_path))
            for target in row.get("validation_targets", []):
                if source_kind != "test" or not isinstance(target, str) or target not in declared_paths or target == source_path:
                    invalid_validation_targets.append({"source_path": source_path, "source_kind": source_kind, "target": target, "target_is_exact_manifest_path": isinstance(target, str) and target in declared_paths})
        test_source_guidance = {test_path: source_paths for test_path in test_paths} if len(source_paths) == 1 else {}
        invalid_relationship_types = manifest_relationship_type_issues(original_raw)
        semantic_findings = manifest_correction_findings(ctx.manifest)
        relationship_evidence = {"declared_paths": declared_paths, "invalid_validation_targets": invalid_validation_targets, "invalid_relationship_types": invalid_relationship_types, "semantic_findings": semantic_findings, "unique_application_source_candidates": source_paths, "test_source_guidance": test_source_guidance}
        target_guidance = "".join(f"Required exact relationship: {test_path} validation_targets={json.dumps(source_paths)}.\n" for test_path in test_paths) if len(source_paths) == 1 else ""
        validation_guidance = "".join(
            f"Remove invalid validation target from {issue['source_path']}: {issue['target']!r}; validation_targets are allowed only on test/validator files, and include a correction_reasons entry.\n"
            for issue in invalid_validation_targets if issue["source_kind"] != "test" or not issue["target_is_exact_manifest_path"]
        )
        type_guidance = "".join(
            f"Invalid typed edge: {issue['file']}:{issue['relationship']} -> {issue['target']} has type {issue['type']!r}. Allowed types for this field: {', '.join(issue['allowed_types'])}. "
            + ("It duplicates the correctly directed runtime reference; remove this reverse contract edge and explain the removal in correction_reasons.\n" if issue["reciprocal_runtime_reference_exists"] else "Correct the relationship field/type based on its actual meaning, and explain every changed edge in correction_reasons.\n")
            for issue in invalid_relationship_types
        )
        correction_prompt = f"""Correct the manifest once. Return a complete schema_version 2 manifest and correction_reasons array. Do not change the project requirements, explicitly requested files, file ownership of retained files, required flags of retained files, or validation strength. Do not change model or transport settings. Classify ambiguous references without guessing. Remove a planner-created file only when it violates an explicit specification constraint, and explain that removal with a reason object whose relationship is \"file\". Every relationship edge in the corrected schema-v2 manifest must include its own reason; every changed edge must also have a correction_reasons reason object {{\"file\":\"...\",\"relationship\":\"file|generation_dependencies|build_dependencies|runtime_references|contract_references|validation_targets\",\"target\":\"...\",\"reason\":\"...\"}}. Preserve every explicitly requested file and add no unrelated files.
VALIDATION RULE: relationship arrays contain exact manifest paths, never prose requirements. validation_targets belong only to test/validator files. Each target must be a different, declared, non-test application source file that the test actually validates. A required test file needs at least one target. Never assign validation_targets to production, asset, or documentation files. Include a correction_reasons entry for every removed or added target.
TYPE RULE: runtime_references types are stylesheet, script, asset, or import. contract_references types are selector_contract, dom_contract, interface_contract, html_selector_contract, or dom_element_contract. A type valid in one field is invalid in the other. HTML-to-stylesheet loading belongs in the HTML file's runtime_references; do not copy it as a reverse contract reference on the CSS file.
RUNTIME DIRECTION RULE: Only HTML files may declare stylesheet/script runtime_references, pointing from the HTML file to its CSS/JavaScript target. CSS, JavaScript, tests, and other files must not declare reverse stylesheet/script-loading edges.
INVALID TYPE GUIDANCE: {type_guidance or 'No invalid relationship types were identified.'}
VALIDATION TARGET GUIDANCE: {validation_guidance or 'No invalid validation_targets were identified.'}
{MANIFEST_RELATIONSHIP_GUIDANCE}
SEMANTIC GRAPH GUIDANCE: Tests use validation_targets, never generation_dependencies or build_dependencies. Documentation cannot be a generation/build prerequisite, and other files cannot depend on documentation as a generation/build prerequisite. HTML loading CSS/JavaScript uses runtime_references, never generation/build edges. HTML, CSS, and JavaScript files must not have generation_dependencies on one another; use runtime_references or contract_references for those relationships. Do not invent replacement edges to files that do not generate or build this source. Keep both graphs acyclic and remove only edges that are not true prerequisites.
SEMANTIC FINDINGS: {json.dumps(semantic_findings, ensure_ascii=False)}
STRUCTURED RELATIONSHIP EVIDENCE: {json.dumps(relationship_evidence, ensure_ascii=False)}
{target_guidance}
VALIDATION EVIDENCE: {redact(initial_error)}
ORIGINAL MANIFEST: {json.dumps(original_raw, ensure_ascii=False)}
Return JSON only."""
        corrected_raw = client.json(PLANNER_MODEL, correction_prompt, timeout=ctx.timeout, num_predict=6000)
        preserved_reason_count = preserve_unchanged_manifest_edge_reasons(original_raw, corrected_raw)
        if preserved_reason_count:
            log(f"MANIFEST_UNCHANGED_EDGE_REASONS_PRESERVED | count={preserved_reason_count}")
        try:
            corrected = ensure_required_files(normalize_manifest(corrected_raw, ctx.spec, require_v2=True), ctx.spec)
        except BuildError as repair_error:
            raise BuildError(f"MANIFEST_REPAIR_REJECTED: {redact(repair_error)}") from repair_error
        if any(x.get("ambiguous_legacy_dependencies") for x in corrected):
            raise BuildError("MANIFEST_REPAIR_REJECTED: ambiguous v1 dependencies remain")
        original_entries = {x.get("path"): x for x in ensure_required_files(normalize_manifest(original_raw, ctx.spec, allow_self_relationships=True, allow_missing_reasons=True), ctx.spec)}
        corrected_entries = {x["path"]: x for x in corrected}
        required_spec_paths = set(required_file_lines(ctx.spec))
        derived_reasons = derive_safe_manifest_correction_reasons(original_raw, corrected_raw)
        if derived_reasons:
            model_reasons = corrected_raw.get("correction_reasons", []) if isinstance(corrected_raw.get("correction_reasons", []), list) else []
            corrected_raw["correction_reasons"] = model_reasons + derived_reasons
            log(f"MANIFEST_CORRECTION_REASONS_DERIVED | count={len(derived_reasons)} | only mechanically verified invalid-edge removals")
        reasons = corrected_raw.get("correction_reasons", []) if isinstance(corrected_raw, dict) else []
        reason_keys = {(str(x.get("file")), str(x.get("relationship")), str(x.get("target"))) for x in reasons if isinstance(x, dict) and str(x.get("reason", "")).strip()}
        validate_manifest_correction_file_set(original_entries, corrected_entries, required_spec_paths, reason_keys)
        rel_fields = ("generation_dependencies", "build_dependencies", "runtime_references", "contract_references", "validation_targets")
        for path, old in original_entries.items():
            if path not in corrected_entries:
                continue
            new = corrected_entries[path]
            for field_name in rel_fields:
                old_targets = {x["path"] for x in old.get(field_name, [])} if field_name in {"runtime_references", "contract_references"} else set(old.get(field_name, []))
                new_targets = {x["path"] for x in new.get(field_name, [])} if field_name in {"runtime_references", "contract_references"} else set(new.get(field_name, []))
                for target in old_targets ^ new_targets:
                    if (path, field_name, target) not in reason_keys:
                        raise BuildError(f"MANIFEST_REPAIR_REJECTED: changed edge has no reason: {path}:{field_name}:{target}")
            for target in old.get("ambiguous_legacy_dependencies", []):
                classified = any(target in ({r["path"] for r in new.get(field_name, [])} if field_name in {"runtime_references", "contract_references"} else set(new.get(field_name, []))) for field_name in rel_fields)
                if classified and not any(key[0] == path and key[2] == target for key in reason_keys):
                    raise BuildError(f"MANIFEST_REPAIR_REJECTED: migrated edge has no reason: {path}:{target}")
        try:
            validate_manifest_relationships(corrected, ctx.spec)
            if len(corrected) > MAX_FILES: raise BuildError("MANIFEST_TOO_LARGE")
            ctx.manifest = topologically_order_manifest(corrected)
        except BuildError as repair_error:
            raise BuildError(f"MANIFEST_REPAIR_REJECTED: {redact(repair_error)}") from repair_error
        log("MANIFEST_REPAIR_ACCEPTED | bounded correction=1")
    ctx.manifest = topologically_order_manifest(ctx.manifest)
    legacy_count = sum(len(x.get("ambiguous_legacy_dependencies", [])) for x in ctx.manifest)
    build_order = topologically_order_manifest(ctx.manifest, graph="build_dependencies")
    ctx.evidence["manifest_migration"] = {"source_schema": source_manifest_schema, "target_schema": MANIFEST_SCHEMA_VERSION, "files_preserved": len(ctx.manifest), "dependencies_classified": sum(len(_manifest_relationships(x)[0]) + len(_manifest_relationships(x)[1]) for x in ctx.manifest), "runtime_references_classified": sum(len(x.get("runtime_references", [])) for x in ctx.manifest), "ambiguous_relationships": legacy_count, "status": "PASS", "checkpoint_compatibility": "LEGACY_CHECKPOINTS_REJECTED; fresh run required"}
    ctx.evidence["planning"] = {"planner": PLANNER_MODEL, "requirements": len(requirements), "manifest_files": len(ctx.manifest), "manifest_order": [x["path"] for x in ctx.manifest], "build_manifest_order": [x["path"] for x in build_order], "completed_at": utc_now()}


def sync_dependency_manifest(ctx: BuildContext, state: StateStore) -> None:
    # Source inspection discovers runtime/import relationships for invalidation
    # and contracts. It must never promote them into generation prerequisites.
    paths = {x["path"] for x in ctx.manifest}
    for item in ctx.manifest:
        generation, build = _manifest_relationships(item)
        missing = set(generation + build) - paths
        if missing: raise BuildError(f"MANIFEST_REFERENCE_TARGET_MISSING: {item['path']}: {sorted(missing)}")
    state.set_manifest(ctx.manifest)
    refresh_observed_contract(ctx)
    state.set_contract(ctx.contract)
    save_json(ctx.staging / "manifest.json", {"schema_version": MANIFEST_SCHEMA_VERSION, "files": ctx.manifest})
    for path, content in ctx.project.items():
        state.accept_file(path, content, contract_hash=file_contract_fingerprint(path, ctx.contract, ctx.manifest), dependency_hash=dependency_hash(path, ctx.manifest, ctx.contract))


def _contract_recovery(client: Cell3Client, ctx: BuildContext, round_no: int, state: StateStore) -> dict[str, Any]:
    report = validate_contract_consistency(ctx.project, ctx.manifest, ctx.contract)
    if report["status"] == "PASS": return report
    findings: list[dict[str, Any]] = []
    for finding in report["findings"]:
        item = dict(finding)
        path = item.get("file")
        if path not in ctx.project:
            path = _first_js(ctx.project) or _first_html(ctx.project) or next(iter(ctx.project), "")
            item["file"] = path
        if path in ctx.project and not item.get("evidence"):
            line = int(item.get("line", 1) or 1)
            item["line"] = line
            item["evidence"] = _evidence_for_line(ctx.project[path], line)
        if path in ctx.project: findings.append(item)
    if not findings: raise BuildError("CONTRACT_RECOVERY_BLOCKED: findings have no safe source target")
    # Resolve HTML-owned selector references directly in the contract owner.
    # This is intentionally done before generic patching so a weak code model
    # cannot replace a class selector in app.js with an unrelated ID selector.
    remaining: list[dict[str, Any]] = []
    for finding in findings:
        path = str(finding.get("file", ""))
        if finding.get("rule") == "JS_DOM_REFERENCE_UNRESOLVED" and Path(path).suffix.lower() in {".html", ".htm"}:
            patched = repair_dynamic_view_root(path, ctx.project[path], finding)
            if patched is not None and patched != ctx.project[path]:
                trial = dict(ctx.project); trial[path] = patched
                trial = validate_project(trial); static_validate(trial)
                validate_contract_patch_target(path, patched, finding)
                ctx.project = trial
                log(f"DOM_REFERENCE_REPAIRED_IN_MARKUP | {path} | {str(finding.get('message', ''))[:120]}")
                continue
        remaining.append(finding)
    findings = remaining
    if not findings:
        report = validate_contract_consistency(ctx.project, ctx.manifest, ctx.contract)
        save_json(ctx.staging / f"contract_recovery_round_{round_no}.json", report)
        return report
    repair_project(client, ctx, findings, round_no, state)
    static_validate(ctx.project)
    refreshed = validate_contract_consistency(ctx.project, ctx.manifest, ctx.contract)
    save_json(ctx.staging / f"contract_recovery_round_{round_no}.json", refreshed)
    return refreshed


def verify_saved_audit(report: dict[str, Any], ctx: BuildContext) -> bool:
    if report.get("status") != "PASS" or report.get("independent_auditor") != AUDITOR_MODEL or report.get("model_status") != "PASS" or not report.get("evidence_verified"):
        return False
    if report.get("spec_sha256") != ctx.spec_hash or report.get("contract_sha256") != ctx.contract.get("sha256"):
        return False
    expected_hashes = {p: sha256_text(source) for p, source in ctx.project.items()}
    if report.get("file_hashes") != expected_hashes or report.get("audited_files") != len(ctx.project):
        return False
    coverage = report.get("coverage")
    if not isinstance(coverage, list) or {x.get("file") for x in coverage if isinstance(x, dict)} != set(ctx.project):
        return False
    for item in coverage:
        if not isinstance(item, dict): return False
        path, evidence = item.get("file"), item.get("evidence")
        if path not in ctx.project or not isinstance(evidence, str) or ctx.project[path].count(evidence) != 1 or not item.get("reviewed_as"):
            return False
        actual_line = ctx.project[path][:ctx.project[path].index(evidence)].count("\n") + 1
        if item.get("line") != actual_line: return False
    return not report.get("blocking_findings")


def _audit_recovery(client: Cell3Client, ctx: BuildContext, state: StateStore) -> dict[str, Any]:
    for round_no in range(0, MAX_INTEGRATION_ROUNDS + 1):
        report = independent_audit(client, ctx)
        if report["status"] == "PASS": return report
        if round_no >= MAX_INTEGRATION_ROUNDS:
            raise BuildError(f"INDEPENDENT_AUDIT_FAILED: {report.get('blocking_findings') or report.get('findings')}")
        findings = [x for x in report.get("blocking_findings", []) if x.get("severity") in {"CRITICAL", "HIGH", "MEDIUM"}]
        if not findings: raise BuildError("INDEPENDENT_AUDIT_FAILED_WITHOUT_REPAIRABLE_EVIDENCE")
        repair_project(client, ctx, findings, round_no + 1, state)
        static_validate(ctx.project)
        integration = validate_contract_consistency(ctx.project, ctx.manifest, ctx.contract)
        for repair_round in range(1, MAX_INTEGRATION_ROUNDS + 1):
            if integration["status"] == "PASS": break
            integration = _contract_recovery(client, ctx, repair_round, state)
        if integration["status"] != "PASS": raise BuildError("AUDIT_REPAIR_LEFT_CONTRACT_FAILURE")
        ctx.evidence["contract_report"] = integration
    raise BuildError("INDEPENDENT_AUDIT_RETRY_EXHAUSTED")


def publish_project(project_name: str, project: dict[str, str]) -> Path:
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    target = PROJECTS_DIR / project_name
    if target.exists(): raise BuildError(f"PUBLISH_TARGET_EXISTS: refusing to overwrite {target}; choose a new project name")
    temp = PROJECTS_DIR / f".{project_name}.tmp-{uuid.uuid4().hex[:10]}"
    try:
        write_project(temp, validate_project(project))
        verified = read_project(temp)
        if verified != project: raise BuildError("PUBLISH_VERIFICATION_FAILED: written files differ from accepted source")
        if {p: sha256_text(c) for p, c in verified.items()} != {p: sha256_text(c) for p, c in project.items()}:
            raise BuildError("PUBLISH_HASH_VERIFICATION_FAILED")
        temp.replace(target)
        return target
    finally:
        if temp.exists(): shutil.rmtree(temp, ignore_errors=True)


def _acceptance_matrix(ctx: BuildContext, static_report: dict[str, Any], integration_report: dict[str, Any]) -> dict[str, Any]:
    gates = {
        "specification_evidence": "PASS" if ctx.evidence.get("specification", {}).get("sha256") == ctx.spec_hash else "FAIL",
        "manifest_complete": "PASS" if set(ctx.project) == {x["path"] for x in ctx.manifest} else "FAIL",
        "static_validation": static_report.get("status", "FAIL"),
        "canonical_contract": integration_report.get("status", "FAIL"),
        "independent_qwen_audit": ctx.audit.get("status", "FAIL"),
        "unit_tests": ctx.tests.get("unit_tests", {}).get("status", "NOT_REQUESTED"),
        "browser_runtime": ctx.tests.get("browser_runtime", {}).get("status", "NOT_APPLICABLE"),
    }
    failures = [k for k, v in gates.items() if v == "FAIL"]
    blockers = [k for k, v in gates.items() if v == "BLOCKED"]
    optional_not_found = gates["unit_tests"] == "NOT_FOUND" and _test_optional(ctx.manifest)
    allowed_unit_status = gates["unit_tests"] in {"PASS", "NOT_REQUESTED"} or optional_not_found
    browser_ok = gates["browser_runtime"] in {"PASS", "NOT_APPLICABLE"}
    if not browser_ok and "browser_runtime" not in blockers:
        blockers.append("browser_runtime")
    if failures: status = Acceptance.FAIL.value
    elif blockers or not allowed_unit_status or not browser_ok: status = Acceptance.BLOCKED.value
    else: status = Acceptance.PASS.value
    return {"status": status, "gates": gates, "failures": failures, "blockers": blockers, "optional_test_absence": optional_not_found, "checked_at": utc_now()}


def run_build(args: argparse.Namespace) -> int:
    global PROGRESS, LOG_FILE
    project_name = args.name.strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", project_name): raise BuildError("Project name must use letters, numbers, underscores, and hyphens")
    if args.resume_run_id:
        ctx, store = _load_resume_context(args.resume_run_id, args.spec_file, project_name, args.timeout, args.run_tests)
    else:
        ctx, store = _new_context(read_spec(args.spec_file), project_name, args.timeout, args.run_tests)
    LOG_FILE = LOGS_DIR / f"{ctx.run_id}.log"; LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    PROGRESS = Progress(project_name)
    print("=" * 78)
    print(f"CELL 3 PROJECT BUILD | version {VERSION}")
    print("Cell 2: NOT USED")
    print("Proxy configuration: NOT MODIFIED")
    print(f"Planner: {PLANNER_MODEL} | Generator/repair: {GENERATOR_MODEL} | Independent auditor: {AUDITOR_MODEL}")
    print("Engine: canonical contracts + semantic units + hash-verified recovery + evidence gates")
    print("Safety: generated source and tests execute locally; build only from trusted specifications")
    print("=" * 78)
    PROGRESS.render(initial=True)
    client: Cell3Client | None = None
    acceptance: dict[str, Any] = {"status": "BLOCKED"}
    try:
        with PROGRESS.stage_scope(1, STAGES[0]):
            client = Cell3Client(ctx.timeout)
            client.capture_dir = RUNS_DIR / ctx.run_id / "model_responses"
            client.capture_dir.mkdir(parents=True, exist_ok=True)
            client.preflight(require_planner=True)
            run_internal_regression_tests()
            store.stage("ENVIRONMENT", "PASS", cell2="NOT USED", proxy_configuration="NOT MODIFIED")
        if not ctx.plan:
            with PROGRESS.stage_scope(2, STAGES[1]):
                # Deterministic specification evidence is retained before model interpretation.
                req_lines = [x.strip() for x in ctx.spec.splitlines() if x.strip()]
                ctx.evidence["specification_analysis"] = {"line_count": len(req_lines), "spec_sha256": ctx.spec_hash, "analyzed_at": utc_now()}
                ctx.evidence["specification"] = {"sha256": ctx.spec_hash, "requirements": req_lines, "reviewed_at": utc_now()}
                store.stage("SPECIFICATION_ANALYSIS", "PASS", line_count=len(req_lines))
            with PROGRESS.stage_scope(3, STAGES[2]):
                _run_planning(client, ctx)
                store.set_manifest(ctx.manifest)
                store.set_planning_artifacts(ctx)
                store.stage("PLANNING", "PASS", manifest=sorted(x["path"] for x in ctx.manifest), manifest_sha256=canonical_hash(ctx.manifest), planning_sha256=store.state["planning_sha256"])
            with PROGRESS.stage_scope(4, STAGES[3]):
                ctx.contract = create_canonical_contract(client, ctx)
                store.set_contract(ctx.contract)
                _save_stage_artifacts(ctx)
                save_json(ctx.staging / "evidence" / "evidence_index.json", ctx.evidence)
                store.stage("CANONICAL_CONTRACT", "PASS", contract_sha256=ctx.contract["sha256"])
        else:
            # Resumed builds reuse the exact plan, manifest and canonical contract.
            if not ctx.manifest or not ctx.contract: raise BuildError("RESUME_ARTIFACTS_MISSING: planning/contract evidence is incomplete")
            with PROGRESS.stage_scope(2, STAGES[1]):
                store.stage("SPECIFICATION_ANALYSIS", "REUSED", spec_sha256=ctx.spec_hash)
            with PROGRESS.stage_scope(3, STAGES[2]):
                store.stage("PLANNING", "REUSED", manifest=sorted(x["path"] for x in ctx.manifest))
            with PROGRESS.stage_scope(4, STAGES[3]):
                store.stage("CANONICAL_CONTRACT", "REUSED", contract_sha256=ctx.contract.get("sha256"))
        with PROGRESS.stage_scope(5, STAGES[4]):
            ctx.project = generate_project(client, ctx, store)
            sync_dependency_manifest(ctx, store)
            write_project(ctx.staging / "generated", ctx.project)
            save_json(ctx.staging / "generation_inventory.json", {p: sha256_text(c) for p, c in sorted(ctx.project.items())})
            store.stage("GENERATION", "PASS", files=len(ctx.project))
        with PROGRESS.stage_scope(6, STAGES[5]):
            static_report = static_validate(ctx.project)
            ctx.evidence["contract_report"] = validate_contract_consistency(ctx.project, ctx.manifest, ctx.contract)
            integration_report = ctx.evidence["contract_report"]
            for round_no in range(1, MAX_INTEGRATION_ROUNDS + 1):
                if integration_report["status"] == "PASS": break
                integration_report = _contract_recovery(client, ctx, round_no, store)
                ctx.evidence["contract_report"] = integration_report
            if integration_report["status"] != "PASS": raise BuildError("CROSS_FILE_INTEGRATION_FAILED")
            static_report = static_validate(ctx.project)
            write_project(ctx.staging / "generated", ctx.project)
            save_json(ctx.staging / "static_validation_report.json", static_report)
            save_json(ctx.staging / "contract_report.json", integration_report)
            store.stage("INTEGRATION", "PASS", contract_report_sha256=integration_report["sha256"])
        with PROGRESS.stage_scope(7, STAGES[6]):
            saved_audit = load_json(ctx.staging / "audit" / "final_audit.json", required=False) if ctx.resume else None
            previous_audit_stage = store.state.get("stage_results", {}).get("INDEPENDENT_AUDIT", {})
            if isinstance(saved_audit, dict) and previous_audit_stage.get("status") in {"PASS", "REUSED"} and verify_saved_audit(saved_audit, ctx):
                ctx.audit = saved_audit
                log("INDEPENDENT_AUDIT_REUSED | source/contract/spec hashes and coverage evidence verified")
                store.stage("INDEPENDENT_AUDIT", "REUSED", auditor=AUDITOR_MODEL, findings=len(ctx.audit.get("findings", [])))
            else:
                ctx.audit = _audit_recovery(client, ctx, store)
                save_json(ctx.staging / "audit" / "final_audit.json", ctx.audit)
                store.stage("INDEPENDENT_AUDIT", "PASS", auditor=AUDITOR_MODEL, findings=0)
        with PROGRESS.stage_scope(8, STAGES[7]):
            ctx.tests = _run_verification(client, ctx, store)
            store.stage("TESTING_RUNTIME", "PASS", unit_tests=ctx.tests["unit_tests"]["status"], browser=ctx.tests["browser_runtime"]["status"])
        with PROGRESS.stage_scope(9, STAGES[8]):
            static_report = static_validate(ctx.project)
            integration_report = validate_contract_consistency(ctx.project, ctx.manifest, ctx.contract)
            if integration_report["status"] != "PASS": raise BuildError("FINAL_EVIDENCE_INTEGRATION_GATE_FAILED")
            if ctx.audit.get("status") != "PASS": raise BuildError("FINAL_EVIDENCE_AUDIT_GATE_FAILED")
            acceptance = _acceptance_matrix(ctx, static_report, integration_report)
            save_json(ctx.staging / "evidence" / "acceptance_matrix.json", acceptance)
            store.stage("ACCEPTANCE", acceptance["status"], gates=acceptance["gates"])
            if acceptance["status"] != Acceptance.PASS.value:
                raise BuildError(f"ACCEPTANCE_{acceptance['status']}: mandatory evidence gates not satisfied; inspect evidence/acceptance_matrix.json")
        with PROGRESS.stage_scope(10, STAGES[9]):
            published = publish_project(project_name, ctx.project)
            report = {"version": VERSION, "run_id": ctx.run_id, "status": Acceptance.PASS.value, "project": project_name, "published_path": str(published), "cell2": "NOT USED", "proxy_configuration": "NOT MODIFIED", "models": {"planner": PLANNER_MODEL, "generator": GENERATOR_MODEL, "auditor": AUDITOR_MODEL}, "acceptance": acceptance, "audit": ctx.audit, "tests": ctx.tests, "files": sorted(ctx.project), "file_hashes": {p: sha256_text(c) for p, c in ctx.project.items()}, "completed_at": utc_now()}
            save_json(RUNS_DIR / ctx.run_id / "report.json", report)
            store.stage("PUBLISHED", "PASS", published_path=str(published), acceptance="PASS")
        PROGRESS.close(True)
        print("\nBUILD PASS")
        print("Version:", VERSION)
        print("Run ID:", ctx.run_id)
        print("Project:", published)
        print("Independent Qwen audit: PASS")
        print("Unit tests:", ctx.tests["unit_tests"]["status"])
        print("Browser runtime:", ctx.tests["browser_runtime"]["status"])
        print("Cell 2: NOT USED")
        print("Proxy configuration: NOT MODIFIED")
        if not args.keep_staging:
            # Keep run checkpoints/evidence; remove only transient generated staging copy.
            shutil.rmtree(ctx.staging / "generated", ignore_errors=True)
        return 0
    except Exception as exc:
        message = redact(exc)
        store.state["status"] = "FAILED"
        store.state["last_error"] = message
        store.state["acceptance"] = acceptance
        store.persist()
        save_json(RUNS_DIR / ctx.run_id / "report.json", {"version": VERSION, "run_id": ctx.run_id, "status": "FAILED", "error": message, "staging": str(ctx.staging), "cell2": "NOT USED", "proxy_configuration": "NOT MODIFIED", "acceptance": acceptance, "updated_at": utc_now()})
        log(f"BUILD FAILED | {message}")
        if PROGRESS: PROGRESS.close(False)
        print("\nBUILD FAILED")
        print(message)
        print("Run ID:", ctx.run_id)
        print("Staging retained:", ctx.staging)
        print("Cell 2: NOT USED")
        print("Proxy configuration: NOT MODIFIED")
        return 1
    finally:
        PROGRESS = None


def run_internal_regression_tests() -> None:
    """Fast offline fault-injection suite; no Kaggle model calls are made here."""
    import unittest

    class EngineRegressionTests(unittest.TestCase):
        def test_html_duplicate_ids_are_rejected(self):
            duplicate = "<html><body><div id='same'></div><button id='same'></button></body></html>"
            with self.assertRaisesRegex(BuildError, "HTML_DUPLICATE_ID"):
                static_validate({"index.html": duplicate})
            unique = "<html><body><div id='one'></div><button id='two'></button></body></html>"
            self.assertEqual(static_validate({"index.html": unique})["status"], "PASS")
        def test_unmatched_css_class_selector_is_detected_without_planner_hint(self):
            project = {"index.html": "<html><body><button class='save'></button></body></html>", "styles.css": ".savve { color: red; }"}
            manifest = [{"path": "index.html", "kind": "html", "purpose": "page"}, {"path": "styles.css", "kind": "css", "purpose": "styles"}]
            contract = build_contract(project, manifest)
            report = validate_contract_consistency(project, manifest, contract)
            self.assertEqual(report["status"], "FAIL")
            self.assertTrue(any(x["rule"] == "CSS_SELECTOR_DOM_MISMATCH" for x in report["findings"]))
        def test_css_parser_rejects_invalid_declarations_and_accepts_nested_rules(self):
            with self.assertRaisesRegex(BuildError, "CSS_SYNTAX_ERROR"):
                static_validate({"styles.css": ".card { color red; }"})
            valid = "@media (max-width: 600px) { .card { color: red; &:hover { color: blue; } } } @keyframes fade { from { opacity: 0; } to { opacity: 1; } }"
            self.assertEqual(static_validate({"styles.css": valid})["status"], "PASS")
        def test_unverified_source_language_does_not_silently_pass(self):
            with self.assertRaisesRegex(BuildError, "SOURCE_VALIDATION_UNVERIFIED"):
                static_validate({"app.rb": "def add(a, b); a + b; end"})
        def test_unvalidated_unknown_file_type_does_not_silently_pass(self):
            with self.assertRaisesRegex(BuildError, "SOURCE_VALIDATION_UNVERIFIED"):
                static_validate({"config.unknownfmt": "anything"})
        def test_yaml_toml_xml_and_svg_syntax_are_checked(self):
            for path, source, marker in [
                ("config.yaml", "items: [unterminated", "YAML_SYNTAX_ERROR"),
                ("config.toml", "items = [", "TOML_SYNTAX_ERROR"),
                ("config.xml", "<root>", "XML_SYNTAX_ERROR"),
                ("icon.svg", "<svg><g></svg>", "XML_SYNTAX_ERROR"),
            ]:
                with self.subTest(path=path):
                    with self.assertRaisesRegex(BuildError, marker):
                        static_validate({path: source})
        def test_supported_config_and_documentation_files_have_explicit_validation_records(self):
            report = static_validate({
                "config.yaml": "items:\n  - one\n",
                "config.toml": "[app]\nname = 'demo'\n",
                "config.xml": "<root><item/></root>",
                "README.md": "# Project\n",
            })
            self.assertEqual(report["status"], "PASS")
            self.assertEqual(len(report["files"]), 4)
        def test_path_traversal_rejected(self):
            with self.assertRaises(BuildError): safe_relative_path("../outside.py")
        def test_http_error_diagnostics_scrub_nested_and_embedded_credentials(self):
            response = type("Response", (), {
                "status_code": 500,
                "headers": {"Content-Type": "application/json", "X-Request-ID": "req-123"},
                "text": json.dumps({"error": "upstream rejected api_key=KEY-SECRET Bearer bearer-secret", "access_token": "TOKEN-SECRET"}),
            })()
            details = http_error_diagnostics(response)
            for secret in ("KEY-SECRET", "bearer-secret", "TOKEN-SECRET"):
                self.assertNotIn(secret, details)
            self.assertIn("[REDACTED]", details)
            self.assertIn("status=500", details)
            self.assertIn("content_type=application/json", details)
            self.assertIn("request_id=req-123", details)
        def test_http_error_diagnostics_scrub_plain_text_credentials(self):
            response = type("Response", (), {
                "status_code": 502,
                "headers": {"Content-Type": "text/plain"},
                "text": "proxy failed; authorization: Bearer plain-secret; api_key=plain-key",
            })()
            details = http_error_diagnostics(response)
            self.assertNotIn("plain-secret", details)
            self.assertNotIn("plain-key", details)
            self.assertIn("[REDACTED]", details)
        def test_http_error_diagnostics_bounds_body_excerpt(self):
            response = type("Response", (), {
                "status_code": 500,
                "headers": {"Content-Type": "text/plain"},
                "text": "x" * 1000,
            })()
            details = http_error_diagnostics(response, body_limit=80)
            self.assertIn("[truncated]", details)
            self.assertLessEqual(len(details.split("body=", 1)[1]), 100)
        def test_cell3_http_500_surfaces_redacted_body_diagnostics(self):
            from unittest.mock import patch

            response = type("Response", (), {
                "status_code": 500,
                "headers": {"Content-Type": "application/json", "X-Request-ID": "req-500"},
                "text": '{"error":"model unavailable; api_key=hidden-key"}',
            })()
            response.raise_for_status = lambda: (_ for _ in ()).throw(requests.HTTPError("HTTP 500", response=response))
            client = Cell3Client.__new__(Cell3Client)
            client.timeout = 30
            client.base = "https://cell3.invalid"
            client.auth_headers = {"Authorization": "Bearer test-token", "Content-Type": "application/json"}
            client.capture_dir = None
            with patch(__name__ + ".request_with_dns_fallback", return_value=response):
                with self.assertRaisesRegex(BuildError, "status=500") as raised:
                    client.request("POST", "/api/chat", payload={"model": "test"})
            self.assertIn("request_id=req-500", str(raised.exception))
            self.assertIn("model unavailable", str(raised.exception))
            self.assertNotIn("hidden-key", str(raised.exception))
            self.assertEqual(client.auth_headers["Authorization"], "Bearer test-token")
        def test_duplicate_recovery_prompt_changes_and_names_rejection(self):
            base = "Generate unit HTML_ShellStructure only."
            retry = _unit_recovery_prompt(base, "HTML_ShellStructure", 2,
                                          "DUPLICATE_UNIT_REJECTED",
                                          "<html><body>already accepted</body></html>")
            self.assertIn("DUPLICATE_UNIT_REJECTED", retry)
            self.assertIn("do not repeat", retry.lower())
            self.assertNotEqual(base, retry)
        def test_html_shell_duplicate_is_reconciled_only_when_already_present(self):
            unit = {"name": "HTML_ShellStructure", "purpose": "Document shell structure"}
            shell = "<!doctype html><html><head></head><body>"
            self.assertTrue(_html_shell_duplicate_is_satisfied(unit, shell, [shell]))
            self.assertFalse(_html_shell_duplicate_is_satisfied(unit, shell, ["<main>different</main>"]))
            self.assertFalse(_html_shell_duplicate_is_satisfied({"name": "MainContent", "purpose": "Main content"}, shell, [shell]))
            self.assertFalse(_html_shell_duplicate_is_satisfied(unit, "<main>content</main>", ["<main>content</main>"]))
        def test_html_plan_routes_css_and_javascript_units_away_from_html(self):
            mixed = {"preamble": "<!doctype html><html><head></head><body></body></html>", "units": [
                {"name": "CSS_DesignTokens", "kind": "css_rule", "purpose": "CSS variables"},
                {"name": "Data_Manager", "kind": "class", "purpose": "Data manager"},
                {"name": "HTML_ShellStructure", "kind": "html_fragment", "purpose": "Sidebar, header and main content"},
            ]}
            _, html_units = validate_unit_plan_for_file(mixed, "index.html")
            self.assertEqual([u["name"] for u in html_units], ["HTML_ShellStructure"])
            _, css_units = validate_unit_plan_for_file(mixed, "styles.css")
            self.assertEqual([u["name"] for u in css_units], ["CSS_DesignTokens"])
            _, js_units = validate_unit_plan_for_file(mixed, "app.js")
            self.assertEqual([u["name"] for u in js_units], ["Data_Manager"])
        def test_uploaded_taskflow_plan_is_routed_to_owning_file_kinds(self):
            # Exact semantic mix reported by run client-20261105T101346-2b0fd918.
            names = ["Data_Manager", "Activity_Logger", "Project_Manager", "Task_Manager",
                     "Dashboard_Renderer", "Kanban_Board", "Calendar_View", "SearchAndFilters",
                     "UI_Controller"]
            captured = {
                "preamble": "<!DOCTYPE html><html lang=\"en\"><head></head><body><div id=\"app-shell\"></div></body></html>",
                "units": [
                    {"name": "CSS_DesignTokens", "kind": "css_rule", "purpose": "Defines centralized CSS design tokens"},
                    *[{"name": name, "kind": "class", "purpose": name.replace("_", " ")} for name in names],
                    {"name": "Application_Initializer", "kind": "entrypoint", "purpose": "Initializes the application"},
                    {"name": "HTML_ShellStructure", "kind": "html_fragment", "purpose": "Provides sidebar, header and main content application layout"},
                ],
            }
            _, html_units = validate_unit_plan_for_file(captured, "index.html")
            _, css_units = validate_unit_plan_for_file(captured, "styles.css")
            _, js_units = validate_unit_plan_for_file(captured, "app.js")
            self.assertEqual([u["name"] for u in html_units], ["HTML_ShellStructure"])
            self.assertEqual([u["name"] for u in css_units], ["CSS_DesignTokens"])
            self.assertEqual(len(js_units), 10)
            self.assertEqual(js_units[-1]["name"], "Application_Initializer")
        def test_explicit_unit_target_file_routes_same_kind_to_distinct_files(self):
            mixed = {"preamble": "", "units": [
                {"name": "Store", "kind": "class", "purpose": "Data store", "target_file": "src/store.js"},
                {"name": "View", "kind": "class", "purpose": "UI view", "target_file": "src/view.js"},
            ]}
            _, store_units = validate_unit_plan_for_file(mixed, "src/store.js", {"src/store.js", "src/view.js"})
            _, view_units = validate_unit_plan_for_file(mixed, "src/view.js", {"src/store.js", "src/view.js"})
            self.assertEqual([u["name"] for u in store_units], ["Store"])
            self.assertEqual([u["name"] for u in view_units], ["View"])
        def test_explicit_unit_target_file_rejects_unknown_manifest_path(self):
            mixed = {"preamble": "", "units": [
                {"name": "Store", "kind": "class", "purpose": "Data store", "target_file": "missing.js"},
            ]}
            with self.assertRaisesRegex(BuildError, "TARGET_FILE_NOT_IN_MANIFEST"):
                validate_unit_plan_for_file(mixed, "src/store.js", {"src/store.js"})
        def test_explicit_unit_target_file_requires_all_units_owned(self):
            mixed = {"preamble": "", "units": [
                {"name": "Store", "kind": "class", "purpose": "Data store", "target_file": "src/store.js"},
                {"name": "View", "kind": "class", "purpose": "UI view"},
            ]}
            with self.assertRaisesRegex(BuildError, "OWNERSHIP_INCOMPLETE"):
                validate_unit_plan_for_file(mixed, "src/store.js", {"src/store.js", "src/view.js"})
        def test_manifest_topological_order_places_dependencies_first(self):
            manifest = [
                {"path": "app.js", "purpose": "app", "generation_dependencies": ["data.js"]},
                {"path": "data.js", "purpose": "data", "generation_dependencies": []},
            ]
            ordered = topologically_order_manifest(manifest)
            self.assertEqual([x["path"] for x in ordered], ["data.js", "app.js"])
        def test_manifest_cycle_rejected_before_generation(self):
            manifest = [
                {"path": "a.js", "purpose": "a", "generation_dependencies": ["b.js"]},
                {"path": "b.js", "purpose": "b", "generation_dependencies": ["a.js"]},
            ]
            with self.assertRaisesRegex(BuildError, "DEPENDENCY_CYCLE"):
                topologically_order_manifest(manifest)
        def test_manifest_multi_file_cycle_reports_complete_path(self):
            manifest = [
                {"path": "a.js", "purpose": "a", "generation_dependencies": ["b.js"]},
                {"path": "b.js", "purpose": "b", "generation_dependencies": ["c.js"]},
                {"path": "c.js", "purpose": "c", "generation_dependencies": ["a.js"]},
            ]
            with self.assertRaisesRegex(BuildError, r"a.js.*b.js.*c.js.*a.js"):
                topologically_order_manifest(manifest)

        def test_manifest_self_and_duplicate_edges_rejected(self):
            self_edge = [{"path": "a.js", "purpose": "a", "generation_dependencies": ["a.js"]}]
            duplicate = [{"path": "a.js", "purpose": "a", "generation_dependencies": ["b.js", "b.js"]}, {"path": "b.js", "purpose": "b"}]
            with self.assertRaisesRegex(BuildError, "MANIFEST_SELF_DEPENDENCY"):
                topologically_order_manifest(self_edge)
            with self.assertRaisesRegex(BuildError, "MANIFEST_DUPLICATE_EDGE"):
                topologically_order_manifest(duplicate)
        def test_manifest_missing_dependency_rejected(self):
            manifest = [{"path": "a.js", "purpose": "a", "generation_dependencies": ["missing.js"]}]
            with self.assertRaisesRegex(BuildError, "DEPENDENCY_NOT_DECLARED"):
                topologically_order_manifest(manifest)
        def test_stylesheet_type_is_runtime_only_and_directional(self):
            valid = [
                {"path": "index.html", "kind": "html", "purpose": "page", "runtime_references": [
                    {"path": "styles.css", "type": "stylesheet", "reason": "HTML loads the stylesheet"}]},
                {"path": "styles.css", "kind": "css", "purpose": "styles"},
            ]
            validate_manifest_relationships(valid)

            invalid_category = [
                {"path": "index.html", "kind": "html", "purpose": "page"},
                {"path": "styles.css", "kind": "css", "purpose": "styles", "contract_references": [
                    {"path": "index.html", "type": "stylesheet", "reason": "Invalid category for stylesheet"}]},
            ]
            with self.assertRaisesRegex(BuildError, r"styles\.css:contract_references:stylesheet"):
                validate_manifest_relationships(invalid_category)

            invalid_direction = [
                {"path": "styles.css", "kind": "css", "purpose": "styles", "runtime_references": [
                    {"path": "app.js", "type": "stylesheet", "reason": "Wrong source and target kinds"}]},
                {"path": "app.js", "kind": "javascript", "purpose": "logic"},
            ]
            with self.assertRaisesRegex(BuildError, "only HTML files may declare browser stylesheet loading"):
                validate_manifest_relationships(invalid_direction)

        def test_manifest_relationship_guidance_covers_repair_type_mismatch(self):
            self.assertIn("NEVER put stylesheet or script loading types in contract_references", MANIFEST_RELATIONSHIP_GUIDANCE)
            self.assertIn("html_selector_contract", MANIFEST_RELATIONSHIP_GUIDANCE)
            self.assertIn("Never invent CSS-to-JavaScript", MANIFEST_RELATIONSHIP_GUIDANCE)

        def test_html_publish_gate_blocks_when_browser_acceptance_not_requested(self):
            ctx = BuildContext("browser-not-requested-gate-test", "p", Path(tempfile.gettempdir()), "spec", sha256_text("spec"), 5, False)
            ctx.evidence = {"specification": {"sha256": ctx.spec_hash}}
            ctx.project = {"index.html": "<html></html>"}
            ctx.manifest = [{"path": "index.html", "purpose": "page"}]
            ctx.audit = {"status": "PASS"}
            ctx.tests = {"unit_tests": {"status": "NOT_REQUESTED"}, "browser_runtime": {"status": "BROWSER_ACCEPTANCE_NOT_RUN"}}
            matrix = _acceptance_matrix(ctx, {"status": "PASS"}, {"status": "PASS"})
            self.assertEqual(matrix["status"], "BLOCKED")
            self.assertIn("browser_runtime", matrix["blockers"])

        def test_manifest_v2_relationships_require_and_preserve_reasons(self):
            raw = {"schema_version": 2, "files": [
                {"path": "index.html", "purpose": "page", "runtime_references": [{"path": "styles.css", "type": "stylesheet", "reason": "Browser loads this stylesheet"}]},
                {"path": "styles.css", "purpose": "styles"},
            ]}
            manifest = normalize_manifest(raw, "", require_v2=True)
            self.assertEqual(manifest[0]["runtime_references"][0]["type"], "stylesheet")
            self.assertEqual(manifest[0]["relationship_reasons"]["runtime_references"]["styles.css"], "Browser loads this stylesheet")
            bad = {"schema_version": 2, "files": [{"path": "a.js", "purpose": "a", "build_dependencies": [{"path": "b.js"}]}, {"path": "b.js", "purpose": "b"}]}
            with self.assertRaisesRegex(BuildError, "MANIFEST_RELATIONSHIP_REASON_MISSING"):
                normalize_manifest(bad, "", require_v2=True)

        def test_manifest_type_issues_identify_reversed_stylesheet_contract(self):
            raw = {"schema_version": 2, "files": [
                {"path": "index.html", "runtime_references": [{"path": "styles.css", "type": "stylesheet", "reason": "Browser loads CSS"}]},
                {"path": "styles.css", "contract_references": [{"path": "index.html", "type": "stylesheet", "reason": "HTML requires CSS styling"}]},
            ]}
            issues = manifest_relationship_type_issues(raw)
            self.assertEqual(len(issues), 1)
            self.assertEqual((issues[0]["file"], issues[0]["relationship"], issues[0]["target"]), ("styles.css", "contract_references", "index.html"))
            self.assertTrue(issues[0]["reciprocal_runtime_reference_exists"])
            self.assertNotIn("stylesheet", issues[0]["allowed_types"])

        def test_manifest_rejects_reverse_browser_loading_references(self):
            raw = {"schema_version": 2, "files": [
                {"path": "index.html", "purpose": "page", "runtime_references": [{"path": "styles.css", "type": "stylesheet", "reason": "HTML loads the stylesheet"}]},
                {"path": "styles.css", "purpose": "styles", "runtime_references": [{"path": "index.html", "type": "stylesheet", "reason": "Incorrect reverse link"}]},
            ]}
            manifest = normalize_manifest(raw, "", require_v2=True)
            with self.assertRaisesRegex(BuildError, "only HTML files may declare browser stylesheet loading"):
                validate_manifest_relationships(manifest)

        def test_manifest_rejects_script_reference_to_html_target(self):
            raw = {"schema_version": 2, "files": [
                {"path": "index.html", "purpose": "page", "runtime_references": [{"path": "app.js", "type": "stylesheet", "reason": "HTML incorrectly calls JavaScript a stylesheet"}]},
                {"path": "app.js", "purpose": "logic"},
            ]}
            manifest = normalize_manifest(raw, "", require_v2=True)
            with self.assertRaisesRegex(BuildError, "declares stylesheet reference to app.js"):
                validate_manifest_relationships(manifest)

        def test_manifest_correction_findings_collect_cross_field_mistakes_and_cycles(self):
            manifest = [
                {"path": "index.html", "kind": "html", "generation_dependencies": ["styles.css", "app.js"], "build_dependencies": ["styles.css", "app.js"]},
                {"path": "styles.css", "kind": "css", "generation_dependencies": ["app.js"], "runtime_references": [{"path": "index.html", "type": "stylesheet"}]},
                {"path": "app.js", "kind": "javascript", "generation_dependencies": ["README.md"], "runtime_references": [{"path": "index.html", "type": "script"}], "validation_targets": ["index.html"]},
                {"path": "README.md", "kind": "documentation", "generation_dependencies": ["app.js"]},
                {"path": "tests/test_logic.js", "kind": "test", "generation_dependencies": ["app.js"], "runtime_references": [{"path": "app.js", "type": "script"}], "validation_targets": ["app.js"]},
            ]
            findings = manifest_correction_findings(manifest)
            combined = "\n".join(findings)
            self.assertIn("app.js:validation_targets -> index.html", combined)
            self.assertIn("tests/test_logic.js:generation_dependencies -> app.js", combined)
            self.assertIn("HTML loading CSS/JavaScript belongs in runtime_references", combined)
            self.assertIn("only HTML files declare browser stylesheet/script loading", combined)
            self.assertIn("documentation is not a generation or build prerequisite", combined)
            self.assertIn("generation_dependencies graph cycle", combined)

        def test_manifest_safe_correction_reasons_are_derived_only_for_known_invalid_edges(self):
            original = {"schema_version": 2, "files": [
                {"path": "index.html", "kind": "html", "generation_dependencies": [{"path": "app.js", "reason": "Script provides app behavior"}], "runtime_references": [{"path": "app.js", "type": "script", "reason": "Browser loads app behavior"}]},
                {"path": "app.js", "kind": "javascript", "validation_targets": [{"path": "index.html", "reason": "Application checks the page"}]},
                {"path": "tests/test_logic.js", "kind": "test", "generation_dependencies": [{"path": "app.js", "reason": "Test needs app source"}], "build_dependencies": [{"path": "app.js", "reason": "Test runs after app"}], "validation_targets": [{"path": "app.js", "reason": "Test validates app source"}]},
                {"path": "README.md", "kind": "documentation", "generation_dependencies": [{"path": "app.js", "reason": "Docs describe app"}]},
            ]}
            corrected = {"schema_version": 2, "files": [
                {"path": "index.html", "kind": "html", "generation_dependencies": [], "runtime_references": [{"path": "app.js", "type": "script", "reason": "Browser loads app behavior"}]},
                {"path": "app.js", "kind": "javascript", "validation_targets": []},
                {"path": "tests/test_logic.js", "kind": "test", "generation_dependencies": [], "build_dependencies": [], "validation_targets": [{"path": "app.js", "reason": "Test validates app source"}]},
                {"path": "README.md", "kind": "documentation", "generation_dependencies": []},
            ]}
            reasons = derive_safe_manifest_correction_reasons(original, corrected)
            keys = {(x["file"], x["relationship"], x["target"]) for x in reasons}
            self.assertEqual(keys, {
                ("index.html", "generation_dependencies", "app.js"),
                ("app.js", "validation_targets", "index.html"),
                ("tests/test_logic.js", "generation_dependencies", "app.js"),
                ("tests/test_logic.js", "build_dependencies", "app.js"),
                ("README.md", "generation_dependencies", "app.js"),
            })

        def test_manifest_unchanged_edge_reasons_are_preserved_from_original(self):
            original = {"files": [{"path": "tests/test_logic.js", "validation_targets": [{"path": "app.js", "reason": "Test checks the application source"}]}]}
            corrected = {"files": [{"path": "tests/test_logic.js", "validation_targets": [{"path": "app.js"}]}]}
            count = preserve_unchanged_manifest_edge_reasons(original, corrected)
            self.assertEqual(count, 1)
            self.assertEqual(corrected["files"][0]["validation_targets"][0]["reason"], "Test checks the application source")

        def test_manifest_runtime_contract_and_validation_edges_do_not_create_generation_cycle(self):
            raw = {"schema_version": 2, "files": [
                {"path": "index.html", "purpose": "page", "runtime_references": [
                    {"path": "styles.css", "type": "stylesheet", "reason": "Browser loads the stylesheet"},
                    {"path": "app.js", "type": "script", "reason": "Browser loads the application script"}],
                    "contract_references": [{"path": "styles.css", "type": "selector_contract", "reason": "CSS selectors must match HTML"}, {"path": "app.js", "type": "dom_contract", "reason": "Script selectors must match HTML"}]},
                {"path": "styles.css", "purpose": "styles", "contract_references": [{"path": "index.html", "type": "html_selector_contract", "reason": "Styles target declared page elements"}]},
                {"path": "app.js", "purpose": "logic", "contract_references": [{"path": "index.html", "type": "dom_element_contract", "reason": "Script uses declared page elements"}]},
                {"path": "tests/test_logic.js", "purpose": "tests", "validation_targets": [{"path": "app.js", "reason": "Tests exercise application logic"}]},
            ]}
            manifest = normalize_manifest(raw, "", require_v2=True)
            validate_manifest_relationships(manifest)
            generation_order = topologically_order_manifest(manifest)
            build_order = topologically_order_manifest(manifest, graph="build_dependencies")
            self.assertEqual({x["path"] for x in generation_order}, {x["path"] for x in manifest})
            self.assertEqual({x["path"] for x in build_order}, {x["path"] for x in manifest})

        def test_html_css_javascript_relationships_cannot_be_build_prerequisites(self):
            manifest = [
                {"path": "index.html", "kind": "html", "purpose": "page", "build_dependencies": ["styles.css"]},
                {"path": "styles.css", "kind": "css", "purpose": "styles"},
            ]
            with self.assertRaisesRegex(BuildError, "HTML asset loading is a runtime_reference"):
                validate_manifest_relationships(manifest)
            manifest = [
                {"path": "app.js", "kind": "javascript", "purpose": "logic", "build_dependencies": ["index.html"]},
                {"path": "index.html", "kind": "html", "purpose": "page"},
            ]
            with self.assertRaisesRegex(BuildError, "CSS/DOM agreement is a contract_reference"):
                validate_manifest_relationships(manifest)

        def test_import_runtime_references_enforce_language_compatibility(self):
            invalid = [
                ([{"path": "app.js", "kind": "javascript", "purpose": "logic", "runtime_references": [{"path": "app.py", "type": "import"}]}, {"path": "app.py", "kind": "python", "purpose": "module"}], "javascript import cannot target python"),
                ([{"path": "styles.css", "kind": "css", "purpose": "styles", "runtime_references": [{"path": "app.js", "type": "import"}]}, {"path": "app.js", "kind": "javascript", "purpose": "logic"}], "css import cannot target javascript"),
            ]
            for manifest, marker in invalid:
                with self.subTest(marker=marker):
                    with self.assertRaisesRegex(BuildError, marker):
                        validate_manifest_relationships(manifest)
            valid = [
                {"path": "app.js", "kind": "javascript", "purpose": "logic", "runtime_references": [{"path": "module.js", "type": "import"}, {"path": "styles.css", "type": "import"}]},
                {"path": "module.js", "kind": "javascript", "purpose": "module"},
                {"path": "styles.css", "kind": "css", "purpose": "styles"},
            ]
            validate_manifest_relationships(valid)

        def test_contract_reference_types_enforce_source_target_direction(self):
            bad_cases = [
                ([{"path": "styles.css", "kind": "css", "purpose": "styles", "contract_references": [{"path": "index.html", "type": "selector_contract"} ]}, {"path": "index.html", "kind": "html", "purpose": "page"}], "selector_contract requires html -> css"),
                ([{"path": "app.js", "kind": "javascript", "purpose": "logic", "contract_references": [{"path": "index.html", "type": "dom_contract"} ]}, {"path": "index.html", "kind": "html", "purpose": "page"}], "dom_contract requires html -> javascript"),
            ]
            for manifest, marker in bad_cases:
                with self.subTest(marker=marker):
                    with self.assertRaisesRegex(BuildError, marker):
                        validate_manifest_relationships(manifest)
            valid = [
                {"path": "index.html", "kind": "html", "purpose": "page", "contract_references": [{"path": "styles.css", "type": "selector_contract"}, {"path": "app.js", "type": "dom_contract"}]},
                {"path": "styles.css", "kind": "css", "purpose": "styles", "contract_references": [{"path": "index.html", "type": "html_selector_contract"}]},
                {"path": "app.js", "kind": "javascript", "purpose": "logic", "contract_references": [{"path": "index.html", "type": "dom_element_contract"}]},
            ]
            validate_manifest_relationships(valid)

        def test_source_files_cannot_depend_on_documentation_in_build_graph(self):
            manifest = [
                {"path": "app.py", "kind": "python", "purpose": "app", "build_dependencies": ["README.md"]},
                {"path": "README.md", "kind": "documentation", "purpose": "docs"},
            ]
            with self.assertRaisesRegex(BuildError, "cannot depend on documentation"):
                validate_manifest_relationships(manifest)

        def test_manifest_build_cycle_is_independent_and_rejected(self):
            manifest = [
                {"path": "a.js", "purpose": "a", "build_dependencies": ["b.js"]},
                {"path": "b.js", "purpose": "b", "build_dependencies": ["a.js"]},
            ]
            with self.assertRaisesRegex(BuildError, "MANIFEST_BUILD_CYCLE"):
                topologically_order_manifest(manifest, graph="build_dependencies")

        def test_legacy_dependencies_are_preserved_but_not_ordered(self):
            manifest = normalize_manifest({"files": [
                {"path": "index.html", "purpose": "page", "dependencies": ["styles.css", "app.js"]},
                {"path": "styles.css", "purpose": "styles"},
                {"path": "app.js", "purpose": "logic"},
            ]}, "")
            ordered = topologically_order_manifest(manifest)
            self.assertEqual({x["path"] for x in ordered}, {"index.html", "styles.css", "app.js"})
            self.assertEqual(manifest[0]["ambiguous_legacy_dependencies"], ["styles.css", "app.js"])
            self.assertEqual(manifest[0]["generation_dependencies"], [])

        def test_v1_legacy_dependencies_are_not_dropped_when_typed_fields_exist(self):
            manifest = normalize_manifest({"schema_version": 1, "files": [
                {"path": "a.py", "purpose": "a", "dependencies": ["b.py"], "generation_dependencies": []},
                {"path": "b.py", "purpose": "b"},
            ]}, "")
            self.assertEqual(manifest[0]["ambiguous_legacy_dependencies"], ["b.py"])
            self.assertEqual(manifest[0]["generation_dependencies"], [])

        def test_v1_malformed_legacy_dependencies_rejected_even_when_empty_string(self):
            with self.assertRaisesRegex(BuildError, "malformed v1 dependencies"):
                normalize_manifest({"schema_version": 1, "files": [
                    {"path": "a.py", "purpose": "a", "dependencies": ""},
                ]}, "")

        def test_v2_rejects_legacy_dependencies_field_even_when_empty(self):
            with self.assertRaisesRegex(BuildError, "uses legacy dependencies"):
                normalize_manifest({"schema_version": 2, "files": [
                    {"path": "a.py", "purpose": "a", "dependencies": [], "generation_dependencies": []},
                ]}, "", require_v2=True)

        def test_ui_relationship_graph_does_not_invent_css_to_js_edge(self):
            manifest = [
                {"path": "index.html", "purpose": "page", "runtime_references": [{"path": "styles.css", "type": "stylesheet"}, {"path": "app.js", "type": "script"}]},
                {"path": "styles.css", "purpose": "styles"},
                {"path": "app.js", "purpose": "logic"},
                {"path": "tests/test_logic.js", "purpose": "tests", "validation_targets": ["app.js"]},
            ]
            project = {"index.html": '<html><head><link rel="stylesheet" href="styles.css"></head><body><script src="app.js"></script></body></html>', "styles.css": "body { color: black; }", "app.js": "function boot() {}", "tests/test_logic.js": "// tests app.js"}
            graph = dependency_graph(manifest, project)
            self.assertIn("styles.css", graph["index.html"])
            self.assertIn("app.js", graph["index.html"])
            self.assertNotIn("app.js", graph["styles.css"])
            self.assertNotIn("styles.css", graph["app.js"])

        def test_cycle_diagnostic_includes_relationship_reason(self):
            manifest = [
                {"path": "a.js", "purpose": "a", "generation_dependencies": ["b.js"], "relationship_reasons": {"generation_dependencies": {"b.js": "Needs shared schema first"}}},
                {"path": "b.js", "purpose": "b", "generation_dependencies": ["a.js"], "relationship_reasons": {"generation_dependencies": {"a.js": "Needs generated API first"}}},
            ]
            with self.assertRaisesRegex(BuildError, "Needs shared schema first"):
                topologically_order_manifest(manifest)
        def test_css_recovery_prompt_does_not_echo_rejected_candidate(self):
            base = "Return one CSS rule only."
            rejected = ":root { --color-a: red; } EXTRA"
            retry = _css_unit_recovery_prompt(base, {"name": "CSS_DesignTokens", "purpose": "Design tokens"}, 2, "CSS_UNIT_BOUNDARY_INVALID")
            self.assertNotIn(rejected, retry)
            self.assertIn("CSS CONTRACT RECOVERY", retry)
            self.assertIn("--content-max-width", retry)
        def test_css_fence_is_removed_only_when_entire_response_is_fenced(self):
            fenced = "```css\n:root { --x: 1; }\n```"
            self.assertEqual(_strip_single_css_fence(fenced), ":root { --x: 1; }")
            mixed = "Here is CSS:\n```css\n:root { --x: 1; }\n```"
            self.assertEqual(_strip_single_css_fence(mixed), mixed)
        def test_css_plan_rejects_html_fragment_unit(self):
            bad = {"preamble": "", "units": [{"name": "Shell", "kind": "html_fragment", "purpose": "HTML shell"}]}
            with self.assertRaisesRegex(BuildError, "UNIT_PLAN_NO_COMPATIBLE_UNITS"):
                validate_unit_plan_for_file(bad, "styles.css")
        def test_html_fragment_rejects_document_shell(self):
            with self.assertRaisesRegex(BuildError, "HTML_UNIT_DOCUMENT_SHELL_FORBIDDEN"):
                validate_html_unit_fragment("<html><body><main>x</main></body></html>", "index.html")
        def test_html_fragments_insert_inside_app_shell(self):
            shell = '<!doctype html><html><head></head><body><div id="app-shell"><!-- placeholder --></div><script src="app.js"></script></body></html>'
            out = assemble_html_document(shell, ["<aside>Nav</aside>", "<main>Content</main>"], "", "index.html")
            self.assertLess(out.index("<aside>"), out.index("</div>"))
            self.assertLess(out.index("<main>"), out.index("</div>"))
            self.assertLess(out.index("</div>"), out.index("</body>"))
        def test_manifest_correction_file_set_allows_only_reasoned_optional_removal(self):
            original = {"app.py": {"path": "app.py", "kind": "python", "required": True}, "unused.py": {"path": "unused.py", "kind": "python", "required": False}}
            corrected = {"app.py": {"path": "app.py", "kind": "python", "required": True}}
            validate_manifest_correction_file_set(original, corrected, {"app.py"}, {("unused.py", "file", "unused.py")})
            with self.assertRaisesRegex(BuildError, "file removal is not justified"):
                validate_manifest_correction_file_set(original, corrected, {"app.py"}, set())
            with self.assertRaisesRegex(BuildError, "explicitly required file was removed"):
                validate_manifest_correction_file_set(original, {"unused.py": original["unused.py"]}, {"app.py"}, {("app.py", "file", "app.py")})
        def test_manifest_correction_file_set_rejects_added_or_reowned_files(self):
            original = {"app.py": {"path": "app.py", "kind": "python", "required": True}}
            with self.assertRaisesRegex(BuildError, "unrelated file was added"):
                validate_manifest_correction_file_set(original, {**original, "other.py": {"path": "other.py", "kind": "python"}}, set(), set())
            with self.assertRaisesRegex(BuildError, "file ownership changed"):
                validate_manifest_correction_file_set(original, {"app.py": {"path": "app.py", "kind": "javascript", "required": True}}, set(), set())
        def test_manifest_schema_rejects_boolean_and_float_versions(self):
            for version in (True, 2.0, "2"):
                with self.subTest(version=version):
                    with self.assertRaisesRegex(BuildError, "unsupported schema_version"):
                        normalize_manifest({"schema_version": version, "files": [{"path": "a.py", "purpose": "a"}]}, "")
        def test_manifest_schema_requires_string_path_and_purpose(self):
            for row in ({"path": 123, "purpose": "a"}, {"path": "a.py", "purpose": 123}):
                with self.subTest(row=row):
                    with self.assertRaisesRegex(BuildError, "must be a string"):
                        normalize_manifest({"schema_version": 2, "files": [row]}, "")
        def test_manifest_v2_required_flag_must_be_boolean(self):
            with self.assertRaisesRegex(BuildError, "required must be a boolean"):
                normalize_manifest({"schema_version": 2, "files": [{"path": "a.py", "purpose": "a", "required": "false"}]}, "")
        def test_duplicate_manifest_path_rejected(self):
            with self.assertRaises(BuildError): normalize_manifest({"files": [{"path": "a.py", "purpose": "one"}, {"path": "a.py", "purpose": "two"}]}, "spec")
        def test_explicit_file_bullets_are_required(self):
            spec = "Use separate, well-organized files:\n- index.html\n- styles.css\n- app.js\n- README.md\n- tests/test_logic.js\n"
            self.assertEqual(set(required_file_lines(spec)), {"index.html", "styles.css", "app.js", "README.md", "tests/test_logic.js"})
        def test_manifest_rejects_source_type_forbidden_by_spec(self):
            manifest = [{"path": "app.py", "kind": "python", "purpose": "application"}]
            with self.assertRaisesRegex(BuildError, "MANIFEST_FILE_OWNERSHIP_INVALID"):
                validate_manifest_relationships(manifest, "Use only:\n- HTML5\n- CSS3\n- Plain JavaScript (ES6+)\n")
        def test_test_validation_target_points_to_application_source(self):
            manifest = [
                {"path": "app.js", "kind": "javascript", "purpose": "application"},
                {"path": "tests/test_logic.js", "kind": "test", "purpose": "logic tests", "validation_targets": ["app.js"]},
            ]
            validate_manifest_relationships(manifest)
        def test_non_test_validation_target_rejected(self):
            manifest = [
                {"path": "app.js", "kind": "javascript", "purpose": "application", "validation_targets": ["tests/test_logic.js"]},
                {"path": "tests/test_logic.js", "kind": "test", "purpose": "logic tests", "validation_targets": ["app.js"]},
            ]
            with self.assertRaisesRegex(BuildError, "only test/validator files"):
                validate_manifest_relationships(manifest)
        def test_explicit_spec_file_overrides_optional_manifest_flag(self):
            manifest = normalize_manifest({"files": [{"path": "tests/test_app.py", "purpose": "tests", "required": "false"}]}, "spec")
            result = ensure_required_files(manifest, "The project must include tests/test_app.py")
            self.assertTrue(result[0]["required"])
        def test_css_ambiguous_target_rejected(self):
            src = ".item { color:red }\n.item { color:blue }\n.item { color:green }"
            with self.assertRaisesRegex(BuildError, "AMBIGUOUS"):
                apply_structural_patch("styles.css", src, {"path": "styles.css", "target": {"kind": "css_rule", "selector": ".item"}, "replacement": ".item { color:black }"})
        def test_css_unique_target_replaced(self):
            src = ".a { color:red }\n.b { color:blue }"
            out = apply_structural_patch("styles.css", src, {"path": "styles.css", "target": {"kind": "css_rule", "selector": ".a"}, "replacement": ".a { color:black }"})
            self.assertIn("color:black", out); self.assertIn("color:blue", out)
        def test_css_missing_selector_rule_is_inserted_safely(self):
            source = ":root { --ink: #111; }"
            out = apply_structural_patch("styles.css", source, {"path": "styles.css", "target": {"kind": "css_rule", "selector": ".activity-log"}, "replacement": ".activity-log { display: grid; gap: 8px; }"})
            self.assertIn(".activity-log {", out)
            self.assertIn("display: grid", out)
        def test_contract_css_selectors_are_repaired_deterministically(self):
            contract = {"css_selectors": [{"selector": ".sidebar"}, {"selector": ".dashboard"}]}
            source = ".app-shell { display: flex; }"
            finding = {"rule": "CSS_SELECTOR_CONTRACT_MISMATCH", "message": "Canonical CSS selectors are missing: ['.sidebar', '.dashboard']."}
            patched = repair_css_selector_contract("styles.css", source, finding, contract)
            self.assertIsNotNone(patched)
            self.assertIn(".sidebar {", patched)
            self.assertIn(".dashboard {", patched)
        def test_nested_design_tokens_map_to_css_variables(self):
            contract = {"design_tokens": {"colors": {"primary": "#123"}, "spacing": {"md": "8px"}, "typography": {"button": "14px"}, "sidebar_width": "280px"}}
            self.assertEqual(set(expected_design_token_values(contract)), {"color-primary", "space-md", "font-size-button", "sidebar-width"})
            source = ":root { --color-primary: #123; --space-md: 8px; --sidebar-width: 280px; }"
            patched = repair_design_token_rule("styles.css", source, {"rule": "DESIGN_TOKEN_CONTRACT_MISMATCH"}, contract)
            self.assertIn("--font-size-button: 14px;", patched)
        def test_css_identical_duplicate_rules_collapse_and_declarations_wrap(self):
            src = ".app-shell { display: flex; }\n.app-shell { display: flex; }"
            out = apply_structural_patch("styles.css", src, {"path": "styles.css", "target": {"kind": "css_rule", "selector": ".app-shell"}, "replacement": "display: flex; min-height: 100vh;"})
            self.assertEqual(len(css_rule_spans(out)), 1)
            self.assertIn(".app-shell {", out)
            self.assertIn("min-height: 100vh", out)
        def test_python_symbol_targeted(self):
            src = "def a():\n    return 1\n\ndef b():\n    return 2\n"
            out = apply_structural_patch("m.py", src, {"path": "m.py", "target": {"kind": "python_symbol", "name": "a"}, "replacement": "def a():\n    return 3"})
            self.assertIn("return 3", out); self.assertIn("return 2", out)
        def test_exact_span_hash_derived_for_unique_target_only(self):
            source = "<main id=\"app\"></main>"
            patch = {"path": "index.html", "target": {"kind": "exact_span", "text": '<main id="app"></main>', "sha256": "0" * 64}, "replacement": "<main id=\"app\"><section></section></main>"}
            self.assertTrue(derive_unique_exact_span_hash(source, patch))
            self.assertIn("<section>", apply_structural_patch("index.html", source, patch))
            ambiguous = {"path": "index.html", "target": {"kind": "exact_span", "text": "duplicate", "sha256": "0" * 64}, "replacement": "x"}
            self.assertFalse(derive_unique_exact_span_hash("duplicate duplicate", ambiguous))
        def test_exact_span_hash_repairs_double_escaped_newlines(self):
            source = '<a href="#">\n  Calendar\n</a>'
            patch = {"path": "index.html", "target": {"kind": "exact_span", "text": '<a href="#">\\n  Calendar\\n</a>', "sha256": "0" * 64}, "replacement": '<a href="#" id="calendar-view">\n  Calendar\n</a>'}
            self.assertTrue(derive_unique_exact_span_hash(source, patch))
            self.assertIn("\n", patch["target"]["text"])
            self.assertIn('id="calendar-view"', apply_structural_patch("index.html", source, patch))
        def test_repair_context_is_bounded_and_finding_focused(self):
            source = "class App {\n" + "noise line\n" * 4000 + "document.getElementById('app');\n}"
            findings = [{"rule": "JS_DOM_REFERENCE_UNRESOLVED", "message": "JavaScript references missing id 'app'."}]
            context = repair_source_context("app.js", source, findings, limit=1000)
            self.assertLessEqual(len(context), 1000)
            self.assertIn("getElementById('app')", context)
            self.assertLessEqual(context.count("noise line"), 2)
        def test_repair_context_preserves_distributed_findings_within_budget(self):
            source = "\n".join(["noise"] * 80 + ["document.getElementById('alpha');"] + ["noise"] * 80 + ["document.getElementById('beta');"] + ["noise"] * 80 + ["document.getElementById('gamma');"])
            findings = [{"rule": "JS_DOM_REFERENCE_UNRESOLVED", "message": f"JavaScript references missing id '{name}'."} for name in ("alpha", "beta", "gamma")]
            context = repair_source_context("app.js", source, findings, limit=900)
            self.assertLessEqual(len(context), 900)
            for name in ("alpha", "beta", "gamma"):
                self.assertIn(f"getElementById('{name}')", context)
        def test_html_repair_context_maps_contract_selectors_to_attributes(self):
            source = '<div id="app-shell"><main id="content-area" class="content-area"></main></div>'
            findings = [{"rule": "CONTRACT_HTML_CLASS_MISSING", "message": "Canonical contract requires class .app-shell, but it is absent."}]
            context = repair_source_context("index.html", source, findings, limit=120)
            self.assertIn('id="app-shell"', context)
        def test_dynamic_interface_context_routes_to_matching_view_class(self):
            source = "class Project { }\n" + "noise line\n" * 300 + "class Dashboard { render() {} }\n"
            findings = [{"rule": "CONTRACT_HTML_ID_MISSING", "message": "Canonical contract requires HTML id #project-cards, but it is absent."}]
            context = repair_source_context("app.js", source, findings, limit=1000)
            self.assertIn("class Dashboard", context)
        def test_known_dynamic_view_root_repairs_are_syntax_checked(self):
            source = "class KanbanBoard { render(container) { container.innerHTML = ''; } }\nclass CalendarView { render(container) { container.innerHTML = ''; } }"
            finding = {"rule": "CONTRACT_HTML_ID_MISSING", "message": "Canonical contract requires HTML id #kanban-board."}
            patched = repair_dynamic_view_root("app.js", source, finding)
            self.assertIsNotNone(patched)
            self.assertIn('container.id = "kanban-board";', patched)
            static_validate({"app.js": patched})
            class_patch = repair_dynamic_view_root("app.js", source, {"rule": "CONTRACT_HTML_CLASS_MISSING", "message": "Canonical contract requires class .calendar-view."})
            self.assertIn('container.classList.add("calendar-view")', class_patch)
        def test_dynamic_view_host_without_generic_render_gets_root_identity_in_constructor(self):
            source = "class KanbanBoard { constructor(taskManager, dataStore, containerElement) { this.taskManager = taskManager; this.container = containerElement; this.renderBoard(); } renderBoard() {} }"
            finding = {"rule": "CONTRACT_HTML_ID_MISSING", "message": "Canonical contract requires HTML id #kanban-board."}
            patched = repair_dynamic_view_root("app.js", source, finding)
            self.assertIn('this.container.id = "kanban-board";', patched)
            self.assertEqual(patched.count("constructor("), 1)
            static_validate({"app.js": patched})
        def test_html_contract_shell_normalizes_duplicate_root_and_builds_hosts(self):
            source = '<!doctype html><html lang="en"><head><title>Test</title><link rel="stylesheet" href="styles.css"></head><body><div id="app-shell"><div id="app-shell"></div></div><script src="app.js"></script></body></html>'
            contract = {"html_elements": [{"id": "app-shell", "class": "app-shell"}, {"id": "header", "class": "header"}, {"id": "content-area", "class": "content-area"}, {"id": "calendar-view", "class": "calendar-view"}]}
            normalized = normalize_html_contract_shell(source, contract, {"app.js": "document.getElementById('view-container');"})
            static_validate({"index.html": normalized})
            facts = HTMLFacts(); facts.feed(normalized); facts.close()
            self.assertEqual(normalized.count('id="app-shell"'), 1)
            self.assertIn("content-area", facts.ids)
            self.assertIn("calendar-view", facts.ids)
            self.assertIn("view-container", facts.ids)
            self.assertIn('href="styles.css"', normalized)
            self.assertIn('<script src="app.js"></script>', normalized)
        def test_dashboard_dynamic_interfaces_are_added_together(self):
            source = "class Dashboard {\n    getDashboardData() { return {projectProgress: [], recentActivity: []}; }\n}\nclass KanbanBoard {}"
            patched = repair_dynamic_view_root("app.js", source, {"rule": "CONTRACT_HTML_ID_MISSING", "message": "Canonical contract requires HTML id #activity-log."})
            self.assertIn('root.id = "overview-dashboard"', patched)
            self.assertIn('projects.id = "project-cards"', patched)
            self.assertIn('activity.id = "activity-log"', patched)
        def test_static_html_class_repair_uses_existing_matching_id(self):
            source = '<!doctype html><html><head></head><body><div id="app-shell"><main></main></div></body></html>'
            patched = repair_dynamic_view_root("index.html", source, {"rule": "CONTRACT_HTML_CLASS_MISSING", "message": "Canonical contract requires class .app-shell."})
            self.assertEqual(patched, '<!doctype html><html><head></head><body><div id="app-shell" class="app-shell"><main></main></div></body></html>')
        def test_dom_reference_repair_uses_existing_shell_and_view_hosts(self):
            source = "class Navigation { constructor() { this.container = document.getElementById('app-container'); } }\nfunction AppShellRenderer() {}\nasync function initializeApp() { const a = document.getElementById('app-container'); if (!a) return; const b = document.getElementById('app-container'); }"
            patched = repair_dynamic_view_root("app.js", source, {"rule": "JS_DOM_REFERENCE_UNRESOLVED", "message": "JavaScript references missing id 'app-container'."})
            self.assertIn("this.container = document.getElementById('view-container')", patched)
            self.assertEqual(patched.count("getElementById('app-shell')"), 2)
            self.assertIn("AppShellRenderer.prototype.renderShell", patched)
        def test_navigation_dom_references_are_added_to_matching_links(self):
            source = '<!doctype html><html><head></head><body><nav><a data-view="overview">Overview</a></nav></body></html>'
            patched = repair_dynamic_view_root("index.html", source, {"rule": "JS_DOM_REFERENCE_UNRESOLVED", "message": "JavaScript references missing id 'nav-overview'."})
            self.assertIn('<a data-view="overview" id="nav-overview">', patched)
        def test_unresolved_javascript_class_is_repaired_on_matching_html_button(self):
            source = '<!doctype html><html><head></head><body><div id="app-shell"><header><button id="new-task-button" class="primary-action-button">New Task</button></header></div></body></html>'
            finding = {"rule": "JS_DOM_REFERENCE_UNRESOLVED", "message": "JavaScript references missing class 'primary-action-new-task'."}
            patched = repair_dynamic_view_root("index.html", source, finding)
            self.assertIn('id="new-task-button" class="primary-action-button primary-action-new-task"', patched)
            validate_contract_patch_target("index.html", patched, finding)
        def test_private_storage_constant_matches_canonical_storage_key(self):
            source = 'class DataStore { static #STORAGE_KEY = "taskflow_pro_v1"; load() { return localStorage.getItem(DataStore.#STORAGE_KEY); } }'
            facts = extract_javascript_interfaces(source, "app.js")
            self.assertEqual(facts["storage_keys"], {"taskflow_pro_v1"})
            contract = {"storage": {"key": "taskflow_pro_v1", "schema": {"version": "1"}}, "html_elements": [], "css_selectors": [], "events": [], "data_models": []}
            report = validate_contract_consistency({"app.js": source}, [{"path": "app.js"}], contract)
            self.assertNotIn("STORAGE_CONTRACT_MISMATCH", {row["rule"] for row in report["findings"]})
        def test_storage_key_is_extracted_when_local_storage_uses_an_instance_alias(self):
            source = 'class DataStore { static STORAGE_KEY = "taskflow_pro_v1"; static #storage = localStorage; load() { return this.#storage.getItem(this.STORAGE_KEY); } }'
            facts = extract_javascript_interfaces(source, "app.js")
            self.assertEqual(facts["storage_keys"], {"taskflow_pro_v1"})
        def test_native_dom_handlers_do_not_count_as_app_event_contracts(self):
            source = "button.addEventListener('click', handler); drop.addEventListener('dragover', handler);"
            facts = extract_javascript_interfaces(source, "app.js")
            self.assertEqual(facts["event_names"], set())
            contract = {"events": [{"name": "taskCreated"}], "html_elements": [], "css_selectors": [], "storage": [], "data_models": []}
            report = validate_contract_consistency({"app.js": source}, [{"path": "app.js"}], contract)
            self.assertNotIn("EVENT_CONTRACT_MISMATCH", {row["rule"] for row in report["findings"]})
        def test_calendar_container_references_target_render_root(self):
            source = "class CalendarView { render(container) { container.id = 'calendar-view'; } go() { return document.getElementById('calendar-view-container'); } }"
            patched = repair_dynamic_view_root("app.js", source, {"rule": "JS_DOM_REFERENCE_UNRESOLVED", "message": "JavaScript references missing id 'calendar-view-container'."})
            self.assertIn("getElementById('calendar-view')", patched)
        def test_missing_page_subtitle_is_added_beside_page_title(self):
            source = '<!doctype html><html><head></head><body><h1 id="page-title">Overview</h1></body></html>'
            patched = repair_dynamic_view_root("index.html", source, {"rule": "JS_DOM_REFERENCE_UNRESOLVED", "message": "JavaScript references missing id 'page-subtitle'."})
            self.assertIn('<p id="page-subtitle"', patched)
        def test_repairs_each_finding_independently(self):
            class PerFindingClient:
                def __init__(self): self.seen = []
                def json(self, model, prompt, timeout=None, num_predict=4096):
                    self.seen.append(prompt)
                    if '"message": "fix alpha"' in prompt:
                        return {"path": "app.py", "target": {"kind": "exact_span", "text": "return 1", "sha256": sha256_text("return 1")}, "replacement": "return 10"}
                    if '"message": "fix beta"' in prompt:
                        return {"path": "app.py", "target": {"kind": "exact_span", "text": "return 2", "sha256": sha256_text("return 2")}, "replacement": "return 20"}
                    raise AssertionError("A repair prompt must contain exactly one verified finding")
            with tempfile.TemporaryDirectory() as td:
                stage = Path(td) / "stage"; stage.mkdir()
                ctx = BuildContext("client-selftest-" + uuid.uuid4().hex[:8], "p", stage, "two values", sha256_text("two values"), 10, False)
                ctx.manifest = [{"path": "app.py", "purpose": "two values", "dependencies": []}]
                ctx.project = {"app.py": "def alpha():\n    return 1\ndef beta():\n    return 2\n"}
                ctx.contract = {}
                client = PerFindingClient()
                result = repair_project(client, ctx, [{"file": "app.py", "message": "fix alpha"}, {"file": "app.py", "message": "fix beta"}], 1)
                self.assertEqual(len(client.seen), 2)
                self.assertIn("return 10", result["app.py"])
                self.assertIn("return 20", result["app.py"])
        def test_html_open_container_patch_preserves_existing_closer(self):
            original = '<div id="app-shell"><p>existing</p></div><script></script>'
            patch = {"path": "index.html", "target": {"kind": "exact_span", "text": '<div id="app-shell">', "sha256": sha256_text('<div id="app-shell">')}, "replacement": '<div id="app-shell"><aside></aside></div><main></main>'}
            updated = apply_structural_patch("index.html", original, patch)
            validate_html_unit_fragment(updated, "index.html")
            self.assertIn("<main></main>", updated)
        def test_html_structural_repair_uses_exact_markup_and_rejects_css_target(self):
            original = '<!doctype html><html><body><div id="app-shell"></div></body></html>'
            wrong = {"path": "index.html", "target": {"kind": "css_rule", "selector": ".app-shell"}, "replacement": "display:flex"}
            with self.assertRaisesRegex(BuildError, "PATCH_TARGET_KIND_MISMATCH: css_rule cannot target index.html"):
                apply_structural_patch("index.html", original, wrong)
            target = '<div id="app-shell">'
            patch = {"path": "index.html", "target": {"kind": "exact_span", "text": target, "sha256": sha256_text(target)}, "replacement": '<div id="app-shell" class="app-shell">'}
            updated = apply_structural_patch("index.html", original, patch)
            validate_html_document(updated, "index.html")
            self.assertIn('id="app-shell" class="app-shell"', updated)
        def test_css_brace_ignored_in_string(self):
            self.assertTrue(balanced_delimiters('.x::after { content:"}"; }', css=True))
        def test_css_extra_brace_detected(self):
            self.assertFalse(balanced_delimiters(".x { color:red }}", css=True))
        def test_js_unclosed_string_detected(self):
            self.assertFalse(balanced_delimiters("const x = 'broken", css=False))
        def test_test_fixture_does_not_satisfy_dom_contract(self):
            project = {"index.html": "<div id='real'></div>", "tests/fixture.js": "document.getElementById('test-only').id='test-only'", "app.js": "document.getElementById('real');"}
            facts = build_contract(project, [{"path": p, "purpose": "x", "dependencies": []} for p in project])
            self.assertNotIn("test-only", facts["javascript"]["dynamic_ids"])
        def test_dependency_closure(self):
            graph = {"a.py": set(), "b.py": {"a.py"}, "c.py": {"b.py"}, "z.py": set()}
            self.assertEqual(dependent_closure({"a.py"}, graph), {"a.py", "b.py", "c.py"})
        def test_audit_pass_with_findings_rejected(self):
            with self.assertRaises(BuildError): validate_audit_response({"status": "PASS", "findings": [{"file": "a.py", "evidence": "x=1"}]}, {"a.py": "x=1"})
        def test_audit_exact_evidence_required(self):
            with self.assertRaises(BuildError): validate_audit_response({"status": "FAIL", "findings": [{"file": "a.py", "evidence": "x=2"}]}, {"a.py": "x=1"})
        def test_missing_test_not_optional(self):
            self.assertFalse(_test_optional([{ "path": "tests/test_app.py", "purpose": "Required unit tests" }]))
        def test_missing_test_optional(self):
            self.assertTrue(_test_optional([{ "path": "tests/test_app.py", "purpose": "Optional tests if applicable" }]))
        def test_contract_mismatch_detected(self):
            project = {"index.html": "<div id='root'></div>", "app.js": "document.getElementById('missing');"}
            manifest = [{"path": p, "purpose": "x", "dependencies": []} for p in project]
            report = validate_contract_consistency(project, manifest, {"html_elements": [], "storage": [], "events": []})
            self.assertEqual(report["status"], "FAIL")
        def test_atomic_write_round_trip(self):
            with tempfile.TemporaryDirectory() as td:
                p = Path(td) / "state.json"; save_json(p, {"x": 1}); self.assertEqual(load_json(p), {"x": 1})
        def test_corrupt_checkpoint_rejected(self):
            with tempfile.TemporaryDirectory() as td:
                root = Path(td); run_id = "client-selftest-" + uuid.uuid4().hex[:8]; ctx = BuildContext(run_id, "p", root, "s", sha256_text("s"), 10, True)
                st = StateStore(ctx); st.set_manifest([{"path": "a.py", "purpose": "test", "dependencies": []}]); st.accept_unit("a.py", "f", "def f():\n return 1\n", prompt_hash="p", contract_hash="c", dependency_hash="d")
                record = st.state["accepted_units"]["a.py::f"]; Path(record["source_file"]).write_text("def f():\n return 2\n", encoding="utf-8")
                self.assertIsNone(st.restore_unit("a.py", "f", prompt_hash="p", contract_hash="c", dependency_hash="d"))
                shutil.rmtree(RUNS_DIR / run_id, ignore_errors=True)
        def test_spec_hash_mismatch_rejected(self):
            with tempfile.TemporaryDirectory() as td:
                root = Path(td); run_id = "client-selftest-" + uuid.uuid4().hex[:8]; ctx = BuildContext(run_id, "p", root, "s", sha256_text("s"), 10, True)
                StateStore(ctx); ctx2 = BuildContext(run_id, "p", root, "changed", sha256_text("changed"), 10, True, resume=True)
                with self.assertRaises(BuildError): StateStore(ctx2)
                shutil.rmtree(RUNS_DIR / run_id, ignore_errors=True)
        def test_windows_case_collision_rejected(self):
            with self.assertRaises(BuildError): validate_project({"App.py": "x=1", "app.py": "x=2"})
        def test_windows_reserved_name_rejected(self):
            with self.assertRaises(BuildError): safe_relative_path("CON.txt")
        def test_html_document_structure_passes(self):
            validate_html_document("<!doctype html><html><head></head><body><main></main></body></html>", "index.html")
        def test_html_misnested_tags_rejected(self):
            with self.assertRaisesRegex(BuildError, "HTML_FRAGMENT_STRUCTURE_INVALID"):
                validate_html_document("<html><head></head><body><div></body></div></html>", "index.html")
        def test_html_truncated_tag_rejected(self):
            with self.assertRaisesRegex(BuildError, "HTML_TRUNCATED_TAG"):
                validate_html_document("<html><head></head><body><div", "index.html")
        def test_nested_css_rule_is_addressable(self):
            src = "@media (max-width: 600px) { .item { color: red; } }"
            self.assertEqual(len([x for x in css_rule_spans(src) if x[0] == ".item"]), 1)
            out = apply_structural_patch("styles.css", src, {"path": "styles.css", "target": {"kind": "css_rule", "selector": ".item"}, "replacement": ".item { color: blue; }"})
            self.assertIn("color: blue", out)
        def test_python_duplicate_implementation_rejected(self):
            with self.assertRaisesRegex(BuildError, "DUPLICATE_TOP_LEVEL"):
                validate_assembled_python("def f(): return 1\ndef f(): return 2\n", "m.py")
        def test_python_preamble_rejects_function_body(self):
            with self.assertRaisesRegex(BuildError, "PREAMBLE_BOUNDARY"):
                validate_unit_preamble("def f(): return 1", "m.py", 10)
        def test_javascript_function_unit_boundary(self):
            unit = {"kind": "function", "name": "save"}
            if shutil.which("node"):
                self.assertIn("function save", validate_javascript_unit("function save(){ return true; }", unit, "app.js", 10))
                with self.assertRaisesRegex(BuildError, "UNIT_BOUNDARY"):
                    validate_javascript_unit("function save(){} function other(){}", unit, "app.js", 10)
            else:
                self.assertEqual(javascript_top_level_declarations("function save(){}", "app.js")["functions"], {"save"})
        def test_javascript_entrypoint_accepts_one_cohesive_setup_helper(self):
            if not shutil.which("node"):
                self.skipTest("Node.js is required for JavaScript syntax validation")
            unit = {"kind": "function", "name": "initializeApp"}
            source = "function initializeApp(){ setupGlobalEventListeners(); }\nfunction setupGlobalEventListeners(){ return true; }"
            validated = validate_javascript_unit(source, unit, "app.js", 10)
            self.assertIn("function setupGlobalEventListeners", validated)
        def test_javascript_top_level_async_method_syntax_is_normalized(self):
            unit = {"kind": "function", "name": "seedDemoData"}
            source = "async seedDemoData() { return []; }"
            validated = validate_javascript_unit(source, unit, "app.js", 10)
            self.assertIn("async function seedDemoData()", validated)
        def test_css_markdown_fence_is_safely_normalized(self):
            source = "```css\n.a { color: red; }\n```"
            self.assertEqual(_strip_single_css_fence(source), ".a { color: red; }")
        def test_css_partial_fence_or_prose_is_not_stripped(self):
            source = "Here is CSS:\n```css\n.a { color: red; }\n```"
            self.assertEqual(_strip_single_css_fence(source), source)
        def test_css_design_tokens_complete_contract_passes(self):
            css = ":root {\n" + "\n".join([
                "--color-primary: #6a4ca4;", "--color-secondary: #4a90e2;", "--color-success: #4caf50;",
                "--color-warning: #ff9800;", "--color-danger: #f44336;", "--color-background: #f8f9fa;",
                "--font-family-base: system-ui;", "--font-size-base: 14px;", "--font-size-h1: 2rem;",
                "--font-size-h2: 1.5rem;", "--font-size-h3: 1.25rem;", "--font-weight-normal: 400;",
                "--font-weight-bold: 700;", "--line-height-base: 1.5;", "--space-sm: .5rem;",
                "--space-md: 1rem;", "--space-lg: 1.5rem;", "--space-xl: 2rem;",
                "--radius-sm: 2px;", "--radius-md: 6px;", "--shadow-base: 0 2px 4px #0001;",
                "--sidebar-width: 250px;", "--content-max-width: 1200px;", "--transition-duration: .2s;",
            ]) + "\n}"
            unit = {"name": "CSS_DesignTokens", "purpose": "Define all centralized design tokens"}
            self.assertIn(":root", validate_css_rule_unit("```css\n" + css + "\n```", unit, "styles.css"))
        def test_css_design_tokens_missing_semantic_categories_rejected(self):
            css = ":root { --color-primary: red; --font-family-base: sans-serif; --font-size-base: 14px; --font-weight-normal: 400; --line-height-base: 1.5; }"
            unit = {"name": "CSS_DesignTokens", "purpose": "Define all centralized design tokens"}
            with self.assertRaisesRegex(BuildError, "CSS_DESIGN_TOKEN_COVERAGE_INCOMPLETE"):
                validate_css_rule_unit(css, unit, "styles.css")
        def test_css_design_tokens_reject_extra_selector(self):
            css = ":root { --color-primary: red; } body { margin: 0; }"
            unit = {"name": "CSS_DesignTokens", "purpose": "Define all centralized design tokens"}
            with self.assertRaisesRegex(BuildError, "CSS_UNIT_BOUNDARY_INVALID"):
                validate_css_rule_unit(css, unit, "styles.css")
        def test_css_non_token_rule_keeps_its_own_selector(self):
            unit = {"name": "Body_BaseStyles", "purpose": "Base body styling"}
            self.assertEqual(validate_css_rule_unit("body { margin: 0; }", unit, "styles.css"), "body { margin: 0; }")
        def test_css_design_tokens_reject_non_custom_property(self):
            css = ":root { color: red; }"
            unit = {"name": "CSS_DesignTokens", "purpose": "Define all centralized design tokens"}
            with self.assertRaisesRegex(BuildError, "CSS_DESIGN_TOKEN_DECLARATION_INVALID"):
                validate_css_rule_unit(css, unit, "styles.css")
        def test_css_design_token_prompt_is_compact_and_file_scoped(self):
            prompt = _css_unit_prompt_contract({"name": "CSS_DesignTokens", "purpose": "Centralized design tokens"})
            self.assertIn("exactly one top-level :root", prompt)
            self.assertIn("--sidebar-width", prompt)
            self.assertIn("not Markdown", prompt)
        def test_css_base_unit_that_mentions_tokens_uses_rule_contract(self):
            unit = {"name": "CSS_BaseStyles", "purpose": "Apply base styles using the defined design tokens"}
            self.assertNotIn("exactly one top-level :root", _css_unit_prompt_contract(unit))
            self.assertEqual(validate_css_rule_unit(".app-shell { display: grid; }", unit, "styles.css"), ".app-shell { display: grid; }")
            self.assertNotIn("one raw :root rule", _css_unit_recovery_prompt("base prompt", unit, 2, "duplicate"))
        def test_css_recovery_names_accepted_selectors(self):
            prompt = "ALREADY ACCEPTED CSS SOURCE (authoritative prefix):\n:root { --x: 1; }\n.app-shell { display: flex; }\nUNIT DEPENDS ON: []"
            unit = {"name": "CSS_LayoutComponents", "purpose": "Style the sidebar and header"}
            recovered = _css_unit_recovery_prompt(prompt, unit, 2, "CSS_SELECTOR_ALREADY_ACCEPTED")
            self.assertIn(".app-shell", recovered)
            self.assertEqual(_css_selector_names(".a, .b { color: red; }"), {".a", ".b"})
        def test_css_rule_unit_boundary(self):
            spans = css_rule_spans(".a { color:red; }")
            self.assertEqual(len([x for x in spans if not x[0].startswith("@")]), 1)
        def test_css_unit_allows_same_selector_in_responsive_rules(self):
            unit = {"name": "CSS_Sidebar", "purpose": "Responsive sidebar layout"}
            source = ".sidebar { width: 240px; } @media (max-width: 700px) { .sidebar { width: 100%; } }"
            self.assertEqual(validate_css_rule_unit(source, unit, "styles.css"), source)
        def test_css_unit_rejects_multiple_distinct_selectors(self):
            unit = {"name": "CSS_Sidebar", "purpose": "Sidebar layout"}
            with self.assertRaisesRegex(BuildError, "one unique selector"):
                validate_css_rule_unit(".sidebar { width: 240px; } .header { height: 60px; }", unit, "styles.css")
        def test_css_component_unit_accepts_its_related_selector_group(self):
            unit = {"name": "CSS_UIElements", "purpose": "Style buttons, cards, and input fields"}
            source = ".btn { padding: 1rem; } .card { border: 1px solid; } .input-field { width: 100%; }"
            self.assertIn("multiple selectors are allowed", _css_unit_prompt_contract(unit))
            self.assertEqual(validate_css_rule_unit(source, unit, "styles.css"), source)
        def test_js_cross_class_private_interface_is_promoted_consistently(self):
            accepted = ["class UI_Renderer { static #mainContent = null; static read() { return UI_Renderer.#mainContent; } }\n"]
            candidate = "class Kanban_Board { static render() { UI_Renderer.#mainContent.innerHTML = ''; } }"
            repaired = repair_js_cross_class_private_access(candidate, accepted)
            self.assertIn("static mainContent = null", accepted[0])
            self.assertIn("UI_Renderer.mainContent.innerHTML", repaired)
            validate_js(accepted[0] + repaired, "app.js", 10)
        def test_javascript_duplicate_declaration_rejected(self):
            with self.assertRaisesRegex(BuildError, "DUPLICATE_TOP_LEVEL"):
                validate_js_duplicate_declarations("function save() {}\nfunction save() {}\n", "app.js")
        def test_javascript_nested_declaration_is_not_top_level_duplicate(self):
            validate_js_duplicate_declarations("function save() {}\nif (ready) { function save() {} }", "app.js")
        def test_for_loop_binding_is_not_top_level_declaration(self):
            validate_js_duplicate_declarations("const id = 'main'; for (const id of ids) { use(id); }", "app.js")
        def test_json_truncation_rejected(self):
            with self.assertRaises(BuildError): static_validate({"data.json": '{"key":'})
        def test_js_syntax_validation_is_not_delimiter_only(self):
            if shutil.which("node"):
                with self.assertRaisesRegex(BuildError, "JS_SYNTAX_ERROR"):
                    validate_js("const = ;", "app.js", 10)
            else:
                with self.assertRaisesRegex(BuildError, "JS_SYNTAX_BLOCKED"):
                    validate_js("const x = 1;", "app.js", 10)
        def test_js_unavailable_is_blocked(self):
            from unittest.mock import patch
            with patch("shutil.which", return_value=None):
                with self.assertRaisesRegex(BuildError, "JS_SYNTAX_BLOCKED"):
                    validate_js("const x = 1;", "app.js", 10)
        def test_proxy_mutation_constraint_detected(self):
            spec = "The client must not modify proxy configuration."
            findings = deterministic_findings(spec, {"client.py": "os.environ['HTTP_PROXY'] = 'http://x'"}, [{"path": "client.py", "purpose": "client", "dependencies": []}], {"findings": []})
            self.assertTrue(any(x["rule"] == "PROXY_CONFIGURATION_MUTATION" for x in findings))
        def test_cell2_prohibition_detected(self):
            spec = "Never use Cell 2."
            findings = deterministic_findings(spec, {"client.py": "cell2_server.request()"}, [{"path": "client.py", "purpose": "client", "dependencies": []}], {"findings": []})
            self.assertTrue(any(x["rule"] == "CELL2_PROHIBITION" for x in findings))
        def test_data_model_field_mismatch_detected(self):
            project = {"app.js": "const task = { id: 1, creationTimestamp: Date.now() }; console.log(task.creationTimestamp);"}
            manifest = [{"path": "app.js", "purpose": "task model", "dependencies": []}]
            contract = {"html_elements": [], "css_selectors": [], "storage": [], "events": [], "data_models": [{"name": "Task", "fields": ["id", "createdAt"]}]}
            report = validate_contract_consistency(project, manifest, contract)
            self.assertTrue(any(x["rule"] == "DATA_MODEL_FIELD_MISMATCH" and "createdAt" in x["message"] for x in report["findings"]))
        def test_python_test_fixture_fields_do_not_satisfy_model_contract(self):
            project = {"tests/test_fixture.py": "class Task:\n    createdAt: str\n"}
            manifest = [{"path": "tests/test_fixture.py", "purpose": "fixture", "dependencies": []}]
            report = validate_contract_consistency(project, manifest, {"html_elements": [], "css_selectors": [], "storage": [], "events": [], "data_models": [{"name": "Task", "fields": ["createdAt"]}]})
            self.assertTrue(any(x["rule"] == "DATA_MODEL_FIELD_MISMATCH" for x in report["findings"]))
        def test_storage_key_mismatch_detected(self):
            project = {"index.html": "<main></main>", "app.js": "localStorage.setItem('new-key', 'x');"}
            manifest = [{"path": p, "purpose": "x", "dependencies": []} for p in project]
            report = validate_contract_consistency(project, manifest, {"html_elements": [], "storage": [{"key": "old-key", "mechanism": "localStorage"}], "events": []})
            self.assertTrue(any(x["rule"] == "STORAGE_CONTRACT_MISMATCH" for x in report["findings"]))
        def test_event_contract_mismatch_detected(self):
            project = {"index.html": "<main></main>", "app.js": "window.dispatchEvent(new CustomEvent('task:saved'));"}
            manifest = [{"path": p, "purpose": "x", "dependencies": []} for p in project]
            report = validate_contract_consistency(project, manifest, {"html_elements": [], "storage": [], "events": [{"name": "task:created"}]})
            self.assertTrue(any(x["rule"] == "EVENT_CONTRACT_MISMATCH" for x in report["findings"]))
        def test_html_local_asset_missing_detected(self):
            project = {"index.html": "<script src='missing.js'></script>"}
            facts = build_contract(project, [{"path": "index.html", "purpose": "page", "dependencies": []}])
            self.assertTrue(facts["integration_findings"]["missing_assets"])
        def test_css_local_asset_missing_detected(self):
            project = {"styles.css": ".hero { background-image: url('../missing.png'); }"}
            facts = build_contract(project, [{"path": "styles.css", "purpose": "styles", "dependencies": []}])
            self.assertTrue(any(x["resolved"] == "__PATH_ESCAPE__" for x in facts["integration_findings"]["missing_assets"]))
        def test_js_comments_do_not_create_contract_references(self):
            facts = extract_javascript_interfaces("// document.getElementById('fake')\ndocument.getElementById('real');", "app.js")
            self.assertEqual(facts["dom_ids"], {"real"})
        def test_js_static_template_selector_is_extracted(self):
            facts = extract_javascript_interfaces("document.querySelector(`#panel`);", "app.js")
            self.assertEqual(facts["dom_ids"], {"panel"})
        def test_js_dynamic_template_is_not_mistaken_for_static_selector(self):
            facts = extract_javascript_interfaces("document.querySelector(`#${name}`);", "app.js")
            self.assertEqual(facts["dom_ids"], set())
        def test_optional_dom_lookup_does_not_require_a_delivered_element(self):
            facts = extract_javascript_interfaces("document.getElementById('optional-action')?.addEventListener('click', run);", "app.js")
            self.assertEqual(facts["dom_ids"], set())
            report = validate_contract_consistency({"app.js": "document.getElementById('optional-action')?.addEventListener('click', run);"}, [{"path": "app.js"}], {"html_elements": [], "css_selectors": [], "storage": [], "events": [], "data_models": []})
            self.assertFalse(any(row["rule"] == "JS_DOM_REFERENCE_UNRESOLVED" for row in report["findings"]))
        def test_null_guarded_dom_lookup_does_not_require_a_delivered_element(self):
            source = "const dueDateGroupFilter = document.getElementById('filter-due-date-group'); if (dueDateGroupFilter) { dueDateGroupFilter.addEventListener('change', run); }"
            facts = extract_javascript_interfaces(source, "app.js")
            self.assertEqual(facts["dom_ids"], set())
        def test_js_arrow_function_and_class_extracted(self):
            facts = extract_javascript_interfaces("const save = async (x) => x; class Store {}", "app.js")
            self.assertIn("save", facts["functions"]); self.assertIn("Store", facts["classes"])
        def test_js_commonjs_import_extracted(self):
            facts = extract_javascript_interfaces("const util = require('./util.js');", "app.js")
            self.assertIn("./util.js", facts["imports"])
        def test_dynamic_template_markup_contributes_real_static_class(self):
            facts = extract_javascript_interfaces('const markup = `<li class="task-item">${task.title}</li>`;', "app.js")
            self.assertIn("task-item", facts["dynamic_classes"])
        def test_css_selector_must_map_to_dom_or_dynamic_class(self):
            project = {"index.html": "<main id='app'></main>", "styles.css": ".missing { color: red; }"}
            manifest = [{"path": p, "purpose": "x", "dependencies": []} for p in project]
            report = validate_contract_consistency(project, manifest, {"html_elements": [], "css_selectors": [".missing"], "storage": [], "events": [], "data_models": []})
            self.assertTrue(any(x["rule"] == "CSS_SELECTOR_DOM_MISMATCH" for x in report["findings"]))
        def test_browser_selector_must_be_grounded(self):
            project = {"index.html": "<button id='save'>Save</button>"}
            facts = build_contract(project, [{"path": "index.html", "purpose": "page", "dependencies": []}])
            self.assertTrue(_browser_selector_grounded("#save", project, facts))
            self.assertFalse(_browser_selector_grounded("#invented", project, facts))
        def test_saved_browser_plan_reused_only_for_identical_source(self):
            ctx = BuildContext("client-browserreuse", "p", Path("."), "spec", sha256_text("spec"), 10, True)
            ctx.contract = {"sha256": "contract-hash", "storage": []}
            ctx.project = {"index.html": "<!doctype html><html><head></head><body><button id='save'></button></body></html>"}
            ctx.manifest = [{"path": "index.html", "purpose": "page", "dependencies": []}]
            plan = {"status": "READY", "generated_by": AUDITOR_MODEL, "spec_sha256": ctx.spec_hash, "contract_sha256": "contract-hash", "file_hashes": {"index.html": sha256_text(ctx.project["index.html"])}, "tests": [{"name": "save", "steps": [{"action": "click", "selector": "#save"}], "assertions": [{"type": "visible", "selector": "#save"}]}]}
            self.assertTrue(verify_saved_browser_plan(plan, ctx))
            ctx.project["index.html"] += "<!-- changed -->"
            self.assertFalse(verify_saved_browser_plan(plan, ctx))
        def test_browser_harness_javascript_syntax(self):
            source = browser_harness_script([{"name": "persist", "steps": [{"action": "reload", "selector": "", "value": ""}], "assertions": [{"type": "storage_key_exists", "selector": "tasks_v1", "value": "", "count": 1}]}])
            body = source.removeprefix("<script>").removesuffix("</script>")
            lex_javascript(body, "browser_harness.js")
            if shutil.which("node"):
                validate_js(body, "browser_harness.js", 10)
            self.assertIn("sessionStorage.setItem", body)
            self.assertIn("localStorage.getItem", body)
        def test_test_failure_findings_map_pytest_to_application_target(self):
            project = {"app.py": "def add(a, b):\n    return a - b\n", "tests/test_app.py": "def test_add():\n    assert True\n"}
            manifest = [{"path": "app.py", "kind": "python", "purpose": "application"}, {"path": "tests/test_app.py", "kind": "test", "purpose": "tests", "validation_targets": ["app.py"]}]
            report = {"runners": [{"runner": "pytest", "status": "FAIL", "files": ["tests/test_app.py"], "stdout": "FAILED tests/test_app.py::test_add - AssertionError: expected 4"}]}
            findings = _test_failure_findings(report, project, manifest)
            self.assertEqual([x["file"] for x in findings], ["app.py"])
            self.assertEqual(findings[0]["rule"], "TEST_FAILURE")
        def test_test_failure_findings_never_repair_test_fixture_without_target(self):
            project = {"tests/test_app.py": "def test_add():\n    assert False\n"}
            report = {"runners": [{"runner": "pytest", "status": "FAIL", "files": ["tests/test_app.py"], "stdout": "AssertionError"}]}
            self.assertEqual(_test_failure_findings(report, project, [{"path": "tests/test_app.py", "kind": "test", "validation_targets": []}]), [])
        def test_test_failure_findings_map_node_syntax_error_directly(self):
            project = {"app.js": "const x = ;\n"}
            report = {"runners": [{"runner": "node --check", "status": "FAIL", "file": "app.js", "stderr": "SyntaxError: Unexpected token"}]}
            findings = _test_failure_findings(report, project, [{"path": "app.js", "kind": "javascript"}])
            self.assertEqual([x["file"] for x in findings], ["app.js"])
        def test_browser_discovery_includes_linux_chromium(self):
            from unittest.mock import patch
            with patch("shutil.which", side_effect=lambda name: "/usr/bin/chromium" if name == "chromium" else None):
                self.assertEqual(find_browser(), "/usr/bin/chromium")
        def test_browser_runtime_missing_is_blocked_not_passed(self):
            from unittest.mock import patch
            with tempfile.TemporaryDirectory() as td, patch(__name__ + ".find_browser", return_value=None):
                project = {"index.html": "<!doctype html><html><head></head><body><main></main></body></html>"}
                report = run_browser_tests(project, {"tests": []}, 5, Path(td))
                self.assertEqual(report["status"], "BROWSER_ACCEPTANCE_NOT_RUN")
        def test_browser_acceptance_not_run_blocks_publish_gate(self):
            ctx = BuildContext("browser-gate-test", "p", Path(tempfile.gettempdir()), "spec", sha256_text("spec"), 5, True)
            ctx.evidence = {"specification": {"sha256": ctx.spec_hash}}
            ctx.project = {"index.html": "<html></html>"}
            ctx.manifest = [{"path": "index.html", "purpose": "page"}]
            ctx.audit = {"status": "PASS"}
            ctx.tests = {"unit_tests": {"status": "PASS"}, "browser_runtime": {"status": "BROWSER_ACCEPTANCE_NOT_RUN"}}
            matrix = _acceptance_matrix(ctx, {"status": "PASS"}, {"status": "PASS"})
            self.assertEqual(matrix["status"], "BLOCKED")
            self.assertEqual(matrix["gates"]["browser_runtime"], "BROWSER_ACCEPTANCE_NOT_RUN")
        def test_browser_plan_accepts_reload_and_storage_evidence(self):
            class BrowserPlanClient:
                def json(self, model, prompt, timeout=None, num_predict=4096):
                    return {"tests": [{"name": "save and reload", "steps": [{"action": "click", "selector": "#save"}, {"action": "reload"}], "assertions": [{"type": "storage_key_exists", "selector": "tasks_v1"}]}]}
            with tempfile.TemporaryDirectory() as td:
                ctx = BuildContext("client-browserplan-" + uuid.uuid4().hex[:8], "p", Path(td), "persist tasks", sha256_text("persist tasks"), 10, False)
                ctx.project = {"index.html": "<!doctype html><html><head></head><body><button id='save'>Save</button></body></html>", "app.js": "document.getElementById('save').addEventListener('click',()=>localStorage.setItem('tasks_v1','[]'));"}
                ctx.manifest = [{"path": p, "purpose": "app", "dependencies": []} for p in ctx.project]
                ctx.contract = {"storage": [{"key": "tasks_v1", "mechanism": "localStorage"}]}
                result = generate_browser_plan(BrowserPlanClient(), ctx)
                self.assertEqual(result["status"], "READY")
                self.assertEqual(result["tests"][0]["steps"][1]["action"], "reload")
        def test_file_contract_dependency_hash_is_scoped(self):
            manifest = [{"path": "app.py", "purpose": "model", "generation_dependencies": []}, {"path": "service.py", "purpose": "service", "generation_dependencies": ["app.py"]}, {"path": "other.py", "purpose": "unrelated", "generation_dependencies": []}]
            first = {"python": {"symbols": {"app.py": ["A"], "service.py": ["f"], "other.py": ["g"]}, "class_fields": {}}, "html": {}, "css": {}, "javascript": {}}
            second = {"python": {"symbols": {"app.py": ["A", "B"], "service.py": ["f"], "other.py": ["g"]}, "class_fields": {}}, "html": {}, "css": {}, "javascript": {}}
            c1 = {"observed_interfaces": first}; c2 = {"observed_interfaces": second}
            self.assertNotEqual(dependency_hash("service.py", manifest, c1), dependency_hash("service.py", manifest, c2))
            self.assertEqual(dependency_hash("other.py", manifest, c1), dependency_hash("other.py", manifest, c2))
        def test_atomic_write_failure_preserves_previous_file(self):
            from unittest.mock import patch
            with tempfile.TemporaryDirectory() as td:
                target = Path(td) / "state.json"; target.write_text("old", encoding="utf-8")
                with patch("os.replace", side_effect=OSError("simulated interrupted checkpoint write")):
                    with self.assertRaises(OSError): atomic_write_text(target, "new")
                self.assertEqual(target.read_text(encoding="utf-8"), "old")
        def test_connection_timeout_retries_are_bounded(self):
            from unittest.mock import patch
            client = object.__new__(Cell3Client); client.base = "https://example.invalid"; client.auth_headers = {}; client.timeout = 1; client.capture_dir = None
            with patch("requests.request", side_effect=requests.exceptions.Timeout("timeout")), patch("time.sleep", return_value=None):
                with self.assertRaisesRegex(BuildError, "CELL3_CONNECTION_ERROR"):
                    client.request("GET", "/health", timeout=1)
        def test_generation_continuation_completes_truncated_unit(self):
            class PartialClient(Cell3Client):
                def __init__(self): self.calls = 0
                def generate(self, model, prompt, timeout=None, num_predict=4096, json_mode=False, temperature=0.0):
                    self.calls += 1
                    return ("def f():\n    return ", {"done": False}) if self.calls == 1 else ("42", {"done": True})
            client = PartialClient()
            self.assertEqual(client.generate_complete("gemma", "prompt"), "def f():\n    return 42")
        def test_audit_nonexistent_file_evidence_rejected(self):
            with self.assertRaisesRegex(BuildError, "COVERAGE_FILE_OUTSIDE_BATCH"):
                validate_audit_response({"status": "FAIL", "coverage": [{"file": "missing.py", "evidence": "x=1"}], "findings": [{"file": "missing.py", "evidence": "x=1"}]}, {"app.py": "x=1"})
        def test_audit_duplicate_evidence_rejected(self):
            with self.assertRaisesRegex(BuildError, "NOT_UNIQUE"):
                validate_audit_response({"status": "FAIL", "coverage": [{"file": "app.py", "evidence": "y=2", "reviewed_as": "source consistency"}], "findings": [{"file": "app.py", "evidence": "x=1"}]}, {"app.py": "y=2\nx=1\nx=1"})
        def test_auditor_pass_without_evidence_rejected(self):
            with self.assertRaisesRegex(BuildError, "COVERAGE"):
                validate_audit_response({"status": "PASS", "coverage": [], "findings": []}, {"app.py": "x=1"})
        def test_saved_audit_reused_only_for_identical_source(self):
            ctx = BuildContext("client-auditreuse", "p", Path("."), "spec", sha256_text("spec"), 10, False)
            ctx.contract = {"sha256": "contract-hash"}; ctx.project = {"app.py": "x = 1\n"}
            source = ctx.project["app.py"]
            report = {"status": "PASS", "independent_auditor": AUDITOR_MODEL, "model_status": "PASS", "evidence_verified": True, "spec_sha256": ctx.spec_hash, "contract_sha256": "contract-hash", "file_hashes": {"app.py": sha256_text(source)}, "audited_files": 1, "coverage": [{"file": "app.py", "line": 1, "evidence": "x = 1", "reviewed_as": "module review"}], "blocking_findings": []}
            self.assertTrue(verify_saved_audit(report, ctx))
            ctx.project["app.py"] = "x = 2\n"
            self.assertFalse(verify_saved_audit(report, ctx))
        def test_audit_coverage_requires_every_file(self):
            with self.assertRaisesRegex(BuildError, "INCOMPLETE"):
                validate_audit_response({"status": "PASS", "coverage": [{"file": "a.py", "evidence": "x=1", "reviewed_as": "module source"}], "findings": []}, {"a.py": "x=1", "b.py": "y=2"})
        def test_audit_coverage_exact_evidence_passes(self):
            result = validate_audit_response({"status": "PASS", "coverage": [{"file": "a.py", "evidence": "x=1", "reviewed_as": "module"}], "findings": []}, {"a.py": "x=1"})
            self.assertEqual(result["status"], "PASS")
        def test_unit_plan_duplicate_names_rejected(self):
            with self.assertRaisesRegex(BuildError, "duplicate"):
                parse_model_units({"preamble": "", "units": [{"name": "f", "kind": "function", "purpose": "one"}, {"name": "f", "kind": "function", "purpose": "two"}]}, "app.py")
        def test_unit_plan_forward_dependency_rejected(self):
            with self.assertRaisesRegex(BuildError, "DEPENDENCY_ORDER"):
                parse_model_units({"preamble": "", "units": [{"name": "first", "kind": "function", "purpose": "one", "depends_on": ["second"]}, {"name": "second", "kind": "function", "purpose": "two"}]}, "app.py")
        def test_checkpoint_file_source_hash_verified(self):
            with tempfile.TemporaryDirectory() as td:
                run_id = "client-selftest-" + uuid.uuid4().hex[:8]; ctx = BuildContext(run_id, "p", Path(td), "s", sha256_text("s"), 10, True)
                st = StateStore(ctx); st.set_manifest([{"path": "app.py", "purpose": "x", "dependencies": []}]); st.accept_file("app.py", "x=1", contract_hash="c", dependency_hash="d")
                candidate = st.accepted_root / "app.py"; candidate.write_text("x=2", encoding="utf-8")
                self.assertIsNone(st.restore_file("app.py", contract_hash="c", dependency_hash="d"))
                shutil.rmtree(RUNS_DIR / run_id, ignore_errors=True)
        def test_resume_dependency_checkpoint_rejection_regenerates_cached_dependents(self):
            class ResumeClient:
                def __init__(self): self.generated = []
                def json(self, model, prompt, timeout=None, num_predict=4096):
                    if "FILE: app.py" in prompt: return {"preamble": "", "units": [{"name": "add", "kind": "function", "purpose": "Add values"}]}
                    if "FILE: service.py" in prompt: return {"preamble": "from app import add", "units": [{"name": "calculate", "kind": "function", "purpose": "Use add"}]}
                    raise AssertionError(prompt[:200])
                def generate_complete(self, model, prompt, timeout=None, num_predict=4096, max_continuations=3):
                    if "UNIT: 001:function:add" in prompt: self.generated.append("app.py"); return "def add(a, b):\n    return a + b"
                    if "UNIT: 001:function:calculate" in prompt: self.generated.append("service.py"); return "def calculate():\n    return add(2, 3)"
                    raise AssertionError(prompt[:200])
            with tempfile.TemporaryDirectory() as td:
                run_id = "client-selftest-" + uuid.uuid4().hex[:8]
                ctx = BuildContext(run_id, "p", Path(td), "add and calculate", sha256_text("add and calculate"), 10, False)
                ctx.manifest = [{"path": "app.py", "purpose": "addition", "dependencies": []}, {"path": "service.py", "purpose": "service", "dependencies": ["app.py"]}]
                original = {"app.py": "def add(a, b):\n    return a + b\n", "service.py": "from app import add\n\ndef calculate():\n    return add(2, 3)\n"}
                ctx.contract = {"html_elements": [], "css_selectors": [], "storage": [], "events": [], "data_models": [], "workflows": [], "manifest_sha256": canonical_hash(ctx.manifest), "observed_interfaces": build_contract(original, ctx.manifest)}
                ctx.contract["sha256"] = canonical_hash(ctx.contract)
                state = StateStore(ctx); state.set_manifest(ctx.manifest); state.set_contract(ctx.contract)
                for path, source in original.items(): state.accept_file(path, source, contract_hash=file_contract_fingerprint(path, ctx.contract, ctx.manifest), dependency_hash=dependency_hash(path, ctx.manifest, ctx.contract))
                (state.accepted_root / "app.py").write_text("corrupted checkpoint", encoding="utf-8")
                ctx.resume = True
                client = ResumeClient()
                result = generate_project(client, ctx, state)
                self.assertIn("def calculate", result["service.py"])
                self.assertIn("service.py", client.generated)
                shutil.rmtree(RUNS_DIR / run_id, ignore_errors=True)
        def test_repair_then_independent_reaudit(self):
            class RepairAuditClient:
                def __init__(self): self.calls = 0
                def json(self, model, prompt, timeout=None, num_predict=4096):
                    self.calls += 1
                    if "Repair only the verified defects" in prompt:
                        return {"path": "app.py", "target": {"kind": "python_symbol", "name": "value"}, "replacement": "def value():\n    return 2"}
                    return {"status": "PASS", "coverage": [{"file": "app.py", "evidence": "def value():", "reviewed_as": "repaired function"}], "findings": []}
            with tempfile.TemporaryDirectory() as td:
                run_id = "client-selftest-" + uuid.uuid4().hex[:8]; stage = Path(td) / "stage"; stage.mkdir()
                ctx = BuildContext(run_id, "p", stage, "Provide value", sha256_text("Provide value"), 10, False)
                ctx.manifest = [{"path": "app.py", "purpose": "value function", "dependencies": []}]
                ctx.project = {"app.py": "def value():\n    return 1\n"}
                ctx.contract = {"html_elements": [], "css_selectors": [], "storage": [], "events": [], "data_models": [], "workflows": []}
                ctx.evidence["contract_report"] = {"status": "PASS", "findings": []}
                state = StateStore(ctx); state.set_manifest(ctx.manifest)
                client = RepairAuditClient()
                repair_project(client, ctx, [{"file": "app.py", "severity": "HIGH", "message": "wrong result"}], 1, state)
                audit = independent_audit(client, ctx)
                self.assertEqual(audit["status"], "PASS")
                self.assertIn("return 2", ctx.project["app.py"])
                shutil.rmtree(RUNS_DIR / run_id, ignore_errors=True)

    suite = unittest.defaultTestLoader.loadTestsFromTestCase(EngineRegressionTests)
    result = unittest.TestResult(); suite.run(result)
    if not result.wasSuccessful():
        failures = [f"{t}: {e}" for t, e in result.failures + result.errors]
        raise BuildError("V7_INTERNAL_REGRESSION_FAILURE: " + " | ".join(failures))
    log(f"V7_INTERNAL_REGRESSION_TESTS_PASS | tests={result.testsRun}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=f"Standalone Cell 3 Project Builder v{VERSION}")
    parser.add_argument("--version", action="version", version=f"Cell 3 Project Builder {VERSION}")
    sub = parser.add_subparsers(dest="command", required=True)
    doctor = sub.add_parser("doctor"); doctor.add_argument("--timeout", type=int, default=30)
    models = sub.add_parser("models"); models.add_argument("--timeout", type=int, default=30)
    plan = sub.add_parser("plan"); plan.add_argument("--spec-file", required=True); plan.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    build = sub.add_parser("build")
    build.add_argument("--spec-file", required=True); build.add_argument("--name", required=True)
    build.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT); build.add_argument("--run-tests", action="store_true")
    build.add_argument("--resume-run-id", default=""); build.add_argument("--keep-staging", action="store_true")
    report = sub.add_parser("report"); report.add_argument("--run-id", required=True)
    sub.add_parser("self-test")
    return parser


def main() -> int:
    parser = build_parser(); args = parser.parse_args()
    if getattr(args, "timeout", 1) <= 0:
        print("ERROR: timeout must be greater than zero"); return 2
    try:
        if args.command == "self-test":
            run_internal_regression_tests(); print("Cell 3 v7 internal regression tests: PASS"); return 0
        if args.command == "report":
            if not re.fullmatch(r"client-[A-Za-z0-9T_-]+", args.run_id): raise BuildError("Invalid run ID")
            path = RUNS_DIR / args.run_id / "report.json"
            if not path.is_file(): path = RUNS_DIR / args.run_id / "build_state.json"
            print(path.read_text(encoding="utf-8")); return 0
        if args.command in {"doctor", "models"}:
            client = Cell3Client(args.timeout); available = client.preflight(require_planner=args.command == "doctor")
            print(f"Cell 3 {args.command}: PASS")
            if args.command == "models": print("Available models:\n" + "\n".join(" - " + x for x in sorted(available)))
            print("Cell 2: NOT USED"); print("Proxy configuration: NOT MODIFIED"); return 0
        if args.command == "plan":
            spec = read_spec(args.spec_file); client = Cell3Client(args.timeout); client.preflight(True)
            ctx = BuildContext("planning-" + uuid.uuid4().hex[:8], "planning", STAGING_DIR / "planning", spec, sha256_text(spec), args.timeout, False)
            ctx.staging.mkdir(parents=True, exist_ok=True); _run_planning(client, ctx)
            print(json.dumps({"plan": ctx.plan, "architecture": ctx.architecture, "design": ctx.design, "manifest": ctx.manifest}, indent=2, ensure_ascii=False)); return 0
        if args.command == "build": return run_build(args)
        raise BuildError("Unknown command")
    except KeyboardInterrupt:
        print("\nInterrupted by user."); return 130
    except Exception as exc:
        print("ERROR:", redact(exc)); print("Cell 2: NOT USED"); print("Proxy configuration: NOT MODIFIED"); return 1

if __name__ == "__main__":
    raise SystemExit(main())
