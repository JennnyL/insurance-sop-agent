"""Targeted live-model regression against the existing budget-guarded preview."""

import json
import os
import time
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent


def main():
    result = {"status": "RUNNING", "checks": [], "turns": []}
    with httpx.Client(base_url=os.getenv("DEMO_URL", "http://127.0.0.1:8000"), timeout=90) as c:
        config = c.get("/v1/config").json()
        assert config["model_mode"] == "openai", "This evaluator requires the live-model preview."
        result.update({k: config[k] for k in ["prompt_version", "response_prompt_version", "model_name"]})
        state = c.post("/v1/sessions", json={}).json()

        def say(text="", action="message"):
            nonlocal state
            r = c.post(
                f"/v1/sessions/{state['session_id']}/messages",
                json={
                    "message_id": uuid.uuid4().hex,
                    "expected_version": state["version"],
                    "text": text,
                    "action": action,
                },
            )
            r.raise_for_status()
            state = r.json()
            answer = state["messages"][-1]["text"]
            result["turns"].append(
                {
                    "question": text or action,
                    "answer": answer,
                    "phase": state["phase"],
                    "verified": state["verified"],
                    "model_status": state["model_status"],
                }
            )
            assert "reliable answer" not in answer and "service error" not in answer.lower(), answer
            assert not state["model_status"] or state["model_status"].get("status") == "ok", answer
            print(
                json.dumps(
                    {"turn": len(result["turns"]), "phase": state["phase"], "answer": answer},
                    ensure_ascii=False,
                ),
                flush=True,
            )
            return answer

        try:
            if os.getenv("SERVICE_LANGUAGE_REMAINING") == "1":
                say("i need human")
                assert not state["verified"] and state["handoff"]["channel"] == "LIVE_CHAT"
                say(action="resume")
                say(
                    "My name is Margaret Chen. DOB is 1985-03-15. SSN last four is 4472. I'm calling about my denied healthcare claim from January."
                )
                assert state["verified"] and state["selected_case_id"] == "CL-2048"
                epoch = state["authorization_epoch"]
                why = say("please explain why")
                assert "pathology" in why.lower() and "live representative" not in why.lower()
                assert c.get(f"/v1/sessions/{state['session_id']}/account").status_code == 200
                result["checks"].append(
                    "Human first -> explicit AI resume -> sample identity verifies -> grounded follow-up and account access; no repeated handoff receipt"
                )
            else:
                say("My name is Margaret Chen. I'm calling about my denied healthcare claim from January.")
                assert not state["verified"] and not state["evidence"]
                answer = say(
                    "I already told you who I am. This is ridiculous. Just tell me why my claim was denied. I do not want to share my SSN."
                )
                assert not state["verified"] and not state["evidence"] and "pathology" not in answer.lower()
                assert any(x in answer.lower() for x in ["frustrat", "understand", "sorry"]) and any(
                    x in answer.lower() for x in ["email", "phone"]
                )
                say("DOB is 1985-03-15. My email is margaret@email.com.")
                assert state["verified"] and state["selected_case_id"] == "CL-2048"
                result["checks"].append(
                    "Emotional refusal before verification preserves identity gate, offers non-SSN options, then uses early denied-healthcare-January context"
                )
                epoch = state["authorization_epoch"]
                why = say("please explain why")
                assert "pathology" in why.lower() and "office" in why.lower()
                how = say("how to resoleve it")
                assert how != why and any(
                    x in how.lower() for x in ["obtain", "request", "gather", "collect", "contact"]
                )
                result["checks"].append(
                    "Exact user follow-ups get distinct, grounded reason versus next-step responses"
                )
            answer = say("我想写邮件给保险公司询问这个理赔的问题，在哪里写？")
            assert "Email enquiry" in answer and "邮箱客户端" not in answer
            assert state["verified"] and state["authorization_epoch"] == epoch
            q = c.post(
                f"/v1/sessions/{state['session_id']}/inquiries",
                json={
                    "message_id": uuid.uuid4().hex,
                    "case_id": "CL-2048",
                    "subject": "Review missing records",
                    "body": "Which records should I prepare?",
                },
            ).json()
            assert q["id"].startswith("E-")
            time.sleep(4)
            answer = say("What is the progress of my email enquiry " + q["id"] + "?")
            assert q["id"] in answer and "simulat" in answer.lower()
            result["checks"].append(
                "Chinese enquiry question points to built-in insurer enquiry form; tracked simulated sending is reported without re-verification"
            )
            answer = say("i need human")
            assert "H-" in answer and state["handoff"]["channel"] == "LIVE_CHAT"
            assert state["handoff"]["status"] in ["OFFLINE", "WAITING"]
            assert state["lifecycle"] == "ACTIVE"
            result["checks"].append(
                "Human request creates online text-chat receipt and honestly reports unavailable/waiting status"
            )
            say(action="finish")
            assert state["phase"] == "POST_PROCESS" and state["draft"]
            body = state["draft"]["body"]
            assert all(x in body for x in ["Discussion:", "Next steps:", "denied"])
            say(action="email_skip")
            assert state["lifecycle"] == "COMPLETED" and state["email_operation"] is None
            assert state["verified"] and state["authorization_epoch"] == epoch
            result["checks"].append(
                "All four phases reached; mandatory summary content provided and customer skip is honored without signing out"
            )
            result["status"] = "PASS"
        except Exception as e:
            result.update(status="FAIL", error=str(e))
            raise
        finally:
            output = os.getenv("SERVICE_LANGUAGE_OUTPUT", "live-service-language-results.json")
            (ROOT / "docs" / output).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
            print(result["status"])


if __name__ == "__main__":
    main()
