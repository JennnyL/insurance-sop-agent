"""Run against a running offline demo: python evals/browser_smoke.py."""

import json
import os
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parent.parent


def main():
    base = os.getenv("DEMO_URL", "http://127.0.0.1:8000")
    destination = ROOT / "docs/screenshots"
    destination.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000}, reduced_motion="reduce")
        errors = []
        page.on("pageerror", lambda err: errors.append(str(err)))
        page.goto(base)
        expect(page.locator("#gate-status")).to_have_text("Identity gate is locked")
        expect(page.locator("#send")).to_be_enabled()
        page.screenshot(path=str(destination / "desktop-start.png"), full_page=True)
        page.locator("#message").fill(
            "My name is Nobody Exists. DOB is 1900-01-01. Email is nobody@example.com. Policy POL-0000."
        )
        page.locator("#send").click()
        page.get_by_text("I couldn’t verify the information together.", exact=False).wait_for()
        assert page.locator("#gate-status").inner_text() == "Identity gate is locked"
        page.locator("#sample").click()
        page.locator("#send").click()
        expect(page.locator("#gate-status")).to_have_text("Identity verified")
        assert page.locator("#phase-name").inner_text() == "Work through it"
        assert "CL-2048" in page.locator("#sources").inner_text()
        page.screenshot(path=str(destination / "desktop-claim.png"), full_page=True)
        page.locator("#finish").click()
        page.locator("#email-send").wait_for(state="visible")
        assert "Next steps:" in page.locator("#summary-body").inner_text()
        page.screenshot(path=str(destination / "desktop-summary.png"), full_page=True)
        page.locator("#email-send").click()
        page.get_by_text("Recorded in the demo outbox. No real email was sent.", exact=True).wait_for(
            timeout=15000
        )
        assert page.locator("#lifecycle").inner_text() == "COMPLETED"
        page.reload()
        page.get_by_text("COMPLETED", exact=True).wait_for()
        page.locator("#reset").click()
        expect(page.locator("#gate-status")).to_have_text("Identity verified")
        page.set_viewport_size({"width": 390, "height": 844})
        page.screenshot(path=str(destination / "mobile-start.png"), full_page=True)
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        page.locator("#inspect-toggle").click()
        page.locator("#sample").click()
        page.locator("#send").click()
        page.locator("#finish").wait_for(state="visible")
        page.locator("#finish").click()
        page.locator("#email-skip").click()
        expect(page.locator("#lifecycle")).to_have_text("COMPLETED")
        page.locator("#account-close").click()
        page.locator("#reset").click()
        expect(page.locator("#gate-status")).to_have_text("Identity verified")
        page.locator("#lookup-panel > summary").click()
        page.locator("#lookup-sample").click()
        page.screenshot(path=str(destination / "mobile-form.png"), full_page=True)
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        page.locator("#lookup-submit").click()
        expect(page.locator("#gate-status")).to_have_text("Identity verified")
        assert page.locator("#lookup-name").input_value() == ""
        assert page.locator("#lookup-ssn").input_value() == ""
        page.locator("#message").fill("How much was paid?")
        page.locator("#send").click()
        page.get_by_text("Finalized insurer payment recorded", exact=False).wait_for()
        assert not errors, errors
        result = {
            "status": "PASS",
            "checks": [
                "incorrect identity then complete sample in the same session",
                "desktop sample",
                "summary preview",
                "consent send",
                "simulated outbox",
                "refresh persistence",
                "reset",
                "mobile layout",
                "mobile skip",
                "mobile form verifies and clears private fields",
                "form-to-chat continuity",
            ],
            "viewport_sizes": [[1440, 1000], [390, 844]],
            "browser_errors": errors,
        }
        (ROOT / "evals/browser-results.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result, indent=2))
        browser.close()


if __name__ == "__main__":
    main()
