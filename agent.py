from datetime import datetime

from dotenv import load_dotenv
from agents import Agent, Runner, function_tool

load_dotenv()


@function_tool
def get_current_datetime() -> str:
    """Return the current local date and time."""
    print("[debug] get_current_datetime tool called")
    return datetime.now().astimezone().strftime("%A, %B %d, %Y at %I:%M:%S %p %Z")


@function_tool
def calculate_tip(bill_amount: float, tip_percentage: float) -> float:
    """Calculate the tip for a bill amount and tip percentage."""
    print(f"[debug] calculate_tip called with {bill_amount=}, {tip_percentage=}")
    return bill_amount * tip_percentage / 100


agent = Agent(
    name="Assistant",
    instructions="You are a helpful general-purpose assistant. Use the date and time tool when needed.",
    tools=[get_current_datetime, calculate_tip],
)

prompt = input("You: ")
result = Runner.run_sync(agent, prompt)
print(f"Assistant: {result.final_output}")
