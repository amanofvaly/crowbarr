"""Bounded runtime discovery, isolated from the web process and model downloads."""

from __future__ import annotations

import contextlib
import copy
import multiprocessing
import os
import threading
import time

_PROBE = r'''
import ctypes
import os
import sys

result = {}
try:
    import ctranslate2
    import faster_whisper
    cpu = sorted(ctranslate2.get_supported_compute_types("cpu"))
    result["cpu"] = {"available": True, "reason": "", "compute_types": cpu}
except Exception as error:
    result["cpu"] = {"available": False, "reason": f"The CTranslate2 speech runtime is missing or cannot load ({type(error).__name__}). Install Crowbarr's inference dependencies.", "compute_types": []}
try:
    if os.environ.get("CROWBARR_IMAGE_VARIANT") == "cpu":
        raise RuntimeError("CPU image")
    if not result["cpu"]["available"]:
        raise RuntimeError("Speech runtime unavailable")
    # CTranslate2 needs cuBLAS and the host driver. It links the CUDA runtime
    # statically and uses no cuDNN, so requiring more would report a false negative.
    if sys.platform.startswith("linux"):
        ctypes.CDLL("libcublas.so.12")
    types = sorted(ctranslate2.get_supported_compute_types("cuda"))
    if not types:
        raise RuntimeError("No CUDA compute types")
    result["cuda"] = {"available": True, "reason": "", "compute_types": types}
except Exception:
    # No GPU is an ordinary state, not a fault, so this reason names no exception.
    reason = "CUDA is unavailable. Use the CUDA image or a CUDA-enabled native installation, with a compatible NVIDIA driver and GPU access."
    if os.environ.get("CROWBARR_IMAGE_VARIANT") == "cpu":
        reason = "This is the CPU image. Install the CUDA image and expose a compatible NVIDIA GPU to enable CUDA."
    result["cuda"] = {"available": False, "reason": reason, "compute_types": []}
try:
    import whisperx
    from whisperx.alignment import align, load_align_model
    result["refinement"] = {"available": True, "reason": "WhisperX is installed. Alignment models are loaded when needed; initialization can still fail."}
except Exception as error:
    result["refinement"] = {"available": False, "reason": f"WhisperX or its dependencies are missing or cannot load ({type(error).__name__}). Whisper word timestamps will be used instead."}
'''

_lock = threading.Lock()
_cached: dict | None = None
_expires = 0.0
_variant = ""

_download_lock = threading.Lock()
_downloads: dict[str, dict] = {}


def _alignment_worker(target, language):
    from whisperx.alignment import load_align_model

    load_align_model(language_code=language, device="cpu", model_dir=target)


def _speech_worker(target, name):
    from faster_whisper.utils import download_model

    download_model(name, cache_dir=target)


def download_state(name: str = "alignment") -> dict:
    with _download_lock:
        return dict(_downloads.get(name, {"status": "idle", "reason": ""}))


def start_download(directory, name: str = "alignment") -> bool:
    """Fetch one model in a child, so the web process never imports a runtime.

    Returns False when that model is already downloading, so a second click is ignored
    rather than starting a competing fetch into the same directory.
    """
    from pathlib import Path as _Path

    with _download_lock:
        if _downloads.get(name, {}).get("status") == "running":
            return False
        _downloads[name] = {"status": "running", "reason": ""}

    alignment = name == "alignment"
    models = _Path(directory) / "models"
    target = str(models / ("alignment" if alignment else "whisper"))
    worker = _alignment_worker if alignment else _speech_worker
    argument = "en" if alignment else name

    def run():
        context = multiprocessing.get_context("spawn")
        process = context.Process(target=worker, args=(target, argument), daemon=True)
        status, reason = "failed", "The download did not finish. Check network access and disk space."
        try:
            process.start()
            process.join(timeout=1800)
            if process.is_alive():
                process.kill()
                process.join(timeout=5)
                reason = "The download timed out after 30 minutes."
            elif process.exitcode == 0:
                status, reason = "ready", ""
        except (OSError, ValueError) as error:
            reason = f"The download could not start ({type(error).__name__})."
        finally:
            if process.pid is not None:
                with contextlib.suppress(ValueError):
                    process.close()
            with _download_lock:
                _downloads[name] = {"status": status, "reason": reason}

    threading.Thread(target=run, daemon=True).start()
    return True


