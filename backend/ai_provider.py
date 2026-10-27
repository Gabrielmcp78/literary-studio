"""Provider routing for Literary Studio's explicit AI operations.

Gemini remains the primary provider when it is configured. Ollama's GPT-OSS
120B cloud model is the preferred configured fallback, followed by AGY as the
terminal-level fallback. Intake uses this router only when the intake oversight
audit is explicitly run; it never silently rewrites manuscript prose.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from config import load_environment

load_environment()

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
    """A provider reply plus provenance.

    ``fallback_reasons`` lists why earlier providers in the chain were skipped,
    so a batch records the provider that actually ran and why.
    """

    text: str
    provider: str
    model: str
    fallback_reasons: tuple[str, ...] = ()


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
    configured_model = os.getenv("LITERARY_STUDIO_AGY_MODEL", "claude-opus-5-5-high")
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
    return AIResponse(text=text.strip(), provider="agy", model=configured_model or "agy CLI default model")


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
    model = os.getenv("LITERARY_STUDIO_OLLAMA_MODEL", "gpt-oss:120b-cloud")
    num_ctx = max(int(os.getenv("LITERARY_STUDIO_OLLAMA_NUM_CTX", "131072")), 8192)
    request_body: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"num_ctx": num_ctx},
    }
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


def _run_openrouter(
    prompt: str,
    system_instruction: str | None = None,
    temperature: float = 0.2,
) -> AIResponse:
    """Call OpenRouter's OpenAI-compatible chat-completions endpoint."""
    api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        raise AIProviderUnavailable("OpenRouter is not configured: OPENROUTER_API_KEY is empty.")

    model = os.getenv(
        "LITERARY_STUDIO_OPENROUTER_MODEL",
        "nvidia/nemotron-3-ultra-550b-a55b:free",
    )
    messages: list[dict[str, str]] = []
    if system_instruction:
        messages.append({"role": "system", "content": system_instruction})
    messages.append({"role": "user", "content": prompt})
    request_body = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    request = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=json.dumps(request_body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "http://localhost:3000",
            "X-OpenRouter-Title": "Literary Studio",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(
            request,
            timeout=max(int(os.getenv("LITERARY_STUDIO_OPENROUTER_TIMEOUT", "180")), 1),
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[-1000:]
        raise AIProviderUnavailable(f"OpenRouter returned HTTP {exc.code}: {detail}") from exc
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise AIProviderUnavailable(f"OpenRouter unavailable: {exc}") from exc

    if payload.get("error"):
        raise AIProviderUnavailable(f"OpenRouter returned an error: {payload['error']}")
    try:
        text = str(payload["choices"][0]["message"]["content"]).strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise AIProviderUnavailable("OpenRouter returned no assistant message.") from exc
    if not text:
        raise AIProviderUnavailable("OpenRouter returned an empty response.")
    return AIResponse(text=text, provider="openrouter", model=model)


def generate_text(
    prompt: str,
    *,
    model: str = "gemini-3.1-pro-preview",
    temperature: float = 0.2,
    system_instruction: str | None = None,
    preferred_provider: str | None = None,
    allowed_providers: tuple[str, ...] | None = None,
) -> AIResponse:
    """Route across providers: Gemini, OpenRouter, Ollama GPT-OSS, AGY, and FModCLI.

    If preferred_provider is specified (or LITERARY_STUDIO_DEFAULT_PROVIDER is set),
    that provider is attempted first, falling back to the remaining chain on failure.
    """
    chosen = (
        (preferred_provider or os.getenv("LITERARY_STUDIO_DEFAULT_PROVIDER", "")).strip().lower()
    )
    if chosen in {"", "auto"}:
        chosen = "auto"

    def _call_gemini() -> AIResponse:
        if genai is None or types is None:
            raise AIProviderUnavailable("Gemini: google-genai is not installed.")
        if not (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")):
            raise AIProviderUnavailable("Gemini: GEMINI_API_KEY / GOOGLE_API_KEY is not set.")
        client = genai.Client()
        config_kwargs: dict[str, Any] = {"temperature": temperature}
        if system_instruction:
            config_kwargs["system_instruction"] = system_instruction
        config = types.GenerateContentConfig(**config_kwargs)
        response = client.models.generate_content(model=model, contents=prompt, config=config)
        text = (response.text or "").strip()
        if not text:
            raise AIProviderUnavailable("Gemini returned an empty response.")
        return AIResponse(text=text, provider="gemini", model=model)

    provider_map = {
        "gemini": _call_gemini,
        "openrouter": lambda: _run_openrouter(prompt, system_instruction, temperature),
        "ollama": lambda: _run_ollama(prompt, system_instruction),
        "agy": lambda: _run_agy(prompt),
        "fmodcli": lambda: _run_fmod(prompt, system_instruction),
    }
    allowed = tuple(dict.fromkeys(allowed_providers or provider_map.keys()))
    unknown = [provider for provider in allowed if provider not in provider_map]
    if unknown:
        raise AIProviderUnavailable(f"Unknown AI provider restriction: {', '.join(unknown)}")
    if not allowed:
        raise AIProviderUnavailable("No AI providers are allowed for this operation.")

    if chosen != "auto" and chosen in allowed:
        ordered_providers = [chosen] + [p for p in allowed if p != chosen]
    else:
        ordered_providers = [p for p in ("gemini", "openrouter", "ollama", "agy", "fmodcli") if p in allowed]

    errors: list[str] = []
    for prov_name in ordered_providers:
        call_fn = provider_map[prov_name]
        try:
            res = call_fn()
            return replace(res, fallback_reasons=tuple(errors))
        except (AIProviderUnavailable, Exception) as exc:
            errors.append(f"{prov_name}: {exc}")

    raise AIProviderUnavailable("; ".join(errors))
