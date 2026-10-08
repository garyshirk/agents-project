from dataclasses import dataclass, field

from arbitrage.contracts import EconomicCostFindingsResult


class AcquisitionCaptureError(RuntimeError):
    pass


@dataclass
class AcquisitionCaptureState:
    attempts: int = 0
    successful_results: list[EconomicCostFindingsResult] = field(default_factory=list)

    def begin(self) -> None:
        self.attempts = 0
        self.successful_results.clear()

    def begin_action(self) -> None:
        self.attempts += 1
        if self.attempts > 1:
            raise AcquisitionCaptureError(
                "Acquisition capture requires exactly one terminal action"
            )

    def select(self, result: EconomicCostFindingsResult) -> None:
        self.successful_results.append(result)

    def complete(self) -> EconomicCostFindingsResult:
        if self.attempts != 1 or len(self.successful_results) != 1:
            raise AcquisitionCaptureError(
                "Acquisition capture requires exactly one successful terminal action"
            )
        return self.successful_results[0]
