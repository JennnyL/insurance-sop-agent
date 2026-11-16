"""Live regression: policy-only lookup, owned selection, no-match and case memory."""

import json
import os
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent


def main():
    report = {"status": "RUNNING", "checks": [], "turns": []}
    with httpx.Client(base_url=os.getenv("DEMO_URL", "http://127.0.0.1:8000"), timeout=90) as client:
        config = client.get("/v1/config").json()
        assert config["model_mode"] == "openai"
        report.update({k: config[k] for k in ["prompt_version", "response_prompt_version", "model_name"]})
        state = client.post("/v1/sessions", json={}).json()

        def say(text="", **extra):
            nonlocal state
            response = client.post(
                f"/v1/sessions/{state['session_id']}/messages",
                json={
                    "message_id": uuid.uuid4().hex,
                    "expected_version": state["version"],
                    "text": text,
                    **extra,
                },
            )
            response.raise_for_status()
            state = response.json()
            answer = state["messages"][-1]["text"]
            row = {
                "input": text or extra,
                "answer": answer,
                "verified": state["verified"],
                "selected_case_id": state["selected_case_id"],
                "model_status": state["model_status"],
            }
            report["turns"].append(row)
            print(json.dumps(row, ensure_ascii=False), flush=True)
            assert not state["model_status"] or state["model_status"].get("status") == "ok", answer
            return answer

        try:
            say(
                action="form",
                form={
                    "name": "Margaret Chen",
                    "dob": "1985-03-15",
                    "email": "margaret@email.com",
                    "case_id": "CL-2048",
                },
            )
            assert state["verified"] and state["selected_case_id"] == "CL-2048"
            epoch = state["authorization_epoch"]
            say("POL-9921")
            assert len(state["case_picker"]["matches"]) == 4
            assert state["selected_case_id"] == "CL-2048" and state["authorization_epoch"] == epoch
            report["checks"].append(
                "Policy number alone lists four owned claims; keeps previous selection and grant"
            )
            answer = say(action="form", form={"case_id": "CL-2102"})
            assert state["selected_case_id"] == "CL-2102" and "pathology" not in answer.lower()
            if not os.getenv("POLICY_SKIP_PAYMENT"):
                answer = say("How much was paid on this case?")
                assert state["selected_case_id"] == "CL-2102" and any(
                    s in answer.lower() for s in ["$0", "no payment", "nothing", "not been paid"]
                )
                report["checks"].append("Payment follow-up uses the auto record")
            report["checks"].append("Selecting auto claim replaces healthcare facts")
            for query in ["POL-0000", "CL-3001"]:
                answer = say(query)
                assert state["selected_case_id"] == "CL-2102"
                assert not state["case_picker"]["matches"]
                assert any(
                    word in answer.lower()
                    for word in ["not find", "couldn't find", "no match", "not found", "could not find"]
                ), answer
                assert state["verified"] and state["authorization_epoch"] == epoch
                assert "Ma Tian" not in answer and "diagnosis report" not in answer.lower()
            report["checks"].append(
                "Unknown policy and foreign claim leave current claim and authentication intact without disclosure"
            )
            say(action="form", form={"case_id": "CL-2048"})
            answer = say("What were we discussing on this case earlier?")
            assert state["selected_case_id"] == "CL-2048" and "denied" in answer.lower()
            assert state["authorization_epoch"] == epoch
            events = client.get(f"/v1/sessions/{state['session_id']}/trace").json()["events"]
            assert sum(e["kind"] == "verification.passed" for e in events) == 1
            report["checks"].append(
                "Returning to healthcare restores its discussion; exactly one verification across all turns"
            )
            report["status"] = "PASS"
        except Exception as error:
            report.update(status="FAIL", error=str(error))
            raise
        finally:
            (ROOT / "docs/live-policy-language-results.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n"
            )


if __name__ == "__main__":
    main()
