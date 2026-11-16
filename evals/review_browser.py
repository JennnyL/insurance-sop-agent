"""P0/P1 review acceptance in Chromium; works with offline or live model servers."""

import json
import os
import re
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
REP = "I'm David Chen, calling on behalf of Margaret Chen. Her DOB is 1985-03-15. Her email is margaret@email.com. Her denied healthcare claim from January."


def main():
    report = {"status": "RUNNING", "checks": [], "errors": []}
    dest = ROOT / "docs" / os.getenv("REPORT_NAME", "review-browser-result.json")
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        context = browser.new_context(base_url=os.getenv("DEMO_URL", "http://127.0.0.1:8003"))
        page = context.new_page()
        page.on("pageerror", lambda e: report["errors"].append(str(e)))
        page.goto("/")
        expect(page.locator("#send")).to_be_enabled()

        def settled(target=page):
            expect(target.locator(".typing")).to_have_count(0, timeout=75000)
            expect(target.locator("#error")).to_be_hidden()
            expect(target.locator("#messages .assistant").last).not_to_contain_text(
                "couldn’t produce a reliable answer"
            )

        def send(text, target=page):
            target.locator("#message").fill(text)
            target.locator("#send").click()
            settled(target)

        def click(selector, target=page):
            target.locator(selector).click()
            settled(target)

        try:
            send(REP)
            expect(page.locator("#identity-progress")).to_have_text("3 of 3 identity fields collected")
            expect(page.locator("#gate-status")).to_have_text("Identity gate is locked")
            expect(page.locator("#messages .assistant").last).to_contain_text("pending")
            expect(page.locator("#messages .assistant").last).not_to_contain_text("CL-2048")
            click('#suggestions button:has-text("Check policyholder consent")')
            expect(page.locator("#gate-status")).to_contain_text("David Chen")
            expect(page.locator("#messages .assistant").last).to_contain_text("Policyholder consent approved")
            expect(page.locator("#messages .assistant").last).to_contain_text("CL-2048")
            report["checks"].append(
                "Representative: three policyholder fields, locked consent pending, approved fixture grant and visible receipt"
            )

            click('#suggestions button:has-text("Yes, this claim")')
            second = context.new_page()
            second.goto("/")
            expect(second.locator("#gate-status")).to_contain_text("David Chen")
            expect(second.locator("#case-picker button")).to_have_count(4)
            expect(second.locator("#lookup-panel")).to_be_hidden()
            expect(second.locator("#messages")).not_to_contain_text("tell me your policy number")
            click('#case-picker button:has-text("CL-2048")', second)
            expect(second.locator("#sources")).to_contain_text("CL-2048")
            report["checks"].append(
                "New tab: shared representative authorization, four claim choices, hidden identity form"
            )

            send("This is ridiculous, why did nobody tell me about the deadline?")
            emotion = page.locator("#messages .assistant").last.inner_text()
            assert re.search("frustrat|upset|sorry", emotion, re.I), emotion
            send("i need human")
            human = page.locator("#messages .assistant").last.inner_text()
            assert "frustrating" not in human
            hid = re.search(r"H-[A-Z0-9]+", human).group()
            send("i need human")
            expect(page.locator("#messages .assistant").last).to_contain_text(f"already have request {hid}")
            send("Can the deadline be extended for me?")
            answer = page.locator("#messages .assistant").last.inner_text()
            assert hid in answer and "frustrating" not in answer
            assert not re.search(r"recommend submitting|submit a human", answer, re.I)
            report["checks"].append(
                "Emotion stays in current turn; human request reuse and decision note are acknowledged with the existing reference"
            )

            click('#suggestions button:has-text("Add question to request")')
            click("#wait-human")
            expect(page.locator("#messages .system").last).to_contain_text("wait-only")
            assert "wait_human" not in page.locator("#messages .user").all_inner_texts()
            send("Please note my concern")
            expect(page.locator("#messages .assistant").last).to_contain_text(
                "System confirmation: added to " + hid
            )
            page.reload()
            expect(page.locator("#human-chat-label")).to_contain_text("Wait only")
            expect(page.locator("#gate-status")).to_contain_text("David Chen")
            expect(page.locator("#messages .assistant").last).to_contain_text(hid)
            click("#chat-mode-toggle")
            expect(page.locator("#messages .system").last).to_contain_text("Continued with AI")
            report["checks"].append(
                "Mode buttons are system events; refresh restores authorization, claim, full messages, ticket and wait mode together"
            )

            for question in ["What is RL?", "write Python code", "give me a recipe"]:
                send(question)
                reply = page.locator("#messages .assistant").last.inner_text()
                assert "abbreviation" not in reply and any(
                    w in reply for w in ["unrelated", "stay with insurance", "only help with insurance"]
                ), reply
            expect(page.locator("#messages .assistant").last).to_contain_text(hid)
            click("#finish")
            expect(page.locator("#account-dialog")).not_to_be_visible()
            expect(page.locator("#chat-summary")).to_be_visible()
            body = page.locator("#chat-summary pre").inner_text()
            assert "References:" in body and hid in body
            assert not re.search("simulat|demo|no real contact", body, re.I)
            expect(page.locator("#chat-summary footer")).to_contain_text("simulated")
            report["checks"].append(
                "In-chat summary card: customer-facing body, existing H reference, Send/Skip and separate demo footer"
            )
            expect(page.locator("#chat-summary h3")).to_be_in_viewport()
            page.screenshot(path=str(ROOT / "docs/screenshots/v16-summary-desktop.png"), full_page=True)
            page.set_viewport_size({"width": 390, "height": 844})
            page.locator("#chat-summary h3").scroll_into_view_if_needed()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.screenshot(path=str(ROOT / "docs/screenshots/v16-summary-mobile.png"), full_page=True)
            page.set_viewport_size({"width": 1280, "height": 900})

            page.locator("#logout").click()
            expect(page.locator("#gate-status")).to_have_text("Identity gate is locked")
            expect(page.locator("#messages")).to_contain_text("Hello")
            expect(page.locator("#human-chat-bar")).to_be_hidden()
            expect(page.locator("#support-count")).to_have_text("")
            expect(page.locator("body")).not_to_contain_text(hid)
            expect(second.locator("#gate-status")).to_have_text("Identity gate is locked", timeout=10000)
            expect(second.locator("body")).not_to_contain_text(hid)
            page.reload()
            expect(page.locator("#messages")).to_contain_text("Hello")
            expect(page.locator("body")).not_to_contain_text(hid)
            send("I don't want to share my SSN")
            expect(page.locator("#messages .assistant").last).to_contain_text("do not have to provide an SSN")
            expect(page.locator("#messages .assistant").last).to_contain_text(
                "Email enquiry and My claims require verification"
            )
            report["checks"].append(
                "Logout clears every tab, request reference/count/banner and old summary; welcome and fixed SSN alternatives remain"
            )
            assert not report["errors"]
            report["status"] = "PASS"
        except Exception as exc:
            report["status"] = "FAIL"
            report["failure"] = str(exc)
            report["last_reply"] = page.locator("#messages .assistant").last.inner_text()
            raise
        finally:
            dest.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
            browser.close()
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
