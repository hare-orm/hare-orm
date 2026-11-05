from __future__ import annotations

from dataclasses import dataclass

from hare.transactions.enums import TransactionOutcome, TransactionPhase


@dataclass(slots=True)
class TransactionState:
    """One wrapper's local phase and server outcome, shared with cleanup and observers.

    FINISHED forbids further statements but does not mean committed. PREPARED awaits a
    distributed decision; RELEASED is only a savepoint release. Callback and connection
    release failures never change an already recorded outcome.
    """

    phase: TransactionPhase = TransactionPhase.NEW
    outcome: TransactionOutcome = TransactionOutcome.PENDING

    @property
    def finished(self) -> bool:
        return self.phase is TransactionPhase.FINISHED

    @property
    def ending(self) -> bool:
        return self.phase is TransactionPhase.ENDING

    def start(self) -> None:
        """Record a successful BEGIN without reviving an abandoned transaction."""
        if self.phase is TransactionPhase.NEW:
            self.phase = TransactionPhase.ACTIVE

    def finish(self, outcome: TransactionOutcome) -> None:
        """Record the server result before callbacks or resource release run."""
        self.outcome = outcome
        self.phase = TransactionPhase.FINISHED
