from pathlib import Path

from agents import RunConfig, Runner, SQLiteSession
from dotenv import load_dotenv


def main() -> None:
    load_dotenv()

    from arbitrage.orchestration import agent

    run_config = RunConfig(
        workflow_name="Learning agent workflow",
        group_id="default_conversation",
    )

    session = SQLiteSession(
        "default_conversation",
        Path(__file__).resolve().parent.parent / "sessions.db",
    )

    try:
        while True:
            prompt = input("You: ")
            if prompt.strip().lower() in {"exit", "quit"}:
                break

            result = Runner.run_sync(agent, prompt, session=session, run_config=run_config)
            print(f"Assistant: {result.final_output}")
    finally:
        session.close()
