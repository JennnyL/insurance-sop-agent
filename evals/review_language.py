"""Additional live multi-turn acceptance against the running budgeted demo."""

import json
import os
import re
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent


def main():
    report = {"status": "RUNNING", "journeys": [], "turns": []}
    base = os.getenv("DEMO_URL", "http://127.0.0.1:8000")

    def create():
        client = httpx.Client(base_url=base, timeout=80)
        response = client.post("/v1/sessions", json={})
        response.raise_for_status()
        return client, response.json()

    def say(client, state, text="", action="message"):
        response = client.post(
            f"/v1/sessions/{state['session_id']}/messages",
            json={
                "message_id": uuid.uuid4().hex,
                "expected_version": state["version"],
                "text": text,
                "action": action,
            },
        )
        response.raise_for_status()
        result = response.json()
        reply = result["messages"][-1]["text"]
        report["turns"].append(
            {
                "input": text or action,
                "reply": reply,
                "phase": result["phase"],
                "verified": result["verified"],
                "identity_count": result["identity_count"],
                "consent": result["representative_consent"],
            }
        )
        assert "couldn’t produce a reliable answer" not in reply, reply
        assert "service error interrupted" not in reply, reply
        print(
            json.dumps(
                {"turn": len(report["turns"]), "phase": result["phase"], "verified": result["verified"]}
            ),
            flush=True,
        )
        return result

    try:
        client, state = create()
        for text, count in [
            (
                "I'm David Chen, calling for my mother Margaret Chen about her denied healthcare claim from January.",
                1,
            ),
            ("Her DOB is 1985-03-15.", 2),
            ("Her email is margaret@email.com.", 3),
        ]:
            state = say(client, state, text)
            assert not state["verified"] and state["identity_count"] == count
            assert "CL-2048" not in state["messages"][-1]["text"]
        assert state["representative_consent"]["status"] == "pending"
        for _ in range(4):
            state = say(client, state, action="consent_timeout")
        assert state["representative_consent"]["status"] == "timeout"
        hid = re.search(r"H-[A-Z0-9]+", state["messages"][-1]["text"]).group()
        assert f"Created human handling request {hid}" in state["messages"][-1]["text"]
        state = say(client, state, action="wait_human")
        state = say(client, state, "Please help obtain policyholder consent")
        assert state["messages"][-1]["text"] == f"System confirmation: added to {hid}."
        client.close()
        report["journeys"].append(
            "David/Margaret: partial policyholder fields, pending consent, timeout, H receipt, wait acknowledgment, no claim disclosure"
        )

        client, state = create()
        state = say(
            client,
            state,
            "My name is Ma Tian. DOB is 1964-09-10. National ID last four is 6688. I'm calling about my denied healthcare claim.",
        )
        assert not state["verified"] and state["identity_count"] == 2
        state = say(client, state, "My email is matian@example.com.")
        assert state["verified"] and state["selected_case_id"] == "CL-3001"
        assert "verified" in state["messages"][-1]["text"].lower()
        client.close()
        report["journeys"].append(
            "Ma Tian: national ID is not SSN; name/DOB/email verify; remembered claim resolves to CL-3001"
        )

        client, state = create()
        state = say(
            client,
            state,
            "My name is Yaven Li. DOB is 1989-12-03. Email is yawen.li@example.com. What claims do I have?",
        )
        assert state["verified"] and state["identity_count"] == 3
        account = client.get(f"/v1/sessions/{state['session_id']}/account").json()
        assert account["profile"]["name"] == "Ya Wen Li" and account["claims"] == []
        client.close()
        report["journeys"].append(
            "Ya Wen Li: name and email aliases both accepted; verified no-claims account, no invented record"
        )
        report["status"] = "PASS"
    except Exception as exc:
        report.update(status="FAIL", error=str(exc))
        raise
    finally:
        (ROOT / "docs/review-live-language-result.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        )
    print(report["status"])


if __name__ == "__main__":
    main()
