"""Seed a policyholder conversation, then exercise the reported representative path."""

import json
import os
import re
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = (
    "I'm Margaret Chen, DOB 1985-03-15, email margaret@email.com. My denied healthcare claim from January."
)
REP = "I'm David Chen, calling for my mother Margaret Chen. DOB 1985-03-15, email margaret@email.com. Her denied healthcare claim from January."


def main():
    base = os.getenv("DEMO_URL", "http://127.0.0.1:8000")
    dest = ROOT / "docs" / os.getenv("REPORT_NAME", "representative-live-browser.json")
    report = {"status": "RUNNING", "base_url": base, "checks": [], "turns": [], "browser_errors": []}
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        context = browser.new_context(base_url=base, viewport={"width": 1280, "height": 900})
        page = context.new_page()
        page.on("pageerror", lambda e: report["browser_errors"].append(str(e)))

        def state(target=page):
            sid = target.evaluate("sessionStorage.getItem('sop-session')")
            return context.request.get("/v1/sessions/" + sid).json()

        def settled():
            expect(page.locator(".typing")).to_have_count(0, timeout=75000)
            expect(page.locator("#error")).to_be_hidden()

        def send(text="", selector=None):
            if selector:
                page.locator(selector).click()
            else:
                page.locator("#message").fill(text)
                page.locator("#send").click()
            settled()
            s = state()
            reply = s["messages"][-1]["text"]
            report["turns"].append(
                {"input": selector or text, "reply": reply, "phase": s["phase"], "caller": s.get("caller")}
            )
            assert "reliable answer" not in reply and "service error" not in reply, reply
            print(json.dumps({"turn": len(report["turns"]), "phase": s["phase"]}), flush=True)
            return s

        try:
            report["config"] = context.request.get("/v1/config").json()
            assert report["config"]["app_version"] == "1.7.5"
            page.goto("/")
            expect(page.locator("#send")).to_be_enabled()
            send(SAMPLE)
            send("Why did nobody tell me? How was I notified?")
            send("What document alternatives are available if I cannot get the report?")
            old = send("i need human")["handoff"]["id"]
            send(selector="#finish")
            if report["config"]["model_mode"] == "openai":
                assert "notification" in state()["draft"]["body"].lower()
            assert "alternatives" in state()["draft"]["body"].lower()
            report["checks"].append(
                "Seeded Margaret notification/document-alternative discussion, H request and summary"
            )
            page.locator("#logout").click()
            expect(page.locator("#gate-status")).to_have_text("Identity gate is locked")
            expect(page.locator("body")).not_to_contain_text(old)
            pending = send(REP)
            assert not pending["verified"] and pending["identity_count"] == 3
            assert pending["representative_consent"]["status"] == "pending"
            assert pending["messages"][-1]["text"].count("pending") == 1
            approved = send(selector='#suggestions button:has-text("Check policyholder consent")')
            reply = approved["messages"][-1]["text"]
            assert reply.startswith("Policyholder consent approved. David Chen,")
            assert len(reply.split()) < 65 and "deadline" not in reply.lower()
            assert approved["selected_case_id"] is None and approved["claim_confirmation"] == "CL-2048"
            expect(page.locator("#case-picker > p").first).to_have_text("Margaret Chen’s claims")
            selected = send(selector='#suggestions button:has-text("Yes, this claim")')
            assert selected["memory"]["case_history"]["turn_count"] == 1
            assert "denial" in selected["memory"]["case_history"]["discussed_topics"]
            assert "denial" not in selected["memory"]["pending_topics"]
            assert re.search(
                r"missing|incomplete|not (?:received|provided)|lack", selected["messages"][-1]["text"], re.I
            )
            assert selected["caller"]["name"] == "David Chen"
            assert selected["data_subject"]["name"] == "Margaret Chen"
            assert not selected["handoffs"]
            serialized = json.dumps(selected)
            assert old not in serialized and "No notification history" not in serialized
            assert "document alternatives" not in serialized
            report["checks"].append(
                "Pending/approved concise; correct caller/data subject; waits for claim confirmation and then answers deferred denial; subject-owned picker; prior dialogue and correspondence isolated"
            )
            fresh = context.new_page()
            fresh.goto("/")
            expect(fresh.locator("#gate-status")).to_contain_text("David Chen")
            expect(fresh.locator("#case-picker button")).to_have_count(4)
            expect(fresh.locator("#case-picker > p").first).to_have_text("Margaret Chen’s claims")
            expect(fresh.locator("#lookup-panel")).to_be_hidden()
            assert "representative authorization" in state(fresh)["messages"][0]["text"]
            report["checks"].append(
                "New tab shares representative grant and labels, without importing consultation content"
            )
            hid = send("i need human")["handoff"]["id"]
            before = state()["handoffs"][0]
            offered = send("Can the deadline be extended for me?")
            assert offered["handoff_note_offer"]["id"] == hid
            assert offered["handoffs"][0]["revision"] == before["revision"]
            assert "Nothing has been added yet" in offered["messages"][-1]["text"]
            added = send(selector='#suggestions button:has-text("Add question to request")')
            assert "added to " + hid in added["messages"][-1]["text"]
            assert added["handoffs"][0]["revision"] == before["revision"] + 1
            report["checks"].append(
                "Human-only decision requires explicit add-question consent; one update and visible receipt"
            )
            finished = send("no, that’s all, thanks")
            assert finished["phase"] == "POST_PROCESS" and finished["draft"]
            assert "anything else" not in finished["messages"][-1]["text"].lower()
            draft = finished["draft"]
            assert draft["recipient_label"] == "the policyholder's registered email"
            assert "David Chen as representative for policyholder Margaret Chen" in draft["body"]
            assert "policyholder's approved consent" in draft["body"]
            assert draft["body"].count("David Chen as representative") == 1
            assert "David Chen asked" in draft["body"]
            assert not re.search(r"you (?:asked|requested|wanted)|your existing request", draft["body"], re.I)
            assert not re.search(
                r"\b(?:you|your|yours)\b", draft["body"].split("Next steps:")[1].split("References:")[0], re.I
            )
            report["summary"] = draft["body"]
            assert draft["body"].lower().count("consent") == 1
            assert not re.search(r"I[, ]+David|your registered (?:email|address)", draft["body"], re.I)
            assert (
                "document alternatives" not in draft["body"]
                and "notification history" not in draft["body"].lower()
            )
            assert hid in draft["body"] and old not in draft["body"]
            assert "Customer:" not in draft["body"]
            expect(page.locator("#chat-summary")).to_be_visible()
            expect(page.locator("#chat-summary")).to_contain_text("the policyholder's registered email")
            page.screenshot(
                path=str(ROOT / "docs/screenshots/v172-representative-summary.png"), full_page=True
            )
            page.reload()
            expect(page.locator("#chat-summary")).to_be_visible()
            assert state()["draft"]["body"] == draft["body"]
            page.set_viewport_size({"width": 390, "height": 844})
            page.locator("#chat-summary h3").scroll_into_view_if_needed()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.screenshot(
                path=str(ROOT / "docs/screenshots/v172-representative-mobile.png"), full_page=True
            )
            report["checks"].append(
                "Explicit ending goes straight to inline summary; caller and consent attribution, policyholder email, fresh-only discussion; refresh/mobile remain usable"
            )
            page.locator("#logout").click()
            expect(page.locator("#gate-status")).to_have_text("Identity gate is locked")
            expect(fresh.locator("#gate-status")).to_have_text("Identity gate is locked", timeout=10000)
            expect(page.locator("body")).not_to_contain_text(hid)
            expect(page.locator("#messages")).to_contain_text("Hello")
            assert not report["browser_errors"]
            report["checks"].append(
                "Logout removes both tabs’ references, drafts and grants; welcome restored"
            )
            report["status"] = "PASS"
        except Exception as exc:
            report["status"], report["failure"] = "FAIL", str(exc)
            report["state_at_failure"] = state()
            raise
        finally:
            dest.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
            browser.close()
    print(json.dumps({"status": report["status"], "checks": len(report["checks"])}))


if __name__ == "__main__":
    main()
