"""Regenerate the reviewable API and model contracts without a live model."""

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402
from app.responder import AnswerDraft, AnswerReview  # noqa: E402
from app.schemas import HandoffUpdate, InquiryCreate, InquiryUpdate, Observation, TurnRequest  # noqa: E402


def main():
    dest = ROOT / "docs/contracts"
    dest.mkdir(exist_ok=True)
    for name, cls in [
        ("turn-request", TurnRequest),
        ("turn-observation", Observation),
        ("answer-draft", AnswerDraft),
        ("answer-review", AnswerReview),
        ("inquiry-create", InquiryCreate),
        ("inquiry-update", InquiryUpdate),
        ("handoff-update", HandoffUpdate),
    ]:
        (dest / (name + ".schema.json")).write_text(json.dumps(cls.model_json_schema(), indent=2) + "\n")
    with tempfile.TemporaryDirectory() as data:
        app = create_app(Settings(data_dir=Path(data), model_mode="offline"))
        (dest / "openapi.json").write_text(json.dumps(app.openapi(), indent=2) + "\n")


if __name__ == "__main__":
    main()
