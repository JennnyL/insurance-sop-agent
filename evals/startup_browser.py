"""Browser startup failures must recover visibly without losing valid sessions."""

import json
import os
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parent.parent


def main():
    result = {"status": "RUNNING", "checks": [], "browser_errors": []}
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        base = os.getenv("DEMO_URL", "http://127.0.0.1:8003")
        context = browser.new_context(base_url=base)
        page = context.new_page()
        page.on("pageerror", lambda error: result["browser_errors"].append(str(error)))
        try:
            page.goto("/")
            expect(page.locator("#send")).to_be_enabled()
            expect(page.locator("#startup-status")).to_be_hidden()
            page.locator("#lookup-panel > summary").click()
            page.locator("#lookup-sample").click()
            page.locator("#lookup-submit").click()
            expect(page.locator("#gate-status")).to_have_text("Identity verified")
            sid = page.evaluate("sessionStorage.getItem('sop-session')")
            result["checks"].append("Normal startup connects, hides startup status and verifies sample")

            endpoint = f"**/v1/sessions/{sid}"
            page.route(
                endpoint,
                lambda route: route.fulfill(
                    status=503, content_type="application/json", body='{"error":"TEMPORARY_FAILURE"}'
                ),
            )
            page.reload()
            expect(page.locator("#startup-status")).to_contain_text("TEMPORARY_FAILURE")
            assert page.evaluate("sessionStorage.getItem('sop-session')") == sid
            page.unroute(endpoint)
            page.locator("#startup-retry").click()
            expect(page.locator("#gate-status")).to_have_text("Identity verified")
            assert page.evaluate("sessionStorage.getItem('sop-session')") == sid
            result["checks"].append(
                "Transient session-read failure is visible; retry restores the same verified session"
            )

            page.route(
                "**/v1/config",
                lambda route: route.fulfill(
                    status=200, content_type="text/html", body="<html>Wrong forwarded service</html>"
                ),
            )
            page.reload()
            expect(page.locator("#startup-status")).to_contain_text("Check the port or forwarded URL")
            page.unroute("**/v1/config")
            result["checks"].append(
                "Wrong-service HTML response explains address/port mismatch instead of staying blank"
            )

            pending = []
            page.route("**/v1/config", lambda route: pending.append(route))
            page.reload(wait_until="domcontentloaded")
            expect(page.locator("#startup-status")).to_contain_text("Connection timed out", timeout=16000)
            page.unroute("**/v1/config")
            page.locator("#startup-retry").click()
            expect(page.locator("#send")).to_be_enabled()
            result["checks"].append("Stalled startup request times out in 12 seconds and Retry reconnects")

            page.route("**/static/portal.js*", lambda route: route.abort())
            page.reload(wait_until="domcontentloaded")
            expect(page.locator("#startup-status")).to_contain_text("could not finish loading")
            page.unroute("**/static/portal.js*")
            result["checks"].append("Blocked JavaScript asset produces a visible reload action")
            context.close()

            context = browser.new_context(base_url=base)
            context.add_init_script(
                "Object.defineProperty(window, 'sessionStorage', {get(){throw new DOMException('Access denied','SecurityError')}}); Object.defineProperty(crypto, 'randomUUID', {value: undefined});"
            )
            page = context.new_page()
            page.on("pageerror", lambda error: result["browser_errors"].append(str(error)))
            page.goto("/")
            expect(page.locator("#send")).to_be_enabled()
            expect(page.locator("#startup-status")).to_be_hidden()
            page.locator("#lookup-panel > summary").click()
            page.locator("#lookup-sample").click()
            page.locator("#lookup-submit").click()
            expect(page.locator("#gate-status")).to_have_text("Identity verified")
            assert page.evaluate("/^[a-f0-9-]{36}$/.test(requestId())")
            result["checks"].append(
                "Denied browser storage and unavailable randomUUID still permit startup and form submission"
            )
            assert not result["browser_errors"], result["browser_errors"]
            result["status"] = "PASS"
        except Exception as error:
            result.update(status="FAIL", error=str(error))
            raise
        finally:
            (ROOT / "docs/startup-browser-results.json").write_text(json.dumps(result, indent=2) + "\n")
            print(json.dumps(result, indent=2))
            browser.close()


if __name__ == "__main__":
    main()
