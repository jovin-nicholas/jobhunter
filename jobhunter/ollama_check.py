"""For check-config: can the configured local models run here? Is Ollama running, is each model pulled, and do they fit
in this computer's memory and disk."""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, Callable

import requests

LAYA_MEMORY_GB = 1.5          # Laya's model and PyTorch, loaded alongside any Ollama model
MEMORY_SHARE = 0.75           # leave a quarter of the memory for the system and other apps

def installed(url: str, get: Callable[..., Any] | None = None) -> dict[str, int] | str:
    """Pulled model names and their sizes in bytes, or why Ollama could not be asked."""
    try:
        resp = (get or requests.get)(f"{url.rstrip('/')}/api/tags", timeout=5)
        resp.raise_for_status()
        return {m.get("name", ""): int(m.get("size") or 0) for m in resp.json().get("models", [])}
    except Exception as e:
        return f"Ollama is not reachable at {url} ({type(e).__name__}); start the Ollama app or run `ollama serve`"


def size_of(found: dict[str, int], model: str) -> int | None:
    """The pulled size of `model` (an untagged name is Ollama's :latest), or None when it is not pulled."""
    for name in (model, f"{model}:latest"):
        if name in found:
            return found[name]
    return None


def ollama_problem(url: str, model: str, get: Callable[..., Any] | None = None) -> str | None:
    """Why `model` cannot be used from the Ollama at `url`, or None when it is there."""
    found = installed(url, get)
    if isinstance(found, str):
        return found
    if size_of(found, model) is not None:
        return None
    return f"{model} is not downloaded; run `ollama pull {model}` ({free_disk_gb():.0f} GB of disk space is free)"


def free_disk_gb() -> float:
    """Free space where Ollama keeps its models (OLLAMA_MODELS, else ~/.ollama)."""
    path = Path(os.environ.get("OLLAMA_MODELS") or Path.home() / ".ollama")
    while not path.exists() and path != path.parent:
        path = path.parent
    return shutil.disk_usage(path).free / 1e9


def memory_gb() -> float | None:
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1e9
    except (ValueError, OSError, AttributeError):
        return None


def memory_notes(sizes_gb: dict[str, float], laya: bool, total_gb: float | None) -> list[str]:
    """Notes when the models in use need more memory than this computer can spare."""
    if not total_gb or not sizes_gb:
        return []
    spare = total_gb * MEMORY_SHARE
    notes = [f"{name} needs about {gb:.0f} GB of memory and this computer has {total_gb:.0f} GB, so Ollama may refuse "
             "to load it or run it very slowly; choose a smaller model (README: Ollama)"
             for name, gb in sizes_gb.items() if gb > spare]
    need = sum(sizes_gb.values()) + (LAYA_MEMORY_GB if laya else 0)
    if not notes and len(sizes_gb) + laya > 1 and need > spare:
        names = ", ".join(sizes_gb) + (" and Laya" if laya else "")
        notes.append(f"{names} together need about {need:.0f} GB of memory and this computer has {total_gb:.0f} GB, "
                     "so they cannot all stay loaded and each run is slower while Ollama swaps them; choose smaller "
                     "models or leave one out")
    return notes
