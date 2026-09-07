"""Maintainer smoke check against a running Crowbarr instance; never seeds fake jobs."""

import argparse
import json
from pathlib import Path

from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:18449")
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("data/browser-check"))
    parser.add_argument("--require-audit", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(args.url)
        page.get_by_label("Crowbarr API key").fill(args.token_file.read_text().strip())
        page.get_by_role("button", name="Sign in", exact=True).click()
        page.get_by_role("heading", name="Library activity").wait_for()
        page.wait_for_timeout(600)
        assert page.locator("#message").is_hidden(), page.locator("#message").inner_text()
        page.screenshot(path=str(args.output / "desktop-activity.png"), full_page=True)
        audit_index = page.evaluate("snapshot.jobs.findIndex(job => job.report?.audit)")
        if args.require_audit:
            assert audit_index >= 0, "A real audited job is required"
        if audit_index >= 0:
            page.locator("#jobs tr").nth(audit_index).get_by_role("button", name="Details").click()
            page.get_by_text("Confident cue coverage", exact=True).wait_for()
            with page.expect_download() as download:
                page.get_by_role("button", name="Download full audit report").click()
            report = json.loads(Path(download.value.path()).read_text())
            assert report["audit"]["before"]["decision"] in {"pass", "repair", "inconclusive"}
            page.screenshot(path=str(args.output / "desktop-audit.png"), full_page=True)
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "Audit overflows"
            page.screenshot(path=str(args.output / "mobile-audit.png"), full_page=True)
            page.set_viewport_size({"width": 1440, "height": 1000})
        page.get_by_role("button", name="Settings", exact=True).click()
        page.get_by_role("heading", name="Settings", exact=True).wait_for()
        for provider in ("sonarr", "radarr"):
            assert page.locator(f"#{provider}-mappings").is_visible()
            assert page.locator(f"#{provider}-monitored").is_visible()
        page.screenshot(path=str(args.output / "desktop-settings.png"), full_page=True)
        page.get_by_role("button", name="Save settings", exact=True).click()
        page.get_by_text("Settings saved.", exact=True).wait_for()
        page.set_viewport_size({"width": 390, "height": 844})
        page.screenshot(path=str(args.output / "mobile-settings.png"), full_page=True)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "Mobile page overflows"
        page.get_by_role("button", name="Activity", exact=True).click()
        page.screenshot(path=str(args.output / "mobile-activity.png"), full_page=True)
        page.get_by_role("button", name="Sign out", exact=True).click()
        page.get_by_role("heading", name="Welcome to Crowbarr").wait_for()
        assert not errors, errors
        browser.close()
    print(
        "Browser check passed: login, activity, settings save, mobile layout, sign-out; no JavaScript errors."
    )


if __name__ == "__main__":
    main()
