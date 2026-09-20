from datetime import datetime

from agents import function_tool


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
