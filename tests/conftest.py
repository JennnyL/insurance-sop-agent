from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

SAMPLE = "I’m the policyholder. My name is Margaret Chen, policy POL-9921. I’m calling about my denied healthcare claim from January. DOB is 1985-03-15, SSN last four is 4472."


class Conversation:
    def __init__(self, client):
        self.client = client
        self.state = client.post("/v1/sessions").json()
        self.counter = 0

    def say(self, text="", action="message", **kwargs):
        self.counter += 1
        data = {
            "message_id": str(self.counter),
            "expected_version": self.state["version"],
            "text": text,
            "action": action,
            **kwargs,
        }
        result = self.client.post(f"/v1/sessions/{self.state['session_id']}/messages", json=data)
        assert result.status_code == 200, result.text
        self.state = result.json()
        return self.state

    def refresh(self):
        self.state = self.client.get(f"/v1/sessions/{self.state['session_id']}").json()
        return self.state

    @property
    def reply(self):
        return self.state["messages"][-1]["text"]

    def trace(self):
        return self.client.get(f"/v1/sessions/{self.state['session_id']}/trace").json()["events"]


@pytest.fixture
def app(tmp_path):
    return create_app(
        Settings(
            data_dir=Path(tmp_path),
            model_mode="offline",
            support_mode="mock",
            demo_as_of="2026-11-14",
            email_delay=0,
        )
    )


@pytest.fixture
def client(app):
    # Avoid worker races; dispatch explicitly in deterministic tests.
    return TestClient(app)


@pytest.fixture
def chat(client):
    return Conversation(client)


@pytest.fixture
def verified(chat):
    chat.say(SAMPLE)
    assert chat.state["verified"], chat.reply
    return chat
