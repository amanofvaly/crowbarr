import json
import shutil
import subprocess
import sys
import types
from decimal import Decimal
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
    monkeypatch.setattr("crowbarr.capabilities.runtime_capabilities", lambda: report())
    with TestClient(create_app(tmp_path, background=False)) as client:
        client.headers["X-Api-Key"] = client.app.state.store.token
        yield client


def test_capabilities_are_authenticated_and_in_settings(client):
    served = client.get("/api/capabilities").json()
    # The probe result is served verbatim, with on-disk model state alongside it.
    assert {key: served[key] for key in report()} == report()
    assert served["models"] == {
        "speech": [],
        "alignment": {"present": False, "bytes": 0, "status": "idle", "reason": ""},
    }
    assert client.get("/api/settings").json()["capabilities"]["models"] == served["models"]
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
    monkeypatch.setattr("crowbarr.capabilities.runtime_capabilities", lambda: report(cuda=True, refinement=True))
    assert client.put("/api/settings", json={"device": "cuda", "compute_type": "float16"}).status_code == 200
    assert client.put("/api/settings", json={"device": "cpu"}).status_code == 422
    assert client.put("/api/settings", json={"device": "cpu", "compute_type": "int8"}).status_code == 200


def test_every_numeric_default_is_valid_for_its_own_input(tmp_path):
    """A default the browser rejects blocks saving the whole panel, not just that field."""
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed to render the settings form")
    from crowbarr.config import Settings

    defaults = Settings().model_dump()
    script = r"""
const fs = require("node:fs"), vm = require("node:vm");
const source = fs.readFileSync("crowbarr/static/app.js", "utf8");
const settings = JSON.parse(process.argv[1]);
settings.capabilities = {cpu: {available: true}, cuda: {available: true},
  refinement: {available: true, reason: "ok"},
  models: {speech: [], alignment: {present: true, bytes: 1, status: "ready", reason: ""}}};
const fields = [];
for (const section of ["library", "processing", "resources", "quality"]) {
  const context = vm.createContext({
    settings, section, groups: [], heading: () => "", esc: v => String(v ?? ""),
    $: () => ({}), status: {media_count: 1}, fmt: v => String(v),
    button: (l, a) => `<button data-action="${a}">${l}</button>`,
    document: {addEventListener: () => {}, documentElement: {dataset: {}}},
  });
  vm.runInContext(source.slice(source.indexOf("const field ="), source.indexOf("function apiPage()")), context);
  const html = vm.runInContext("settingsPage()", context);
  for (const tag of html.match(/<input[^>]*type="number"[^>]*>/g) || []) {
    const get = key => (tag.match(new RegExp(key + '="([^"]*)"')) || [])[1];
    fields.push({name: get("name"), value: get("value"), min: get("min"), step: get("step")});
  }
}
console.log(JSON.stringify(fields));
"""
    result = subprocess.run(
        [node, "-e", script, json.dumps(defaults)],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    fields = json.loads(result.stdout)
    assert fields, "no numeric inputs were rendered"
    for field in fields:
        if field["step"] == "any":
            continue
        offset = Decimal(field["value"]) - Decimal(field["min"])
        assert offset % Decimal(field["step"]) == 0, (
            f'{field["name"]} default {field["value"]} is not reachable from '
            f'min {field["min"]} in steps of {field["step"]}'
        )


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
      refinement: {available: false, reason: "Whisper word timestamps will be used instead."},
      models: {speech: ["small"], alignment: {present: true, bytes: 377487360, status: "ready", reason: ""}}}},
  section: "processing", groups: [], heading: () => "", esc: value => String(value ?? ""),
  status: {media_count: 2218}, fmt: value => String(value),
  button: (label, action, style, attrs) => `<button data-action="${action}" ${attrs || ""}>${label}</button>`,
  $: id => nodes[id], dirty: false, draftDirty: false,
  captureSettings: () => {context.settings.device = elements.device.value;},
  document: {addEventListener: (name, fn) => {change = fn;}},
});
vm.runInContext(source.slice(source.indexOf("const field ="), source.indexOf("function apiPage()")), context);
let html = vm.runInContext("settingsPage()", context);
assert.match(html, /value="cuda" selected disabled/);
assert.match(html, /set to use a graphics card, but none is available/);
assert.match(html, /Processing runs on the CPU instead/);
assert.match(html, /re-checks all 2218 media files/);
assert.match(html, /name="refine_generated"[^>]*checked[^>]*disabled/);
assert.match(html, /Whisper word timestamps/);
assert.doesNotMatch(html, /value="cpu"[^>]*disabled/);
context.settings.capabilities.cpu.available = false;
assert.match(vm.runInContext("settingsPage()", context), /value="cpu"[^>]*disabled/);
context.settings.capabilities.cpu.available = true;
context.settings.cpu_fallback = false;
assert.match(vm.runInContext("settingsPage()", context), /Turn on CPU fallback or choose CPU/);
context.settings.capabilities.cuda.available = true;
context.settings.capabilities.refinement.available = true;
html = vm.runInContext("settingsPage()", context);
assert.doesNotMatch(html, /value="cuda" selected disabled/);
assert.doesNotMatch(html, /name="refine_generated"[^>]*disabled/);
assert.match(html, /alignment model is downloaded \(360 MB\)/);
// Without the model, refinement cannot be turned on and the download is offered instead.
context.settings.capabilities.models.alignment = {present: false, bytes: 0, status: "idle", reason: ""};
const missing = vm.runInContext("settingsPage()", context);
assert.match(missing, /name="refine_generated"[^>]*disabled/);
assert.match(missing, /data-action="fetch-alignment"/);
assert.match(missing, /Download alignment model/);
context.settings.capabilities.models.alignment = {present: false, bytes: 0, status: "running", reason: ""};
assert.match(vm.runInContext("settingsPage()", context), /Downloading…/);
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
