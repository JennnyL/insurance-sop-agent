"""Local live demo with a hidden, in-memory token and a persistent spend ceiling."""

import argparse
import asyncio
import getpass
import json
import sys
from pathlib import Path

import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings  # noqa: E402
from app.interpreter import ModelInterpreter  # noqa: E402
from app.main import create_app  # noqa: E402
from evals.budget import BudgetTransport  # noqa: E402


async def main(args):
    key = getpass.getpass("Temporary demo API key (hidden, not saved): ").strip()
    settings = Settings(
        model_mode="openai",
        model_api_key=key,
        model_base_url="https://api.openai.com/v1",
        model_name="gpt-4.1-mini",
        data_dir=Path(args.data_dir),
    )
    transport = BudgetTransport(limit_usd=args.budget, max_calls=args.max_calls, ledger_path=args.ledger)
    transport.persist()
    interpreter = ModelInterpreter(settings, transport=transport)
    if args.diagnostics:

        def record(receipt):
            with Path(args.diagnostics).open("a") as output:
                output.write(json.dumps(receipt, ensure_ascii=False) + "\n")

        interpreter.record_answer_attempt = record
    app = create_app(settings, interpreter=interpreter)
    print(
        f"Live demo listening on {args.host}:{args.port}; conservative spend cap ${args.budget:.2f}",
        flush=True,
    )
    try:
        await uvicorn.Server(
            uvicorn.Config(app, host=args.host, port=args.port, access_log=False, log_level="warning")
        ).serve()
    finally:
        transport.persist()
        await transport.finish()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--host", default="127.0.0.1", help="Bind address; use 0.0.0.0 for direct server access"
    )
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--budget", type=float, default=1.0)
    parser.add_argument(
        "--max-calls",
        type=int,
        default=1000,
        help="Physical request ceiling for this ledger (a turn uses about 2-5 calls); the dollar ceiling is always enforced too. When reached, replies fall back to the scripted assistant instead of failing",
    )
    parser.add_argument("--data-dir", default="data/live-demo")
    parser.add_argument("--ledger", default="data/live-demo-spend.json")
    parser.add_argument("--diagnostics", help="Optional synthetic-test-only draft/review JSONL output")
    args = parser.parse_args()
    if not 0 < args.budget <= 2:
        parser.error("Budget must be positive and no more than $2 for this test utility")
    if not 1 <= args.max_calls <= 1000:
        parser.error("Physical request ceiling must be between 1 and 1000")
    try:
        asyncio.run(main(args))
    except KeyboardInterrupt:
        pass
