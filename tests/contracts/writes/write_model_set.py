from __future__ import annotations

import dataclasses

from hare.models import Model
from tests.contracts.writes.enums import WriteRole


@dataclasses.dataclass(frozen=True)
class WriteModelSet:
    """The models of the write matrix, made for one write observer.

    Attributes:
        subject: The model most paths write.
        note: The model with a foreign key to the subject - what a relation manager and a cascade write.
        tag: The model the subject has a many-to-many relation to.
        link: The through model of that relation.
        extras: The models the observer needs besides - an outbox.
    """

    subject: type[Model]
    note: type[Model]
    tag: type[Model]
    link: type[Model]
    extras: tuple[type[Model], ...] = ()

    def get_models(self) -> list[type[Model]]:
        """Every model of the set.

        Returns:
            The extras first.
        """
        return [*self.extras, self.tag, self.subject, self.note, self.link]

    def get_model(self, role: WriteRole) -> type[Model]:
        """The model of a role.

        Args:
            role: The role.

        Returns:
            The model.
        """
        return {
            WriteRole.SUBJECT: self.subject,
            WriteRole.NOTE: self.note,
            WriteRole.TAG: self.tag,
            WriteRole.LINK: self.link,
        }[role]
