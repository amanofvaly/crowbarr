"""Bounded runtime discovery, isolated from the web process and model downloads."""

from __future__ import annotations

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
except Exception:
    result["cpu"] = {"available": False, "reason": "The CTranslate2 speech runtime is missing or cannot load. Install Crowbarr's inference dependencies.", "compute_types": []}
try:
    if os.environ.get("CROWBARR_IMAGE_VARIANT") == "cpu":
        raise RuntimeError("CPU image")
    if not result["cpu"]["available"]:
        raise RuntimeError("Speech runtime unavailable")
    if sys.platform.startswith("linux"):
        ctypes.CDLL("libcublas.so.12")
        ctypes.CDLL("libcudnn.so.9")
    types = sorted(ctranslate2.get_supported_compute_types("cuda"))
    if not types:
        raise RuntimeError("No CUDA compute types")
    result["cuda"] = {"available": True, "reason": "", "compute_types": types}
except Exception:
    reason = "CUDA is unavailable. Use the CUDA image or a CUDA-enabled native installation, with a compatible NVIDIA driver and GPU access."
    if os.environ.get("CROWBARR_IMAGE_VARIANT") == "cpu":
        reason = "This is the CPU image. Install the CUDA image and expose a compatible NVIDIA GPU to enable CUDA."
    result["cuda"] = {"available": False, "reason": reason, "compute_types": []}
try:
    import whisperx
    from whisperx.alignment import align, load_align_model
    result["refinement"] = {"available": True, "reason": "WhisperX is installed. Alignment models are loaded when needed; initialization can still fail."}
except Exception:
    result["refinement"] = {"available": False, "reason": "WhisperX or its dependencies are missing or cannot load. Whisper word timestamps will be used instead."}
'''

_lock = threading.Lock()
_cached: dict | None = None
_expires = 0.0
_variant = ""


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
        except (OSError, EOFError, RuntimeError, ValueError, KeyError, IndexError, TypeError):
            reason = "Runtime detection failed or timed out. Check the inference installation and try again in five minutes."
            result = {
                name: {"available": False, "reason": reason, "compute_types": []}
                for name in ("cpu", "cuda", "refinement")
            }
        result["image_variant"] = variant
        _cached, _expires, _variant = result, time.monotonic() + 300, variant
        return copy.deepcopy(result)