def _probe_worker(connection):
    try:
        namespace = {}
        exec(_PROBE, namespace)
        connection.send(namespace["result"])
    finally:
        connection.close()


def _run_probe() -> dict:
    # Spawn also works in frozen installs through the entry point's freeze_support.
    context = multiprocessing.get_context("spawn")
    receive, send = context.Pipe(duplex=False)
    process = context.Process(target=_probe_worker, args=(send,), daemon=True)
    try:
        process.start()
        send.close()
        if not receive.poll(20):
            raise TimeoutError("Runtime probe timed out")
        return receive.recv()
    finally:
        receive.close()
        send.close()
        if process.pid is not None:
            process.join(timeout=1)
            if process.is_alive():
                process.terminate()
                process.join(timeout=1)
                if process.is_alive():
                    process.kill()
                    process.join(timeout=1)
            process.close()


# WhisperX names the English alignment model after its torchaudio pipeline bundle.
ALIGNMENT_MODEL = "alignment/wav2vec2_fairseq_base_ls960_asr_ls960.pth"
SPEECH_MODELS = (
    "tiny.en", "base.en", "small.en", "medium.en", "large-v3", "large-v3-turbo",
    "tiny", "base", "small", "medium",
)


def model_inventory(directory) -> dict:
    """Report which models are already downloaded. Filesystem only, no imports."""
    from pathlib import Path as _Path

    models = _Path(directory) / "models"
    speech = {}
    whisper = models / "whisper"
    if whisper.is_dir():
        for entry in whisper.iterdir():
            name, marker = entry.name, "faster-whisper-"
            if entry.is_dir() and marker in name:
                speech[name.split(marker, 1)[1]] = True
    alignment = models / ALIGNMENT_MODEL
    present = alignment.is_file()
    state = download_state()
    return {
        "speech": sorted(speech),
        "speech_downloads": {
            name: download_state(name)
            for name in SPEECH_MODELS
            if name not in speech and download_state(name)["status"] != "idle"
        },
        "alignment": {
            "present": present,
            "bytes": alignment.stat().st_size if present else 0,
            "status": "ready" if present else state["status"],
            "reason": "" if present else state["reason"],
        },
    }


def capabilities_for(directory) -> dict:
    """Runtime probe, which models are on disk, and what a change would re-check."""
    from .config import FINGERPRINT_FIELDS

    result = runtime_capabilities()
    result["models"] = model_inventory(directory)
    result["recheck_fields"] = list(FINGERPRINT_FIELDS)
    return result


def runtime_capabilities() -> dict:
    """Cache probes for five minutes; never import inference libraries in this process."""
    global _cached, _expires, _variant
    variant = os.environ.get("CROWBARR_IMAGE_VARIANT", "native")
    with _lock:
        if _cached is not None and time.monotonic() < _expires and variant == _variant:
            return copy.deepcopy(_cached)
        try:
            result = _run_probe()
            if not all(isinstance(result[key]["available"], bool) for key in ("cpu", "cuda", "refinement")):
                raise ValueError("Invalid runtime probe")
        except (OSError, EOFError, RuntimeError, ValueError, KeyError, IndexError, TypeError) as error:
            reason = (
                f"Runtime detection failed or timed out ({type(error).__name__}). "
                "Check the inference installation and try again in five minutes."
            )
            result = {
                name: {"available": False, "reason": reason, "compute_types": []}
                for name in ("cpu", "cuda", "refinement")
            }
        result["image_variant"] = variant
        _cached, _expires, _variant = result, time.monotonic() + 300, variant
        return copy.deepcopy(result)
