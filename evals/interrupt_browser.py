"""Real browser interruption races against an isolated app with a delayed offline interpreter."""

import asyncio
import json
import socket
import sys
import tempfile
from pathlib import Path

import uvicorn
from playwright.async_api import async_playwright, expect

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import ROOT, Settings  # noqa: E402
from app.interpreter import OfflineInterpreter  # noqa: E402
from app.main import create_app  # noqa: E402


class DelayedInterpreter(OfflineInterpreter):
    async def observe(self, text, context):
        await asyncio.sleep(1)
        return await super().observe(text, context)


async def send(page, text):
    await page.locator("#message").fill(text)
    await page.locator("#send").click()


async def idle(page):
    await expect(page.locator("#stop-reply")).to_be_hidden(timeout=10000)


async def main():
    result = {
        "status": "RUNNING",
        "kind": "browser_races_with_delayed_offline_interpreter",
        "checks": [],
        "browser_errors": [],
    }
    with tempfile.TemporaryDirectory(prefix="sop-interrupt-browser-") as directory:
        settings = Settings(
            data_dir=Path(directory), model_mode="offline", support_mode="mock", email_delay=3
        )
        app = create_app(settings, interpreter=DelayedInterpreter())
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen(128)
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
        task = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            async with asyncio.timeout(10):
                while not server.started:
                    await asyncio.sleep(0.02)
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                context = await browser.new_context(viewport={"width": 1440, "height": 1000})
                page = await context.new_page()
                page.on("pageerror", lambda e: result["browser_errors"].append(str(e)))
                await page.goto(f"http://127.0.0.1:{port}")
                await expect(page.locator("#gate-status")).to_have_text("Identity gate is locked")
                await expect(page.locator("#send")).to_be_enabled()
                await send(page, "My name is Margaret Chen. My denied healthcare claim from January.")
                await expect(page.locator("#stop-reply")).to_be_visible()
                await page.locator("#message").fill("Draft that must survive the pending reply")
                await idle(page)
                await expect(page.locator("#message")).to_have_value(
                    "Draft that must survive the pending reply"
                )
                await expect(page.locator("#identity-progress")).to_have_text(
                    "1 of 3 identity fields collected"
                )
                result["checks"].append("new draft survives earlier reply completion")

                await send(page, "DOB is 1900-01-01. SSN last four is 0000.")
                await expect(page.locator("#stop-reply")).to_be_visible()
                await send(page, "DOB is 1985-03-15. SSN last four is 4472.")
                await expect(page.locator("#gate-status")).to_have_text("Identity verified", timeout=10000)
                await idle(page)
                await expect(page.locator("#sources")).to_contain_text("CL-2048")
                await expect(page.locator("#error")).to_be_hidden()
                result["checks"].append(
                    "new message interrupts old reply and preserves prior identity and case hints"
                )

                await page.locator("#finish").click()
                await page.locator("#review-summary").click()
                await expect(page.locator("#email-send")).to_be_visible()
                await page.locator("#email-send").click()
                await idle(page)
                await page.locator("#account-close").click()
                await send(page, "Wait, how much has actually been paid?")
                await expect(page.locator("#stop-reply")).to_be_visible()
                await page.locator("#stop-reply").click()
                await idle(page)
                await expect(page.locator("#email-status")).to_contain_text("paused")
                # Past the dispatch window: stopped work must not release the mail.
                await asyncio.sleep(3.2)
                await expect(page.locator("#lifecycle")).to_have_text("ACTIVE")
                await expect(page.locator("#email-status")).to_contain_text("paused")
                await page.locator("#review-summary").click()
                await page.locator("#email-cancel").click()
                await idle(page)
                await page.locator("#account-close").click()
                result["checks"].append("stop reply pauses queued email across the worker dispatch window")

                await send(page, "How much was paid?")
                await expect(page.locator("#stop-reply")).to_be_visible()
                await page.locator("#human").click()
                await expect(page.locator("#lifecycle")).to_have_text("ACTIVE")
                await expect(page.locator("#messages")).to_contain_text("no real person is contacted")
                await idle(page)
                await page.locator("#support-open").click()
                await expect(page.locator("#support-list")).to_contain_text("H-")
                await page.locator("#account-close").click()
                result["checks"].append("human help interrupts a pending reply and leaves chat active")
                await page.locator("#reset").click()
                await idle(page)

                await page.locator("#logout").click()
                await expect(page.locator("#gate-status")).to_have_text("Identity gate is locked")

                # Hold an HTTP reply AFTER the server has committed verification.
                # Reset must survive the stale version and ignore this late reply.
                committed, release = asyncio.Event(), asyncio.Event()
                held = False

                async def hold_first(route):
                    nonlocal held
                    payload = route.request.post_data_json
                    if not held and payload.get("action") == "message":
                        held = True
                        response = await route.fetch()
                        committed.set()
                        await release.wait()
                        try:
                            await route.fulfill(response=response)
                        except Exception:
                            pass  # The browser is expected to abort this fetch.
                    else:
                        await route.continue_()

                await page.route("**/messages", hold_first)
                await page.locator("#sample").click()
                await page.locator("#send").click()
                await asyncio.wait_for(committed.wait(), 10)
                await page.locator("#reset").click()
                await idle(page)
                release.set()
                await expect(page.locator("#gate-status")).to_have_text("Identity verified")
                await expect(page.locator("#identity-progress")).to_have_text("3 distinct fields verified")
                await expect(page.locator("#sources")).not_to_contain_text("CL-2048")
                await expect(page.locator("#error")).to_be_hidden()
                await page.unroute("**/messages", hold_first)
                result["checks"].append(
                    "reset handles a committed but delayed response without stale rendering"
                )

                await page.set_viewport_size({"width": 390, "height": 844})
                await send(page, "My name is Margaret Chen.")
                await expect(page.locator("#stop-reply")).to_be_visible()
                await page.locator("#message").fill("Unsent correction on mobile")
                await page.locator("#stop-reply").click()
                await idle(page)
                await expect(page.locator("#message")).to_have_value("Unsent correction on mobile")
                assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                result["checks"].append("mobile stop control keeps unsent draft without horizontal overflow")
                assert not result["browser_errors"]
                await browser.close()
                result["status"] = "PASS"
        except Exception as exc:
            result.update(status="FAIL", error=str(exc))
            raise
        finally:
            server.should_exit = True
            await task
            (ROOT / "evals/interrupt-browser-results.json").write_text(json.dumps(result, indent=2) + "\n")
            print(json.dumps(result, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
