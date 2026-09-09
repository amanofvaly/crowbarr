import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from crowbarr import capabilities
from crowbarr.app import create_app


def report(cuda=False, refinement=False):
    return {
        "cpu": {"available": True, "reason": "", "compute_types": ["int8", "float32"]},
        "cuda": {"available": cuda, "reason": "Use the CUDA image with GPU access.", "compute_types": []},
        "refinement": {"available": refinement, "reason": "WhisperX unavailable."},
    }


@pytest.fixture(autouse=True)
def clear_cache(monkeypatch):
    monkeypatch.setattr(capabilities, "_cached", None)


def test_probe_is_bounded_cached_and_returns_independent_results(monkeypatch):
    calls = []

    def run():
        calls.append(True)
        return report()

    monkeypatch.setattr(capabilities, "_run_probe", run)
    monkeypatch.setenv("CROWBARR_IMAGE_VARIANT", "cpu")
    first = capabilities.runtime_capabilities()
    first["cuda"]["available"] = True
    assert not capabilities.runtime_capabilities()["cuda"]["available"]
    assert len(calls) == 1
    monkeypatch.setattr(capabilities, "_expires", 0)
    capabilities.runtime_capabilities()
    assert len(calls) == 2


@pytest.mark.parametrize("failure", [TimeoutError("probe"), OSError("missing"), EOFError()])
def test_failed_probe_reports_unavailable_and_recovery(monkeypatch, failure):
    def run(*args, **kwargs):
        raise failure

    monkeypatch.setattr(capabilities, "_run_probe", run)
    result = capabilities.runtime_capabilities()
    assert not result["cuda"]["available"]
    assert not result["refinement"]["available"]
    assert "five minutes" in result["cuda"]["reason"]


def test_probe_timeout_terminates_child_and_closes_pipes(monkeypatch):
    events = []
    receive = types.SimpleNamespace(
        poll=lambda timeout: events.append(("poll", timeout)) or False,
        close=lambda: events.append("receive closed"),
    )
    send = types.SimpleNamespace(close=lambda: events.append("send closed"))

    class Process:
        pid = 123
        alive = True

        def start(self):
            events.append("started")

        def join(self, timeout):
            events.append(("join", timeout))

        def is_alive(self):
            return self.alive

        def terminate(self):
            self.alive = False
            events.append("terminated")

        def close(self):
            events.append("process closed")

    def context(method):
        assert method == "spawn"
        return types.SimpleNamespace(Pipe=lambda duplex: (receive, send), Process=lambda **kwargs: Process())

    monkeypatch.setattr(capabilities.multiprocessing, "get_context", context)
    with pytest.raises(TimeoutError):
        capabilities._run_probe()
    assert ("poll", 20) in events
    assert "terminated" in events
    assert "receive closed" in events
    assert "send closed" in events
    assert "process closed" in events


@pytest.mark.parametrize("variant,gpu,refinement", [("cpu", True, True), ("cuda", False, True), ("native", True, False)])
def test_probe_detects_runtime_and_refinement_independently(monkeypatch, variant, gpu, refinement):
    def compute(device):
        if device == "cuda" and not gpu:
            raise RuntimeError("no GPU")
        return {"int8", "float32"}

    monkeypatch.setenv("CROWBARR_IMAGE_VARIANT", variant)
    monkeypatch.setitem(sys.modules, "ctranslate2", types.SimpleNamespace(get_supported_compute_types=compute))
    monkeypatch.setitem(sys.modules, "faster_whisper", types.ModuleType("faster_whisper"))
    monkeypatch.setitem(sys.modules, "whisperx", types.ModuleType("whisperx") if refinement else None)
    monkeypatch.setitem(sys.modules, "whisperx.alignment", types.SimpleNamespace(align=None, load_align_model=None))
    monkeypatch.setattr("ctypes.CDLL", lambda name: object())
    namespace = {}
    exec(capabilities._PROBE, namespace)
    result = namespace["result"]
    assert result["cuda"]["available"] is (gpu and variant != "cpu")
    assert result["refinement"]["available"] is refinement


