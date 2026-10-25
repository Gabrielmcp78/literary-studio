"""Provider routing for Literary Studio's explicit AI operations.

Gemini remains the primary provider when it is configured. AGY is the
terminal-level fallback: it is invoked in print/sandbox mode. Intake uses
this router only when the intake oversight audit is explicitly run; it never
silently rewrites manuscript prose.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    from google import genai
    from google.genai import types
except ImportError:  # Keep local deterministic routes available without Gemini.
    genai = None
    types = None


class AIProviderUnavailable(RuntimeError):
    """Raised when neither the primary nor terminal fallback is available."""


@dataclass(frozen=True)
class AIResponse:
    text: str
    provider: str
    model: str


def _executable(
    configured_names: tuple[str, ...],
    name: str,
    candidates: tuple[Path, ...] = (),
) -> str | None:
    configured = next((os.getenv(key) for key in configured_names if os.getenv(key)), None)
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_file() and candidate.stat().st_mode & 0o111:
            return str(candidate)
    discovered = shutil.which(name)
    if discovered:
        return discovered
    for candidate in candidates:
        if candidate.is_file() and candidate.stat().st_mode & 0o111:
            return str(candidate)
    return None


def _agy_path() -> str | None:
    return _executable(
        ("LITERARY_STUDIO_AGY_BIN", "AGY_BIN"),
        "agy",
        (Path.home() / ".local" / "bin" / "agy",),
    )


def _fmod_path() -> str | None:
    return _executable(
        ("LITERARY_STUDIO_FMODCLI_BIN", "FMODCLI_BIN"),
        "fmodcli",
        (Path.home() / ".local" / "bin" / "fmodcli", Path("/usr/local/bin/fmodcli")),
    )


def _response_text(payload: Any) -> str:
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        for key in ("response", "result", "content", "output", "text"):
            value = payload.get(key)
            if isinstance(value, str):
                return value
        for value in payload.values():
            text = _response_text(value)
            if text:
                return text
    if isinstance(payload, list):
        for value in reversed(payload):
            text = _response_text(value)
            if text:
                return text
    return ""


def _run_agy(prompt: str) -> AIResponse:
    executable = _agy_path()
    if not executable:
        raise AIProviderUnavailable(
            "AGY fallback is unavailable. Set LITERARY_STUDIO_AGY_BIN or install agy."
        )

    timeout = os.getenv("LITERARY_STUDIO_AGY_TIMEOUT", "120s")
    command = [
        executable,
        f"--print={prompt}",
        "--output-format",
        "json",
        "--disable-slash-commands",
        "--sandbox",
        "--print-timeout",
        timeout,
    ]
    configured_model = os.getenv("LITERARY_STUDIO_AGY_MODEL")
    if configured_model:
        command.extend(["--model", configured_model])

    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=max(int(os.getenv("LITERARY_STUDIO_AGY_PROCESS_TIMEOUT", "150")), 1),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AIProviderUnavailable(f"AGY fallback failed to start or timed out: {exc}") from exc

    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "unknown AGY error"
        raise AIProviderUnavailable(f"AGY fallback returned {completed.returncode}: {detail[-1000:]}")

    raw = completed.stdout.strip()
    try:
        payload = json.loads(raw)
        text = _response_text(payload)
    except json.JSONDecodeError:
        text = raw
    if not text.strip():
        raise AIProviderUnavailable("AGY fallback returned an empty response.")
    return AIResponse(text=text.strip(), provider="agy", model=configured_model or "agy")


def _run_fmod(prompt: str, system_instruction: str | None = None) -> AIResponse:
    executable = _fmod_path()
    if not executable:
        raise AIProviderUnavailable(
            "fmodcli fallback is unavailable. Set LITERARY_STUDIO_FMODCLI_BIN or install fmodcli."
        )
    command = [executable, "run", prompt, "--output", "json"]
    if system_instruction:
        command.extend(["--system", system_instruction])
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=max(int(os.getenv("LITERARY_STUDIO_FMOD_PROCESS_TIMEOUT", "120")), 1),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AIProviderUnavailable(f"fmodcli fallback failed to start or timed out: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "unknown fmodcli error"
        raise AIProviderUnavailable(f"fmodcli fallback returned {completed.returncode}: {detail[-1000:]}")
    raw = completed.stdout.strip()
    try:
        payload = json.loads(raw)
        text = _response_text(payload)
        model = str(payload.get("model", "SystemLanguageModel.default")) if isinstance(payload, dict) else "SystemLanguageModel.default"
    except json.JSONDecodeError:
        text = raw
        model = "SystemLanguageModel.default"
    if not text.strip():
        raise AIProviderUnavailable("fmodcli fallback returned an empty response.")
    return AIResponse(text=text.strip(), provider="fmodcli", model=model)


def _run_ollama(prompt: str, system_instruction: str | None = None) -> AIResponse:
    host = os.getenv("LITERARY_STUDIO_OLLAMA_HOST", os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")).rstrip("/")
    model = os.getenv("LITERARY_STUDIO_OLLAMA_MODEL", "llama3.1:8b")
    request_body: dict[str, Any] = {"model": model, "prompt": prompt, "stream": False}
    if system_instruction:
        request_body["system"] = system_instruction
    request = urllib.request.Request(
        f"{host}/api/generate",
        data=json.dumps(request_body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(
            request,
            timeout=max(int(os.getenv("LITERARY_STUDIO_OLLAMA_TIMEOUT", "180")), 1),
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
        raise AIProviderUnavailable(f"Ollama fallback unavailable: {exc}") from exc
    if payload.get("error"):
        raise AIProviderUnavailable(f"Ollama fallback returned an error: {payload['error']}")
    text = str(payload.get("response", "")).strip()
    if not text:
        raise AIProviderUnavailable("Ollama fallback returned an empty response.")
    return AIResponse(text=text, provider="ollama", model=model)


def generate_text(
    prompt: str,
    *,
    model: str,
    temperature: float = 0.2,
    system_instruction: str | None = None,
) -> AIResponse:
    """Use Gemini first, then AGY, Apple FoundationModels, and Ollama."""

    primary_error: Exception | None = None
    if genai is not None and types is not None and (
        os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    ):
        try:
            client = genai.Client()
            config_kwargs: dict[str, Any] = {"temperature": temperature}
            if system_instruction:
                config_kwargs["system_instruction"] = system_instruction
            config = types.GenerateContentConfig(**config_kwargs)
            response = client.models.generate_content(model=model, contents=prompt, config=config)
            text = (response.text or "").strip()
            if text:
                return AIResponse(text=text, provider="gemini", model=model)
            primary_error = RuntimeError("Gemini returned an empty response.")
        except Exception as exc:  # Fallback is specifically for provider failures.
            primary_error = exc

    errors: list[str] = []
    if primary_error:
        errors.append(f"Gemini: {primary_error}")
    for fallback in (
        lambda: _run_agy(prompt),
        lambda: _run_fmod(prompt, system_instruction),
        lambda: _run_ollama(prompt, system_instruction),
    ):
        try:
            return fallback()
        except AIProviderUnavailable as exc:
            errors.append(str(exc))
    raise AIProviderUnavailable("; ".join(errors))
