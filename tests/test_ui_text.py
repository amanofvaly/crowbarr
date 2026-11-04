import json
import shutil
import subprocess
from pathlib import Path

import pytest

import crowbarr

SCRIPT = (Path(crowbarr.__file__).parent / "static" / "app.js").read_text()
needs_node = pytest.mark.skipif(not shutil.which("node"), reason="runs page code with node")


def run_node(program: str):
    done = subprocess.run(["node", "-e", program], capture_output=True, text=True, check=True)
    return json.loads(done.stdout)


def excerpt(start: str, end: str) -> str:
    return SCRIPT[SCRIPT.index(start) : SCRIPT.index(end)]


@needs_node
def test_relative_times_use_the_singular_for_one():
    program = excerpt("const elapsed", "// A time of day") + """
const now = Date.now() / 1000;
console.log(JSON.stringify([60, 120, 3600, 7200, 86400, 172800].map((s) => ago(now - s))));
"""
    assert run_node(program) == [
        "1 minute ago", "2 minutes ago", "1 hour ago", "2 hours ago", "1 day ago", "2 days ago",
    ]


@needs_node
@pytest.mark.parametrize(
    "monitored_only, expected",
    [(True, "Only monitored items are included"), (False, "has no imported shows yet")],
)
def test_empty_library_explains_a_connected_manager_with_no_files(monitored_only, expected):
    program = f"""
let listQuery = "", libraryKind = "shows";
let status = {{ integrations: [{{ provider: "sonarr", healthy: 1, file_count: 0 }}] }};
let savedSettings = {{ sonarr: {{ monitored_only: {str(monitored_only).lower()} }} }};
""" + excerpt("function libraryEmptyText", "async function loadList") + """
console.log(JSON.stringify(libraryEmptyText()));
"""
    heading, text = run_node(program)
    assert heading == "Sonarr reported no files"
    assert expected in text


@needs_node
def test_empty_library_without_a_manager_keeps_the_setup_prompt():
    program = """
let listQuery = "", libraryKind = "shows", status = { integrations: [] }, savedSettings = {};
""" + excerpt("function libraryEmptyText", "async function loadList") + """
console.log(JSON.stringify(libraryEmptyText()));
"""
    assert run_node(program)[0] == "Your library is waiting"


@needs_node
def test_services_without_a_sync_record_are_not_shown_as_needing_setup():
    program = 'const fmt = (n) => n;\n' + excerpt("function serviceState", "function systemPanel") + """
const healthy = { healthy: 1, file_count: 4 };
console.log(JSON.stringify([
  serviceState("bazarr", true, undefined), serviceState("plex", false, undefined),
  serviceState("sonarr", true, healthy), serviceState("radarr", true, { healthy: 0 }),
]));
"""
    assert run_node(program) == ["Configured", "Not set up", "Connected · 4 files", "Cannot sync"]


def test_every_setting_has_a_control_in_the_page_code():
    """A setting added without any control cannot be changed, so its default applies silently.

    This checks that a control is written, not that it is reachable on screen."""
    import re

    from crowbarr.config import Settings

    # Set elsewhere: the model table, the pause button, the connection forms, a fixed value.
    elsewhere = {"language", "model", "paused", "sonarr", "radarr", "bazarr", "plex", "plex_mappings"}
    controls = re.compile(r'(?:numeric|check|select)\(\s*"(\w+)"|name="(\w+)"')
    present = {a or b for a, b in controls.findall(SCRIPT)}
    missing = set(Settings.model_fields) - elsewhere - present
    assert not missing, f"no control for: {sorted(missing)}"


UPDATE_HELPERS = """
const esc = (value) => String(value ?? "");
const ago = () => "2 minutes ago";
const button = (label, action, style, attrs = "") => `<button data-action="${action}" ${attrs}>${label}</button>`;
const check = (name, label, help = "", disabled = false) => `<input name="${name}" ${disabled ? "disabled" : ""}>${label}`;
const settingPanel = (name, description, fields) => fields;
let settings = { update_check: true, auto_update: false };
"""


def render_updates(info: dict, **settings) -> str:
    program = UPDATE_HELPERS + f"settings = {{ update_check: true, auto_update: false, ...{json.dumps(settings)} }};\n"
    program += excerpt("let updateInfo = null;", "function settingPanel(")
    program += f"\nupdateInfo = {json.dumps(info)};\nconsole.log(JSON.stringify(updatesPanel()));"
    return run_node(program)


BASE = {
    "current": "0.4.12", "latest": "0.4.13", "available": True, "checked_at": 1, "error": "", "notes": "",
    "kind": "image", "can_install": True, "reason": "", "instructions": [], "image_tag": "",
    "job": {"status": "idle", "reason": ""}, "running_update": "", "failed": None,
}


@needs_node
def test_the_updates_page_offers_the_install_button_when_the_image_can_run_the_release():
    html = render_updates({**BASE, "notes": "- Fixes things"})

    assert "Version 0.4.13 is available." in html
    assert 'data-action="update-install"' in html and "Update to 0.4.13" in html
    assert "What is new in 0.4.13" in html and "Fixes things" in html


@needs_node
def test_the_updates_page_gives_the_image_tag_when_the_release_needs_a_new_image():
    html = render_updates({
        **BASE, "can_install": False, "reason": "This release needs fastapi 0.2, and this image has 0.1.",
        "instructions": ["Change the image to the new tag and save."],
        "image_tag": "ghcr.io/amanofvaly/crowbarr:0.4.13-cuda",
    })

    assert "update-install" not in html
    assert "needs fastapi 0.2" in html
    assert "ghcr.io/amanofvaly/crowbarr:0.4.13-cuda" in html
    assert "Change the image to the new tag" in html


@needs_node
def test_the_updates_page_shows_progress_failures_and_a_reverted_update():
    working = render_updates({**BASE, "job": {"status": "restarting", "reason": ""}})
    assert "Restarting. This page reloads" in working
    assert working.count("disabled") >= 2

    failed = render_updates({**BASE, "job": {"status": "failed", "reason": "The download did not match its checksum."}})
    assert "did not match its checksum" in failed

    reverted = render_updates({**BASE, "failed": {"version": "0.4.13", "reason": "Version 0.4.13 did not stay running."}})
    assert "did not stay running" in reverted


@needs_node
def test_the_updates_page_says_when_nothing_is_new_and_ties_automatic_installs_to_checking():
    html = render_updates({**BASE, "available": False, "latest": "0.4.12", "can_install": False})
    assert "You are up to date." in html
    assert 'name="auto_update" ' in html

    unchecked = render_updates({**BASE, "available": False}, update_check=False)
    assert 'name="auto_update" disabled' in unchecked
