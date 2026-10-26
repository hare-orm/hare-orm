from __future__ import annotations

from typing import TYPE_CHECKING

from hare.query.expressions import F, Value

if TYPE_CHECKING:
    from tests.contracts.writes.write_scenario import WriteScenario


class WritePathActions:
    """What each write path of the matrix runs on a scenario. The scenario's rows: subjects 1 ("a") and 2 ("b",
    with the note 1), tags 1 and 2, subject 1 linked to tag 1."""

    @staticmethod
    async def save_new_row(scenario: WriteScenario, amount: int) -> None:
        await scenario.subject(id=3, name="c", amount=scenario.value(amount)).save()

    @staticmethod
    async def save_changed_row(scenario: WriteScenario, amount: int) -> None:
        subject = await scenario.fetch(scenario.subject, 1)
        subject.amount = scenario.value(amount)
        await subject.save()

    @staticmethod
    async def save_update_fields(scenario: WriteScenario, amount: int) -> None:
        subject = await scenario.fetch(scenario.subject, 1)
        subject.amount = scenario.value(amount)
        await subject.save(update_fields=["amount"])

    @staticmethod
    async def delete_instance(scenario: WriteScenario, amount: int) -> None:
        subject = await scenario.fetch(scenario.subject, 1)
        await subject.delete()

    @staticmethod
    async def hard_delete_instance(scenario: WriteScenario, amount: int) -> None:
        subject = await scenario.fetch(scenario.subject, 1)
        await subject.hard_delete()

    @staticmethod
    async def delete_instance_with_notes(scenario: WriteScenario, amount: int) -> None:
        subject = await scenario.fetch(scenario.subject, 2)
        await subject.delete()

    @staticmethod
    async def hard_delete_instance_with_notes(scenario: WriteScenario, amount: int) -> None:
        subject = await scenario.fetch(scenario.subject, 2)
        await subject.hard_delete()

    @staticmethod
    async def soft_delete_subject(scenario: WriteScenario) -> None:
        await scenario.subject.objects.filter(id=1).delete()

    @staticmethod
    async def restore_instance(scenario: WriteScenario, amount: int) -> None:
        subject = await scenario.fetch(scenario.subject, 1, include_deleted=True)
        await subject.restore()

    @staticmethod
    async def create(scenario: WriteScenario, amount: int) -> None:
        await scenario.subject.objects.create(id=3, name="c", amount=scenario.value(amount))

    @staticmethod
    async def get_or_create_creating(scenario: WriteScenario, amount: int) -> None:
        await scenario.subject.objects.get_or_create(id=3, defaults={"name": "c", "amount": scenario.value(amount)})

    @staticmethod
    async def get_or_create_finding(scenario: WriteScenario, amount: int) -> None:
        await scenario.subject.objects.get_or_create(id=1, defaults={"name": "a", "amount": scenario.value(amount)})

    @staticmethod
    async def update_or_create_updating(scenario: WriteScenario, amount: int) -> None:
        await scenario.subject.objects.update_or_create(id=1, defaults={"amount": scenario.value(amount)})

    @staticmethod
    async def update_or_create_creating(scenario: WriteScenario, amount: int) -> None:
        await scenario.subject.objects.update_or_create(id=3, defaults={"name": "c", "amount": scenario.value(amount)})

    @staticmethod
    async def bulk_create(scenario: WriteScenario, amount: int) -> None:
        await scenario.subject.objects.bulk_create(
            [
                scenario.subject(id=3, name="c", amount=scenario.value(amount)),
                scenario.subject(id=4, name="d", amount=scenario.value(amount)),
            ]
        )

    @staticmethod
    async def bulk_create_ignoring_conflicts(scenario: WriteScenario, amount: int) -> None:
        await scenario.subject.objects.bulk_create(
            [
                scenario.subject(id=1, name="a", amount=scenario.value(amount)),
                scenario.subject(id=3, name="c", amount=scenario.value(amount)),
            ],
            ignore_conflicts=True,
        )

    @staticmethod
    async def bulk_create_upserting(scenario: WriteScenario, amount: int) -> None:
        await scenario.subject.objects.bulk_create(
            [
                scenario.subject(id=1, name="a", amount=scenario.value(amount)),
                scenario.subject(id=3, name="c", amount=scenario.value(amount)),
            ],
            update_fields=["amount"],
            on_conflict=["id"],
        )

    @staticmethod
    async def bulk_update(scenario: WriteScenario, amount: int) -> None:
        subjects = await scenario.fetch_all(scenario.subject)
        for subject in subjects:
            subject.amount = scenario.value(amount)
        await scenario.subject.objects.bulk_update(subjects, ["amount"])

    @staticmethod
    async def queryset_update(scenario: WriteScenario, amount: int) -> None:
        await scenario.subject.objects.filter(id=1).update(amount=scenario.value(amount))

    @staticmethod
    async def queryset_delete(scenario: WriteScenario, amount: int) -> None:
        await scenario.subject.objects.filter(id=1).delete()

    @staticmethod
    async def queryset_hard_delete(scenario: WriteScenario, amount: int) -> None:
        await scenario.subject.objects.filter(id=1).hard_delete()

    @staticmethod
    async def queryset_restore(scenario: WriteScenario, amount: int) -> None:
        await scenario.subject.objects.include_deleted().filter(id=1).restore()

    @staticmethod
    async def insert_from(scenario: WriteScenario, amount: int) -> None:
        source = scenario.subject.objects.filter(id=1).values(
            new_id=F("id") + 2, new_name=Value("c"), new_amount=Value(scenario.value(amount))
        )
        await scenario.subject.objects.insert_from(source, fields=["id", "name", "amount"])

    @staticmethod
    async def merge(scenario: WriteScenario, amount: int) -> None:
        rows = [
            {"id": 1, "name": "a", "amount": scenario.value(amount)},
            {"id": 3, "name": "c", "amount": scenario.value(amount)},
        ]
        await (
            scenario.subject.objects.merge(rows, on="id")
            .when_matched(update={"amount": F("merge_source__amount")})
            .when_not_matched(
                insert={
                    "id": F("merge_source__id"),
                    "name": F("merge_source__name"),
                    "amount": F("merge_source__amount"),
                }
            )
        )

    @staticmethod
    async def notes_create(scenario: WriteScenario, amount: int) -> None:
        subject = await scenario.fetch(scenario.subject, 2)
        await subject.notes.create(id=2, name="n2", amount=scenario.value(amount))

    @staticmethod
    async def notes_bulk_create(scenario: WriteScenario, amount: int) -> None:
        subject = await scenario.fetch(scenario.subject, 2)
        await subject.notes.bulk_create(
            [
                scenario.note(id=2, name="n2", amount=scenario.value(amount)),
                scenario.note(id=3, name="n3", amount=scenario.value(amount)),
            ]
        )

    @staticmethod
    async def notes_get_or_create(scenario: WriteScenario, amount: int) -> None:
        subject = await scenario.fetch(scenario.subject, 2)
        await subject.notes.get_or_create(id=2, defaults={"name": "n2", "amount": scenario.value(amount)})

    @staticmethod
    async def notes_update_or_create(scenario: WriteScenario, amount: int) -> None:
        subject = await scenario.fetch(scenario.subject, 2)
        await subject.notes.update_or_create(id=1, defaults={"amount": scenario.value(amount)})

    @staticmethod
    async def notes_add(scenario: WriteScenario, amount: int) -> None:
        subject = await scenario.fetch(scenario.subject, 1)
        note = await scenario.fetch(scenario.note, 1)
        await subject.notes.add(note)

    @staticmethod
    async def notes_remove(scenario: WriteScenario, amount: int) -> None:
        subject = await scenario.fetch(scenario.subject, 2)
        note = await scenario.fetch(scenario.note, 1)
        await subject.notes.remove(note)

    @staticmethod
    async def notes_clear(scenario: WriteScenario, amount: int) -> None:
        subject = await scenario.fetch(scenario.subject, 2)
        await subject.notes.clear()

    @staticmethod
    async def notes_set(scenario: WriteScenario, amount: int) -> None:
        subject = await scenario.fetch(scenario.subject, 1)
        note = await scenario.fetch(scenario.note, 1)
        await subject.notes.set([note])

    @staticmethod
    async def tags_add(scenario: WriteScenario, amount: int) -> None:
        subject = await scenario.fetch(scenario.subject, 1)
        tag = await scenario.fetch(scenario.tag, 2)
        await subject.tags.add(tag)

    @staticmethod
    async def tags_remove(scenario: WriteScenario, amount: int) -> None:
        subject = await scenario.fetch(scenario.subject, 1)
        tag = await scenario.fetch(scenario.tag, 1)
        await subject.tags.remove(tag)

    @staticmethod
    async def tags_clear(scenario: WriteScenario, amount: int) -> None:
        subject = await scenario.fetch(scenario.subject, 1)
        await subject.tags.clear()

    @staticmethod
    async def tags_set(scenario: WriteScenario, amount: int) -> None:
        subject = await scenario.fetch(scenario.subject, 1)
        tag = await scenario.fetch(scenario.tag, 2)
        await subject.tags.set([tag])

    @staticmethod
    async def tags_create(scenario: WriteScenario, amount: int) -> None:
        subject = await scenario.fetch(scenario.subject, 1)
        await subject.tags.create(id=3, name="t3")

    @staticmethod
    async def tags_get_or_create(scenario: WriteScenario, amount: int) -> None:
        subject = await scenario.fetch(scenario.subject, 1)
        await subject.tags.get_or_create(id=3, defaults={"name": "t3"})

    @staticmethod
    async def tags_update_or_create(scenario: WriteScenario, amount: int) -> None:
        subject = await scenario.fetch(scenario.subject, 1)
        await subject.tags.update_or_create(id=3, defaults={"name": "t3"})
