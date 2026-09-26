import asyncio
import os
from pathlib import Path

from agents import RunConfig, Runner, SQLiteSession
from dotenv import load_dotenv

from arbitrage.candidate_workflow import ApplicationContext, CandidateWorkflow
from arbitrage.contracts import CandidateWorkflowResult, LeadDecision
from arbitrage.coordinator import ArbitrageCoordinator
from arbitrage.persistence import CandidateRepository


DEFAULT_SESSION_ID = "default_conversation"
SESSION_ID_ENVIRONMENT_VARIABLE = "ARBITRAGE_SESSION_ID"
SESSIONS_DATABASE_PATH = Path(__file__).resolve().parent.parent / "sessions.db"


def get_session_id() -> str:
    return os.getenv(SESSION_ID_ENVIRONMENT_VARIABLE, DEFAULT_SESSION_ID)


def qualification_input(session: SQLiteSession, prompt: str, context: str):
    return [
        *asyncio.run(session.get_items()),
        {
            "role": "user",
            "content": (
                f"{prompt}\n\n[Application-owned active investigation context]\n{context}"
            ),
        },
    ]


def record_application_response(
    session: SQLiteSession, prompt: str, response: str
) -> None:
    asyncio.run(
        session.add_items(
            [
                {"role": "user", "content": prompt},
                {"role": "assistant", "content": response},
            ]
        )
    )


def main() -> None:
    load_dotenv()

    from arbitrage.orchestration import agent, lead_qualifier

    run_config = RunConfig(
        workflow_name="Learning agent workflow",
        group_id="default_conversation",
    )

    session = SQLiteSession(
        get_session_id(),
        SESSIONS_DATABASE_PATH,
    )
    context = ApplicationContext(
        candidate_workflow=CandidateWorkflow(CandidateRepository())
    )
    coordinator = ArbitrageCoordinator(context.candidate_workflow)

    try:
        while True:
            prompt = input("You: ")
            if prompt.strip().lower() in {"exit", "quit"}:
                break

            print("[debug] Lead Qualifier called")
            qualification = Runner.run_sync(
                lead_qualifier,
                qualification_input(
                    session,
                    prompt,
                    coordinator.qualification_context(),
                ),
                context=context,
                run_config=run_config,
            )
            decision = qualification.final_output_as(
                LeadDecision, raise_if_incorrect_type=True
            )
            print(f"[debug] Lead decision: {decision.disposition.value}")

            def evaluate(_workflow_result: CandidateWorkflowResult) -> str:
                print("[debug] Substantive evaluation started")
                result = Runner.run_sync(
                    agent,
                    prompt,
                    context=context,
                    session=session,
                    run_config=run_config,
                )
                return str(result.final_output)

            outcome = coordinator.coordinate(decision, evaluate)
            if not outcome.proceeded:
                record_application_response(session, prompt, outcome.response)
            print(f"Assistant: {outcome.response}")
    finally:
        session.close()