def test_web_process_does_not_import_torch():
    result = subprocess.run(
        [sys.executable, "-c", "import crowbarr.capabilities; import crowbarr.app; import sys; assert 'torch' not in sys.modules"],
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr("crowbarr.app.runtime_capabilities", lambda: report())
    with TestClient(create_app(tmp_path, background=False)) as client:
        client.headers["X-Api-Key"] = client.app.state.store.token
        yield client


def test_capabilities_are_authenticated_and_in_settings(client):
    assert client.get("/api/capabilities").json() == report()
    assert client.get("/api/settings").json()["capabilities"] == report()
    client.headers.clear()
    assert client.get("/api/capabilities").status_code == 401


def test_unavailable_cuda_cannot_be_newly_selected(client):
    response = client.put("/api/settings", json={"device": "cuda"})
    assert response.status_code == 422
    assert "CUDA image" in response.json()["detail"]
    assert client.app.state.store.get().device == "cpu"


@pytest.mark.parametrize("full_payload", [False, True])
def test_saved_unavailable_cuda_survives_unrelated_saves(client, full_payload):
    store = client.app.state.store
    saved = store.get()
    saved.device, saved.compute_type = "cuda", "float16"
    store.save(saved)
    payload = client.get("/api/settings").json() if full_payload else {}
    payload["cpu_threads"] = 3
    response = client.put("/api/settings", json=payload)
    assert response.status_code == 200
    assert response.json()["device"] == "cuda"
    assert store.get().compute_type == "float16"
    assert store.get().cpu_threads == 3


def test_available_cuda_can_be_selected_and_cpu_precision_is_validated(client, monkeypatch):
    monkeypatch.setattr("crowbarr.app.runtime_capabilities", lambda: report(cuda=True, refinement=True))
    assert client.put("/api/settings", json={"device": "cuda", "compute_type": "float16"}).status_code == 200
    assert client.put("/api/settings", json={"device": "cpu"}).status_code == 422
    assert client.put("/api/settings", json={"device": "cpu", "compute_type": "int8"}).status_code == 200


def test_settings_ui_retains_unavailable_choices_and_changes_cpu_precision():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed to execute the Settings UI behavior test")
    script = r'''
const fs = require("node:fs");
const vm = require("node:vm");
const assert = require("node:assert/strict");
const source = fs.readFileSync("crowbarr/static/app.js", "utf8");
const elements = {
  device: {value: "cuda"},
  compute_type: {value: "float16", options: ["int8", "float32", "float16", "int8_float16"].map(value => ({value}))},
};
const nodes = {"settings-form": {elements}, "precision-help": {}, "device-help": {}, "save-state": {}};
let change;
const context = vm.createContext({
  settings: {device: "cuda", compute_type: "float16", cpu_fallback: true, refine_generated: true,
    capabilities: {cpu: {available: true}, cuda: {available: false, reason: "Use the CUDA image."},
      refinement: {available: false, reason: "Whisper word timestamps will be used instead."}}},
  section: "processing", groups: [], heading: () => "", esc: value => String(value ?? ""),
  $: id => nodes[id], dirty: false, draftDirty: false,
  captureSettings: () => {context.settings.device = elements.device.value;},
  document: {addEventListener: (name, fn) => {change = fn;}},
});
vm.runInContext(source.slice(source.indexOf("const field ="), source.indexOf("function apiPage()")), context);
let html = vm.runInContext("settingsPage()", context);
assert.match(html, /value="cuda" selected disabled/);
assert.match(html, /saved CUDA choice is retained/);
assert.match(html, /attempt CPU fallback/);
assert.match(html, /name="refine_generated"[^>]*checked[^>]*disabled/);
assert.match(html, /Whisper word timestamps/);
assert.doesNotMatch(html, /value="cpu"[^>]*disabled/);
context.settings.capabilities.cpu.available = false;
assert.match(vm.runInContext("settingsPage()", context), /value="cpu"[^>]*disabled/);
context.settings.capabilities.cpu.available = true;
context.settings.cpu_fallback = false;
assert.match(vm.runInContext("settingsPage()", context), /enable CPU fallback or choose CPU/);
context.settings.capabilities.cuda.available = true;
context.settings.capabilities.refinement.available = true;
html = vm.runInContext("settingsPage()", context);
assert.doesNotMatch(html, /value="cuda" selected disabled/);
assert.doesNotMatch(html, /name="refine_generated"[^>]*disabled/);
vm.runInContext(source.slice(source.indexOf('document.addEventListener("change"'),
  source.indexOf('document.addEventListener("submit"')), context);
elements.device.value = "cpu";
change({target: {name: "device", closest: () => true}});
assert.equal(elements.compute_type.value, "int8");
assert.ok(elements.compute_type.options.find(option => option.value === "float16").disabled);
assert.match(nodes["precision-help"].textContent, /Precision changed to INT8/);
assert.equal(context.draftDirty, true);
elements.device.value = "cuda";
change({target: {name: "device", closest: () => true}});
assert.equal(elements.compute_type.options.find(option => option.value === "float16").disabled, false);
'''
    result = subprocess.run(
        [node, "-e", script], cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
