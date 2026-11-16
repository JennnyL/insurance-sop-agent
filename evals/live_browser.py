"""Exercise the real live UI through the separately budget-limited local preview."""

import json
import os
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parent.parent


def main():
    result = {"status": "RUNNING", "checks": [], "replies": [], "browser_errors": []}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.on("pageerror", lambda error: result["browser_errors"].append(str(error)))
        try:
            page.goto(os.getenv("DEMO_URL", "http://127.0.0.1:8000"))
            expect(page.locator("#mode")).to_have_text("LIVE MODEL")
            expect(page.locator("#send")).to_be_enabled()
            page.locator("#lookup-panel > summary").click()
            page.locator("#lookup-sample").click()
            page.locator("#lookup-submit").click()
            expect(page.locator("#gate-status")).to_have_text("Identity verified", timeout=65000)
            expect(page.locator("#stop-reply")).to_be_hidden(timeout=65000)
            result["checks"].append("form verifies through live UI")

            def ask(text):
                page.locator("#message").fill(text)
                page.locator("#send").click()
                expect(page.locator("#stop-reply")).to_be_visible()
                expect(page.locator("#stop-reply")).to_be_hidden(timeout=65000)
                reply = page.locator(".message.assistant .bubble").last.inner_text()
                assert "couldn’t produce a reliable answer" not in reply, reply
                assert "service error" not in reply.lower(), reply
                result["replies"].append({"question": text, "answer": reply})
                return reply

            steps = ask("how to resoleve it")
            reason = ask("please explain why")
            assert steps != reason
            assert "pathology" in reason.lower() and "office" in reason.lower()
            assert "pathology" in ask("why").lower()
            result["checks"].append("the user's exact follow-ups get contextual model-written answers")
            page.locator(".recent-cases button", has_text="CL-2048").wait_for()

            def switch(case):
                page.locator("#lookup-panel > summary").click()
                page.locator("#lookup-claim").fill(case)
                page.locator("#lookup-submit").click()
                expect(page.locator("#stop-reply")).to_be_visible()
                expect(page.locator("#stop-reply")).to_be_hidden(timeout=65000)
                expect(page.locator("#sources")).to_contain_text(case)

            switch("CL-2102")
            assert "CL-2102" in ask("What is the status of this case?")
            page.locator(".recent-cases button", has_text="CL-2048").click()
            expect(page.locator("#stop-reply")).to_be_visible()
            expect(page.locator("#stop-reply")).to_be_hidden(timeout=65000)
            memory = ask("What were we discussing on this case earlier?")
            assert "pathology" in memory.lower() or "deni" in memory.lower()
            assert "auto" not in memory.lower()
            expect(page.locator("#memory")).to_contain_text("saved turns")
            result["checks"].append("returning to a case restores its own remembered conversation")
            page.screenshot(path=str(ROOT / "docs/screenshots/live-context-and-memory.png"), full_page=True)
            assert not result["browser_errors"]
            result["status"] = "PASS"
        except Exception as exc:
            result.update(status="FAIL", error=str(exc))
        finally:
            browser.close()
            (ROOT / "docs/live-browser-results.json").write_text(
                json.dumps(result, indent=2, ensure_ascii=False) + "\n"
            )
            print(json.dumps({k: result[k] for k in ("status", "checks", "browser_errors")}, indent=2))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
