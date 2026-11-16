"""Bounded live-model acceptance through an already budget-guarded demo server."""

import json
import os
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent


def main():
    report = {"status": "RUNNING", "turns": [], "checks": []}
    with httpx.Client(base_url=os.getenv("DEMO_URL", "http://127.0.0.1:8000"), timeout=80) as client:
        state = client.post("/v1/sessions").json()
        report["config"] = client.get("/v1/config").json()

        def say(text="", action="message", **extra):
            nonlocal state
            response = client.post(
                f"/v1/sessions/{state['session_id']}/messages",
                json={
                    "message_id": uuid.uuid4().hex,
                    "expected_version": state["version"],
                    "text": text,
                    "action": action,
                    **extra,
                },
            )
            response.raise_for_status()
            state = response.json()
            reply = state["messages"][-1]["text"]
            report["turns"].append(
                {
                    "input": text or action,
                    "reply": reply,
                    "phase": state["phase"],
                    "verified": state["verified"],
                    "case_id": state["selected_case_id"],
                    "model_status": state["model_status"],
                    "draft": state.get("draft"),
                }
            )
            assert "couldn’t produce a reliable answer" not in reply, reply
            assert "service error interrupted" not in reply, reply
            print(json.dumps({"input": text or action, "reply": reply}, ensure_ascii=False), flush=True)
            return reply

        try:
            reply = say(
                "I already told you who I am. This is ridiculous. Just tell me why my claim was denied."
            )
            assert not state["verified"] and "CL-2048" not in reply and "pathology" not in reply.lower()
            assert any(w in reply.lower() for w in ["understand", "frustrat", "sorry"])
            say("I won't give you my SSN. What else can I use?")
            assert "ssn_last4" in state["refused_fields"]
            say("My name is Margaret Chen. I am calling about my denied healthcare claim from January.")
            assert not state["verified"]
            reply = say("My DOB is 1985-03-15 and my email is margaret@email.com.")
            assert state["verified"] and state["selected_case_id"] == "CL-2048"
            assert "Margaret" in reply and "CL-2048" in reply
            report["checks"].append(
                "Emotion, SSN alternatives, early January/denial memory, three-field gate and named confirmation"
            )
            for q in ["what is RL?", "What is the capital of France?", "Give me a pancake recipe."]:
                reply = say(q)
            assert any(w in reply.lower() for w in ["human", "representative"])
            report["checks"].append("Three unrelated questions declined with human-handling offer")
            say("i need human")
            hid = state["handoff"]["id"]
            say(action="wait_human")
            assert say("I want you to accept a late appeal") == f"System confirmation: added to {hid}."
            assert hid in say(action="resume")
            reply = say("Can you make an exception and accept my late appeal?")
            assert hid in reply and any(w in reply.lower() for w in ["representative", "human"])
            reply = say("This is ridiculous, why did nobody tell me about that deadline?")
            assert any(w in reply.lower() for w in ["notification", "notified"])
            assert any(
                w in reply.lower()
                for w in [
                    "no record",
                    "don't have",
                    "do not have",
                    "no notification",
                    "not show",
                    "can't confirm",
                    "cannot confirm",
                    "does not include",
                    "no information",
                ]
            )
            report["checks"].append(
                "Async ticket, wait acknowledgments, resume, bounded late-appeal decision and absent notification history"
            )
            say("thanks")
            assert state["closing_pending"] and not state["draft"]
            say("no")
            body = state["draft"]["body"]
            assert hid in body and "CL-2048" in body
            assert all(h in body for h in ["Conclusion:", "Discussion:", "Next steps:", "References:"])
            assert "unknown" not in body.lower() and "verification" not in body.lower()
            assert state["next_steps"]["expired"]
            say(action="email_skip")
            report["checks"].append(
                "Natural closing, model-written four-section summary, current deadline policy, H reference and skip"
            )
            state = client.post("/v1/sessions").json()
            assert state["verified"] and hid in state["messages"][-1]["text"]
            say("POL-9921")
            assert len(state["case_picker"]["matches"]) == 4
            say(action="form", form={"case_id": "CL-2102"})
            assert state["selected_case_id"] == "CL-2102"
            say("POL-0000")
            assert state["selected_case_id"] == "CL-2102" and not state["case_picker"]["matches"]
            report["checks"].append(
                "New conversation shares authorization and pending ticket; policy switching and no-match retain correct context"
            )
            report["status"] = "PASS"
        except Exception as exc:
            report["status"] = "FAIL"
            report["error"] = str(exc)
            raise
        finally:
            (ROOT / "docs/revision-live-result.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2)
            )
    print(json.dumps({"status": report["status"], "checks": report["checks"]}, indent=2))


if __name__ == "__main__":
    main()
