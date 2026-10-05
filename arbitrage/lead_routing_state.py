from dataclasses import dataclass, field

from arbitrage.contracts import LeadDecision


class LeadRoutingError(RuntimeError):
    """Raised when a Lead run does not select exactly one terminal action."""


@dataclass
class LeadRoutingState:
    decisions: list[LeadDecision] = field(default_factory=list)

    def begin(self) -> None:
        self.decisions.clear()

    def select(self, decision: LeadDecision) -> None:
        self.decisions.append(decision)
        print(f"[debug] Lead routing action selected: {decision.disposition.value}")
        if len(self.decisions) > 1:
            raise LeadRoutingError(
                "Lead Qualifier selected multiple terminal routing actions"
            )

    def complete(self) -> LeadDecision:
        if not self.decisions:
            raise LeadRoutingError(
                "Lead Qualifier completed without selecting a terminal routing action"
            )
        if len(self.decisions) > 1:
            raise LeadRoutingError(
                "Lead Qualifier selected multiple terminal routing actions"
            )
        return self.decisions[0]
