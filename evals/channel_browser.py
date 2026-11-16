"""Reproduce channel switching with both identity inputs and reload in four orders."""

import json
import os
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = "My name is Margaret Chen. DOB is 1985-03-15. SSN last four is 4472. My denied healthcare claim from January."


def main():
    report = {"status": "RUNNING", "cases": [], "errors": []}
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            for human_first in [True, False]:
                for method in ["chat", "form"]:
                    context = browser.new_context(base_url=os.getenv("DEMO_URL", "http://127.0.0.1:8002"))
                    page = context.new_page()
                    page.on("pageerror", lambda e: report["errors"].append(str(e)))
                    page.goto("/")
                    expect(page.locator("#send")).to_be_enabled()

                    def idle():
                        expect(page.locator("#stop-reply")).to_be_hidden()

                    def send(text):
                        page.locator("#message").fill(text)
                        page.locator("#send").click()
                        idle()

                    def human_resume():
                        page.locator("#human").click()
                        idle()
                        verified = page.locator("#gate-status").inner_text() == "Identity verified"
                        selector = (
                            "#chat-mode-toggle"
                            if verified
                            else '#suggestions button:has-text("Continue with AI")'
                        )
                        page.locator(selector).click()
                        idle()
                        expect(page.locator("#messages .assistant").last).to_contain_text("AI assistant")
                        page.reload()
                        expect(page.locator("#messages .assistant").last).to_contain_text("AI assistant")
                        if not verified:
                            expect(page.locator("#human-chat-bar")).to_be_hidden()
                            expect(page.locator("#support-count")).to_have_text("")

                    if human_first:
                        human_resume()
                    if method == "chat":
                        send(SAMPLE)
                    else:
                        page.locator("#lookup-panel > summary").click()
                        page.locator("#lookup-sample").click()
                        page.locator("#lookup-submit").click()
                        idle()
                    expect(page.locator("#gate-status")).to_have_text("Identity verified")
                    if not human_first:
                        human_resume()
                    # Mode toggle can go both ways; identity and AI context must survive.
                    page.locator("#wait-human").click()
                    idle()
                    expect(page.locator("#human-chat-label")).to_contain_text("Wait only")
                    page.locator("#chat-mode-toggle").click()
                    idle()
                    send("How much was paid?")
                    expect(page.locator("#messages .assistant").last).to_contain_text("$0.00")
                    expect(page.locator("#messages .assistant").last).not_to_contain_text(
                        "Your request for a live representative"
                    )
                    page.locator("#my-claims").click()
                    expect(page.locator("#account-claims .portal-card")).to_have_count(4)
                    page.locator("[data-panel=enquiries]").click()
                    expect(page.locator("#inquiry-form")).to_be_visible()
                    page.locator("#inquiry-subject").fill("A question after resuming AI")
                    page.locator("#inquiry-body").fill("Please confirm the documents needed.")
                    page.locator("#inquiry-submit").click()
                    expect(page.locator("#inquiry-list")).to_contain_text("E-")
                    sid = page.evaluate("sessionStorage.getItem('sop-session')")
                    events = page.request.get(f"/v1/sessions/{sid}/trace").json()["events"]
                    assert sum(e["kind"] == "verification.passed" for e in events) == 1
                    report["cases"].append(
                        {
                            "human_first": human_first,
                            "identity_input": method,
                            "status": "PASS",
                            "checks": [
                                "AI choice survives reload",
                                "repeated toggle resumes server AI state",
                                "claim follow-up is grounded",
                                "portal and email reuse one verification",
                            ],
                        }
                    )
                    page.locator("#account-close").click()
                    if method == "chat":
                        send("POL-9921")
                    else:
                        page.locator("#my-claims").click()
                        page.locator("#case-reference").fill("POL-9921")
                        page.locator("#case-search button").click()
                        idle()
                    expect(page.locator("#case-picker button")).to_have_count(4)
                    assert page.locator("#case-picker").bounding_box()["height"] >= 100
                    expect(page.locator("#case-picker button").first).to_be_in_viewport()
                    mobile_picker = not human_first and method == "form"
                    if mobile_picker:
                        page.set_viewport_size({"width": 390, "height": 844})
                        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                        assert page.locator("#case-picker").bounding_box()["height"] >= 100
                        for button in [
                            page.locator("#case-picker button").last,
                            page.locator("#case-picker button").first,
                        ]:
                            button.scroll_into_view_if_needed()
                            expect(button).to_be_in_viewport()
                        page.screenshot(
                            path=str(ROOT / "docs/screenshots/mobile-policy-picker.png"), full_page=True
                        )
                    if human_first and method == "chat":
                        page.screenshot(
                            path=str(ROOT / "docs/screenshots/policy-case-picker.png"), full_page=True
                        )
                    page.locator("#case-picker button").filter(has_text="CL-2102").click()
                    idle()
                    if mobile_picker:
                        page.set_viewport_size({"width": 1280, "height": 720})
                    expect(page.locator("#sources")).to_contain_text("CL-2102")
                    expect(page.locator("#messages .assistant").last).not_to_contain_text("pathology")
                    send("POL-0000")
                    expect(page.locator("#case-picker")).to_contain_text("No matching policy")
                    expect(page.locator("#sources")).to_contain_text("CL-2102")
                    send("CL-2048")
                    expect(page.locator("#sources")).to_contain_text("CL-2048")
                    expect(page.locator("#gate-status")).to_have_text("Identity verified")
                    report["cases"][-1]["checks"].append(
                        "policy-only chat/form lookup, four candidates, switch to auto, missing policy preserves auto, switch back without re-verification"
                    )
                    context.close()
            assert not report["errors"], report["errors"]
            report["status"] = "PASS"
        except Exception as e:
            report.update(status="FAIL", error=str(e))
            raise
        finally:
            (ROOT / "docs/channel-browser-results.json").write_text(json.dumps(report, indent=2) + "\n")
            print(json.dumps(report, indent=2))
            browser.close()


if __name__ == "__main__":
    main()
