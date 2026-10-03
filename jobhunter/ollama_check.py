"""For check-config: can the configured local models run here? Is Ollama running, is each model pulled, and do they fit
in this computer's memory and disk."""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import requests

GB = 2 ** 30
LAYA_MEMORY_GB = 1.5          # Laya's model and PyTorch, loaded alongside any Ollama model
MEMORY_SHARE = 0.75           # leave a quarter of the memory for the system and other apps
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def installed(url: str, get: Callable[..., Any] | None = None) -> dict[str, int] | str:
    """Pulled model names and their sizes in bytes, or why Ollama could not be asked."""
    try:
        resp = (get or requests.get)(f"{url.rstrip('/')}/api/tags", timeout=5)
    except requests.RequestException as e:
        return f"Ollama is not reachable at {url} ({type(e).__name__}); start the Ollama app or run `ollama serve`"
    if resp.status_code != 200:
        return f"{url} answered HTTP {resp.status_code} instead of listing Ollama models; is this the right Ollama URL?"
    try:
        models = resp.json().get("models") or []
        return {str(m["name"]): int(m.get("size") or 0) for m in models}
    except Exception as e:
        return f"{url} did not answer like Ollama ({type(e).__name__}); is this the right Ollama URL?"


def size_of(found: dict[str, int], model: str) -> int | None:
    """The pulled size of `model` (an untagged name is Ollama's :latest), or None when it is not pulled."""
    for name in (model, f"{model}:latest"):
        if name in found:
            return found[name]
    return None


def ollama_problem(url: str, model: str, get: Callable[..., Any] | None = None,
                   found: dict[str, int] | str | None = None) -> str | None:
    """Why `model` cannot be used from the Ollama at `url`, or None when it is there."""
    found = installed(url, get) if found is None else found
    if isinstance(found, str):
        return found
    if size_of(found, model) is not None:
        return None
    disk = free_disk_gb() if is_local(url) else None
    space = f" ({disk:.0f} GB of disk space is free)" if disk is not None else ""
    return f"{model} is not downloaded; run `ollama pull {model}`{space}"


def is_local(url: str) -> bool:
    """Ollama on this computer, so this computer's memory and disk are the ones that matter."""
    return (urlparse(url).hostname or "localhost") in LOCAL_HOSTS


def free_disk_gb() -> float | None:
    """Free space where Ollama keeps its models (OLLAMA_MODELS, else ~/.ollama), or None when it cannot be read."""
    try:
        path = Path(os.environ.get("OLLAMA_MODELS") or Path.home() / ".ollama")
        while not path.exists() and path != path.parent:
            path = path.parent
        return shutil.disk_usage(path).free / GB
    except OSError:
        return None


def memory_gb() -> float | None:
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / GB
    except (ValueError, OSError, AttributeError):
        return None


def memory_notes(sizes_gb: dict[str, float], laya: bool, total_gb: float | None) -> list[str]:
    """Notes when the models that run together need more memory than this computer can spare."""
    if not total_gb or not (sizes_gb or laya):
        return []
    spare = total_gb * MEMORY_SHARE
    room = f"more than the {spare:.0f} GB of {total_gb:.0f} GB this computer can spare"
    notes = [f"{name} needs about {gb:.0f} GB of memory, {room}, so Ollama may refuse to load it or run it very "
             "slowly; choose a smaller model (README: Ollama)" for name, gb in sizes_gb.items() if gb > spare]
    need = sum(sizes_gb.values()) + (LAYA_MEMORY_GB if laya else 0)
    if not notes and len(sizes_gb) + laya > 1 and need > spare:
        names = ", ".join(sizes_gb) + (" and Laya" if laya else "")
        notes.append(f"{names} run together and need about {need:.0f} GB of memory, {room}, so each run is slower "
                     "while Ollama swaps them in and out; choose smaller models or leave one out")
    return notes
