"""Read-only browser checks against a running instance and its actual library."""

import argparse
from pathlib import Path

from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:18449")
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("data/browser-check"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(
            viewport={"width": 1512, "height": 982},
            extra_http_headers={"X-Api-Key": args.token_file.read_text().strip()},
        )
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
        routes = [
            "dashboard",
            "activity",
            "library",
            "review",
            "history",
            "settings/connections",
            "settings/processing",
            "settings/resources",
            "settings/quality",
            "api",
        ]
        for size, width, height in [("desktop", 1512, 982), ("mobile", 390, 844)]:
            page.set_viewport_size({"width": width, "height": height})
            for route in routes:
                page.goto(args.url + "/#" + route)
                page.locator("#workspace").wait_for(state="visible")
                page.wait_for_timeout(400)
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), route
                page.screenshot(
                    path=str(args.output / f"{size}-{route.replace('/', '-')}.png"), full_page=True
                )
        page.set_viewport_size({"width": 1512, "height": 982})
        page.goto(args.url + "/#activity")
        page.wait_for_timeout(400)
        if page.get_by_role("button", name="Details", exact=True).count():
            page.get_by_role("button", name="Details", exact=True).first.click()
            page.locator("dialog[open]").wait_for()
            page.get_by_role("button", name="Close details").click()
        page.goto(args.url + "/#api")
        page.get_by_role("button", name="Show", exact=True).click()
        assert page.locator("#api-key").get_attribute("type") == "text"
        page.get_by_role("button", name="Hide", exact=True).click()
        assert context.request.get(args.url + "/api/openapi.json").status == 200
        page.get_by_role("button", name="Switch to light theme").click()
        assert page.locator("html").get_attribute("data-theme") == "light"
        page.goto(args.url + "/#dashboard")
        page.wait_for_timeout(400)
        page.screenshot(path=str(args.output / "desktop-light-dashboard.png"), full_page=True)
        assert not errors, errors
        browser.close()
    print("Browser check passed: real server data, all routes, desktop/mobile layouts, details, API, themes.")


if __name__ == "__main__":
    main()
