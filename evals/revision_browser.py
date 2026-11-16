"""Real browser acceptance: shared cookie authorization and asynchronous requests."""

import json
import os
import re
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parent.parent


def main():
    result = {"status": "RUNNING", "checks": [], "browser_errors": []}
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        context = browser.new_context(base_url=os.getenv("DEMO_URL", "http://127.0.0.1:8002"))
        page = context.new_page()
        page.on("pageerror", lambda e: result["browser_errors"].append(str(e)))
        page.goto("/")
        expect(page.locator("#send")).to_be_enabled()

        def send(text, target=page):
            target.locator("#message").fill(text)
            target.locator("#send").click()
            expect(target.locator(".typing")).to_have_count(0, timeout=75000)
            expect(target.locator("#error")).to_be_hidden()

        send("i need human")
        expect(page.locator("#human-chat-bar")).to_be_hidden()
        expect(page.locator("#messages .assistant").last).to_contain_text("registered email or phone")
        hid = re.search(r"H-[A-Z0-9]+", page.locator("#messages .assistant").last.inner_text()).group()
        page.locator('#suggestions button:has-text("Wait for human only")').click()
        expect(page.locator("#messages .assistant").last).to_contain_text("Wait-only")
        send("Can you make an exception?")
        expect(page.locator("#messages .assistant").last).to_have_text(
            "Harbor AI assistantSystem confirmation: added to " + hid + "."
        )
        page.locator('#suggestions button:has-text("Continue with AI")').click()
        expect(page.locator("#messages .assistant").last).to_contain_text("AI assistant")
        expect(page.locator("#messages .assistant").last).to_contain_text(hid)
        result["checks"].append(
            "Human request before verification: reference, process, wait acknowledgment and AI resume"
        )

        page.locator("#lookup-panel > summary").click()
        page.locator("#lookup-sample").click()
        page.locator("#lookup-submit").click()
        expect(page.locator("#gate-status")).to_have_text("Identity verified", timeout=75000)
        second = context.new_page()
        second.goto("/")
        expect(second.locator("#gate-status")).to_have_text("Identity verified")
        second.locator("#my-claims").click()
        expect(second.locator("#account-claims .portal-card")).to_have_count(4)
        second.locator("#account-close").click()
        second.locator("#inquiry-open").click()
        expect(second.locator("#inquiry-submit")).to_be_enabled()
        second.locator("#inquiry-subject").fill("Please review the expired deadline")
        second.locator("#inquiry-body").fill("Which options can a representative assess?")
        second.locator("#inquiry-submit").click()
        expect(second.locator("#inquiry-list")).to_contain_text("E-", timeout=10000)
        second.locator("#account-close").click()
        second.locator("#reset").click()
        expect(second.locator("#gate-status")).to_have_text("Identity verified")
        expect(second.locator("#messages")).to_contain_text(hid)
        result["checks"].append(
            "New tab and New session retain identity; claims, email and pending H request share authorization"
        )

        send("POL-9921", second)
        expect(second.locator("#case-picker button")).to_have_count(4)
        second.locator("#case-picker button").filter(has_text="CL-2048").click()
        expect(second.locator("#sources")).to_contain_text("CL-2048")
        expect(second.locator("#suggestions")).not_to_contain_text("documents")
        send("thanks", second)
        expect(second.locator("#messages .assistant").last).to_contain_text("anything else")
        second.locator("#suggestions button").filter(has_text="No, that").click()
        expect(second.locator("#chat-summary")).to_contain_text("Send this summary", timeout=75000)
        expect(second.locator("#chat-summary")).to_contain_text("Skip email")
        second.locator("#review-summary").click()
        expect(second.locator("#summary-body")).to_contain_text("Conclusion:")
        expect(second.locator("#summary-body")).to_contain_text(hid)
        expect(second.locator("#summary-body")).not_to_contain_text("Ask your provider")
        second.locator("#account-close").click()
        second.locator("#chat-summary button").filter(has_text="Skip email").click()
        result["checks"].append(
            "Policy switching; expired next steps; natural closing and in-chat email Send/Skip with H reference"
        )

        second.locator("#support-open").click()
        card = second.locator("#ticket-" + hid)
        card.get_by_text("Demo controls · simulate staff updates").click()
        card.get_by_role("button", name="Simulate representative reply", exact=True).click()
        expect(second.locator("#support-list")).to_contain_text("SIMULATED staff response")
        second.locator("#ticket-" + hid).get_by_role("button", name="Cancel request", exact=True).click()
        expect(second.locator("#support-list")).to_contain_text("Cancelled")
        second.locator("#account-close").click()
        result["checks"].append("Human request feedback, tracking and cancellation")

        second.locator("#logout").click()
        expect(second.locator("#gate-status")).to_have_text("Identity gate is locked")
        expect(page.locator("#gate-status")).to_have_text("Identity gate is locked", timeout=6000)
        third = context.new_page()
        third.goto("/")
        expect(third.locator("#gate-status")).to_have_text("Identity gate is locked")
        third.locator("#inquiry-open").click()
        expect(third.locator("#inquiry-form")).to_be_hidden()
        result["checks"].append("Logout revokes all tabs and future conversations, including email access")
        page.set_viewport_size({"width": 390, "height": 844})
        page.reload()
        expect(page.locator("#send")).to_be_enabled()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        result["checks"].append("Mobile loads without horizontal overflow")
        result["status"] = "PASS" if not result["browser_errors"] else "FAIL"
        browser.close()
    (ROOT / "docs/revision-browser-result.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
