from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Target:
    """One ORM set-up the benchmark measures, on one database.

    Attributes:
        key: The name on the command line and in results.json.
        name: The name on the charts.
        database: The key of the ``Database`` it runs on.
        suite: Which scenario implementation runs it - a key of ``Runner.SUITES``.
        family: The row of the summary table it stands in - the same ORM on every database
            (``SQLAlchemy`` for clickhouse-sqlalchemy too).
        distribution: The package whose version the results record.
        light_colour: The series colour on a light background - the same for one ORM on every
            database, so an ORM keeps its colour across the pages.
        dark_colour: The series colour on a dark background.
        driver_english: How the pages name the target's driver after "on" - for a target of
            ``BASELINE_DISTRIBUTION``, whose drivers the summary compares.
        driver_russian: The same after the Russian "на".
        environment: The virtual environment it runs in (``.bench-venv-<environment>``) when its
            packages can't share the main one; None for the main one.
    """

    key: str
    name: str
    database: str
    suite: str
    family: str
    distribution: str
    light_colour: str
    dark_colour: str
    driver_english: str = ""
    driver_russian: str = ""
    environment: str | None = None
