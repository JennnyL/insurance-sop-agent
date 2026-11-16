"""Account, human-help tracking and editable email UI, against DEMO_URL."""

import json
import os
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parent.parent


def main():
    base = os.getenv("DEMO_URL", "http://127.0.0.1:8001")
    live = os.getenv("PORTAL_LIVE") == "1"
    result = {"status": "RUNNING", "live_model": live, "checks": [], "browser_errors": []}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000}, reduced_motion="reduce")
        page.set_default_timeout(60000 if live else 10000)
        page.on("pageerror", lambda err: result["browser_errors"].append(str(err)))

        def idle():
            expect(page.locator("#stop-reply")).to_be_hidden(timeout=60000)

        def close():
            page.locator("#account-close").click()

        def send(text):
            page.locator("#message").fill(text)
            page.locator("#send").click()
            idle()
            reply = page.locator("#messages .assistant .bubble").last.inner_text()
            assert "couldn’t produce a reliable answer" not in reply, reply
            return reply

        try:
            page.goto(base)
            expect(page.locator("#send")).to_be_enabled()
            page.locator("#my-claims").click()
            expect(page.locator("#account-claims")).to_contain_text("Verify any three")
            close()
            page.locator("#human").click()
            idle()
            page.locator("#support-open").click()
            expect(page.locator("#account-dialog")).to_be_visible()
            expect(page.locator("#support-list")).to_contain_text("H-")
            idle()
            close()
            expect(page.locator("#message")).to_be_enabled()
            result["checks"].append("left human-help button works before verification; chat stays usable")

            page.locator("#lookup-panel > summary").click()
            page.locator("#lookup-name").fill("Margaret Chen")
            page.locator("#lookup-dob").fill("1985-03-15")
            page.locator("#lookup-email").fill("margaret@email.com")
            page.locator("#lookup-submit").click()
            idle()
            expect(page.locator("#gate-status")).to_have_text("Identity verified")
            page.locator("#my-claims").click()
            expect(page.locator("#account-claims .portal-card")).to_have_count(4)
            expect(page.locator("#account-claims")).not_to_contain_text("CL-3001")
            page.screenshot(path=str(ROOT / "docs/screenshots/my-claims.png"), full_page=True)
            page.locator("#account-claims .portal-card").filter(has_text="CL-2048").get_by_role(
                "button"
            ).click()
            idle()
            expect(page.locator("#sources")).to_contain_text("CL-2048")
            result["checks"].append(
                "identity-only form lists all four owned claims; selecting one resumes chat"
            )

            reply = send("i need human")
            assert "H-" in reply, reply
            page.locator("#support-open").click()
            card = page.locator("#support-list .portal-card").filter(has_text="CL-2048")
            hid = card.locator("h3").inner_text()
            card.locator("textarea").fill("Please clarify how I can get a real status update.")
            card.get_by_role("button", name="Save follow-up").click()
            expect(card.locator(".ticket-timeline")).to_contain_text("Please clarify")
            card.locator(".demo-controls summary").click()
            card.get_by_role("button", name="Simulate representative reply").click()
            expect(card).to_contain_text("SIMULATED staff response")
            page.screenshot(path=str(ROOT / "docs/screenshots/human-progress.png"), full_page=True)
            page.reload()
            expect(page.locator("#send")).to_be_enabled()
            page.locator("#support-open").click()
            expect(page.locator("#support-list")).to_contain_text(hid)
            expect(page.locator("#support-list")).to_contain_text("SIMULATED staff response")
            page.locator("#ticket-id").fill(hid)
            page.locator("#ticket-search button").click()
            expect(page.locator(".ticket-found")).to_be_visible()
            close()
            reply = send(
                "What feedback is available on my human help request? What is its tracking reference?"
            )
            assert hid in reply and "simulat" in reply.lower(), reply
            result["checks"].append(
                "natural human request, persistent reference, customer follow-up, simulated feedback, refresh and natural progress query"
            )
            reply = send("How much has actually been paid on this claim?")
            assert "$0.00" in reply, reply
            result["checks"].append(
                "ordinary contextual claim questions still work while human request is open"
            )

            page.locator("#finish").click()
            expect(page.locator("#summary")).to_be_visible()
            expect(page.locator("#summary-body")).to_contain_text("Next steps:")
            page.locator("#email-note").fill("Please keep a record of my request for a representative.")
            expect(page.locator("#email-send")).to_be_disabled()
            page.locator("#email-save").click()
            idle()
            expect(page.locator("#summary-body")).to_contain_text("Customer-written note")
            expect(page.locator("#email-send")).to_be_enabled()
            page.screenshot(path=str(ROOT / "docs/screenshots/email-editor.png"), full_page=True)
            page.locator("#email-send").click()
            expect(page.locator("#email-status")).to_contain_text("No real email was sent", timeout=15000)
            page.locator("[data-panel=enquiries]").click()
            page.locator(".summary-history > summary").click()
            expect(page.locator("#email-history")).to_contain_text("SIMULATED RECORDED")
            close()
            expect(page.locator("#human")).to_be_enabled()
            page.locator("#human").click()
            idle()
            page.locator("#support-open").click()
            expect(page.locator("#support-list")).to_contain_text(hid)
            close()
            result["checks"].append(
                "visible email entry, preview and note editing, fresh consent, simulated outbox history, human help after completion"
            )

            page.set_viewport_size({"width": 390, "height": 844})
            page.locator("#support-open").click()
            expect(page.locator("#portal-human")).to_be_enabled()
            expect(page.locator("#support-list")).to_contain_text(hid)
            page.screenshot(path=str(ROOT / "docs/screenshots/mobile-support.png"), full_page=True)
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            close()
            page.locator("#logout").click()
            idle()
            page.locator("#support-open").click()
            expect(page.locator("#support-list")).not_to_contain_text("CL-2048")
            expect(page.locator("#support-list")).not_to_contain_text("Please clarify")
            expect(page.locator("#support-list")).to_contain_text("Verify the original account")
            close()
            result["checks"].append(
                "mobile portal accessible without overflow; logout hides protected ticket feedback"
            )
            assert not result["browser_errors"], result["browser_errors"]
            result["status"] = "PASS"
        except Exception as exc:
            result.update(status="FAIL", error=str(exc))
            page.screenshot(path=str(ROOT / "docs/screenshots/portal-failure.png"), full_page=True)
            raise
        finally:
            (
                ROOT
                / "docs"
                / ("live-portal-browser-results.json" if live else "portal-browser-results.json")
            ).write_text(json.dumps(result, indent=2) + "\n")
            print(json.dumps(result, indent=2))
            browser.close()


if __name__ == "__main__":
    main()
