from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any, ClassVar

from orm_benchmark.constants import (
    BASELINE_DISTRIBUTION,
    BENCHMARK_DIRECTORY,
    DOCS_DIRECTORY,
    LOAD_CONCURRENCY,
    LOAD_OPERATIONS,
    POOL_MAX_SIZE,
    REPOSITORY,
    SIZES,
)
from orm_benchmark.declarations.targets import TARGET_BY_KEY
from orm_benchmark.definitions.database import Database
from orm_benchmark.output.chart_writer import ChartWriter


class BenchmarkPages:
    """Every text about the benchmark, written from the report and the benchmark's own settings: the
    docs' benchmark pages (whole) - the summary and a page per database - the Benchmarks section of
    both READMEs and the scenario and method lists of the benchmark's README (between their markers).
    Nothing there is edited by hand."""

    READMES: ClassVar[dict[str, Path]] = {"en": REPOSITORY / "README.md", "ru": REPOSITORY / "README.ru.md"}
    BENCHMARK_READMES: ClassVar[dict[str, Path]] = {
        "en": BENCHMARK_DIRECTORY / "README.md",
        "ru": BENCHMARK_DIRECTORY / "README.ru.md",
    }
    SITE_URL = "https://hare-orm.github.io/hare-orm/"
    REPOSITORY_URL = "https://github.com/hare-orm/hare-orm/blob/dev/"
    GENERATED_NOTICE = "<!-- Written by benchmarks/bench.py (`all` or `charts`): edit the text there, not here. -->"
    #: The page's heading and the name of each database's server in the version table.
    SERVER_NAMES: ClassVar[dict[str, str]] = {
        "postgresql": "PostgreSQL",
        "sqlite": "SQLite",
        "clickhouse": "ClickHouse",
    }

    def __init__(self, report: dict[str, Any], language: str) -> None:
        """
        Args:
            report: The report of ``Runner.run_all()``.
            language: ``en`` or ``ru``.
        """
        self.report = report
        self.language = language
        self.russian = language == "ru"
        self.charts = ChartWriter(report, language, "light")

    # --- Words --------------------------------------------------------------------------------------------

    def text(self, english: str, russian: str) -> str:
        return russian if self.russian else english

    @staticmethod
    def get_russian_plural(number: int, one: str, few: str, many: str) -> str:
        """The Russian noun form for ``number``: 1 задача, 2 задачи, 5 задач."""
        if number % 10 == 1 and number % 100 != 11:
            return one
        if 2 <= number % 10 <= 4 and not 12 <= number % 100 <= 14:
            return few
        return many

    def format_count(self, value: int) -> str:
        return self.charts.format_number(value, 0)

    @staticmethod
    def wrap(text: str, indent: str = "") -> str:
        """A paragraph or list item broken into lines of at most 100 characters."""
        return textwrap.fill(text, width=100, subsequent_indent=indent, break_long_words=False, break_on_hyphens=False)

    def join(self, items: list[str]) -> str:
        """``a, b and c`` in the page's language."""
        conjunction = " и " if self.russian else " and "
        return items[0] if len(items) == 1 else ", ".join(items[:-1]) + conjunction + items[-1]

    def heading(self, level: int, title: str, anchor: str) -> str:
        """A heading with its anchor, as GitHub and the site both read it."""
        return f'{"#" * level} <a id="{anchor}"></a>{title}'

    def get_compared_names(self, databases: list[Database]) -> str:
        """The other libraries of some databases, one name per family."""
        names: list[str] = []
        for database in databases:
            for target in self.charts.get_targets(database):
                name = target.family.replace(" (autocommit)", "")
                if target.distribution != BASELINE_DISTRIBUTION and name not in names:
                    names.append(name)
        return self.join(names)

    def get_baseline_phrase(self, database: Database) -> str:
        """hare-orm on the database's fastest driver, as the summary names it."""
        drivers = self.charts.get_baseline_drivers(database)
        baseline = drivers[0]
        if self.russian:
            phrase = f"hare-orm на {baseline.driver_russian}"
            if len(drivers) > 1:
                phrase += f" ({'более' if len(drivers) == 2 else 'самом'} быстром драйвере hare-orm в этом прогоне)"
            return phrase
        phrase = f"hare-orm on {baseline.driver_english}"
        if len(drivers) > 1:
            phrase += f" (its {'faster' if len(drivers) == 2 else 'fastest'} driver in this run)"
        return phrase

    def get_load_description(self, database: Database) -> str:
        template = database.load_russian if self.russian else database.load_english
        return template.format(
            workers=self.format_count(LOAD_CONCURRENCY), operations=self.format_count(LOAD_OPERATIONS)
        )

    def get_rows_count(self, database: Database) -> int:
        return int(self.report["databases"][database.key]["rows"])

    def get_table_description(self, database: Database) -> str:
        """What the database's scenarios run on."""
        rows = self.format_count(self.get_rows_count(database))
        if database.key == "clickhouse":
            return self.text(
                f"a table of {rows} events, sorted by their key",
                f"таблица из {rows} событий, отсортированная по ключу",
            )
        return self.text(
            f"a table of {rows} widgets, each with a gadget, a tag and a JSON document",
            f"таблица из {rows} виджетов, у каждого — гаджет, метка и документ JSON",
        )

    # --- Images ----------------------------------------------------------------------------------------------

    def get_chart_path(self, name: str, theme: str) -> str:
        return f"assets/benchmarks/{name}-{self.language}-{theme}.svg"

    def get_docs_image(self, name: str, alternative_text: str) -> str:
        """Both variants of a chart, from a page under docs/benchmarks/: the docs theme and GitHub each
        show the one for their colour scheme."""
        return (
            f"![{alternative_text}](../{self.get_chart_path(name, 'light')}#gh-light-mode-only)\n"
            f"![{alternative_text}](../{self.get_chart_path(name, 'dark')}#gh-dark-mode-only)"
        )

    def get_readme_image(self, name: str, alternative_text: str) -> str:
        width = ChartWriter.WIDTH + 2 * ChartWriter.PADDING
        return (
            '<p align="center">\n  <picture>\n'
            f'    <source media="(prefers-color-scheme: dark)" srcset="docs/{self.get_chart_path(name, "dark")}">\n'
            f'    <img src="docs/{self.get_chart_path(name, "light")}" alt="{alternative_text}" width="{width}">\n'
            "  </picture>\n</p>"
        )

    def get_scoreboard_alternative_text(self) -> str:
        return self.text("Times slower than hare-orm, by database", "Во сколько раз медленнее hare-orm, по базам")

    # --- Tables and lists ------------------------------------------------------------------------------------

    def get_environment_rows(self) -> list[tuple[str, str]]:
        report, environment = self.report, self.report["environment"]
        rows = [
            (self.text("Date", "Дата"), report["generated"]),
            (
                self.text("Machine", "Машина"),
                f"{environment['os']}, {environment['processor']}, {environment['cpu_count']} CPU",
            ),
            ("Python", environment["python"]),
        ]
        if report.get("hare_commit"):
            rows.append((self.text("hare-orm commit", "Коммит hare-orm"), f"`{report['hare_commit']}`"))
        runs = str(report["runs"])
        if report["runs"] > 1:
            runs += self.text(", median", ", медиана")
        rows.append((self.text("Runs", "Прогонов"), runs))
        return rows

    def get_database_version_rows(self, database: Database) -> list[tuple[str, str]]:
        """The server, the drivers and every library of a database's runs - with its Python where a
        target ran on another one."""
        database_report = self.report["databases"][database.key]
        rows = [(self.SERVER_NAMES[database.key], database_report["server"])]
        rows.extend(sorted(database_report["drivers"].items()))
        common_python = self.report["environment"]["python"]
        versions: dict[str, str] = {}
        for key, target_report in database_report["targets"].items():
            target = TARGET_BY_KEY[key]
            version = target_report["version"] or "—"
            if target_report.get("python", common_python) != common_python:
                version += f", Python {target_report['python']}"
            versions.setdefault(target.distribution, version)
        rows.extend(versions.items())
        rows.append(
            (self.text("Rows in the table", "Строк в таблице"), self.format_count(self.get_rows_count(database)))
        )
        return rows

    @staticmethod
    def get_table(rows: list[tuple[str, str]]) -> str:
        return "| | |\n|---|---|\n" + "\n".join(f"| {name} | {value} |" for name, value in rows)

    def get_method_items(self) -> list[str]:
        """How the numbers are taken - the same list on the docs pages and in the benchmark's README."""
        databases = [database.key for database in self.charts.get_databases()]
        pool = POOL_MAX_SIZE
        if self.russian:
            items = [
                f"На PostgreSQL у каждой ORM пул из {pool} "
                f"{self.get_russian_plural(pool, 'подключения', 'подключений', 'подключений')}; все они "
                "открываются до начала замеров.",
                "Перед нагрузочным тестом каждое подключение пула вне замера выполняет каждую операцию "
                "нагрузки: замер показывает работающее приложение, а не только что запущенное, — "
                "подключение уже открыто и подготовило свои запросы.",
                "Внутри прогона сценарий повторяется 3–5 раз, и в зачёт идёт самый быстрый повтор; строки, "
                "которые пишет сценарий записи, удаляются перед каждым повтором вне замера. Удаления и `add()` "
                "многие-ко-многим меняют данные безвозвратно и выполняются один раз.",
                "Каждая ORM работает в отдельном процессе. Прогоны идут по кругу, в каждом круге ORM запускаются в "
                "случайном порядке; результат сценария — медиана по прогонам.",
                "SQLAlchemy на PostgreSQL и SQLite измеряется дважды: с транзакцией вокруг каждой сессии (её "
                "поведение по умолчанию) и с движком в режиме autocommit, где каждая команда фиксируется сразу, "
                "как в hare и Django.",
                "Сценарий, которого в ORM нет, помечен на графике «нет в этой ORM» и не входит в её среднее.",
                "Запуск — это инициализация библиотеки и её первый запрос; библиотека к этому моменту уже "
                "импортирована. В среднее запуск не входит.",
            ]
            if "sqlite" in databases:
                items.append(
                    "На SQLite каждая ORM работает с одним файлом так, как работает по умолчанию; подключения "
                    "SQLAlchemy ждут блокировку записи файла до 30 с вместо 5 с по умолчанию — иначе нагрузка с "
                    "записью у её пула из пяти подключений обрывается ошибкой «database is locked». "
                    "`select_for_update()` на SQLite не измеряется: блокировок строк в нём нет."
                )
            if "clickhouse" in databases:
                items.append(
                    "Таблицу ClickHouse заполняет сам сервер перед сценариями; мутация (`update()`, `delete()`) "
                    "в каждой библиотеке завершается до возврата из вызова. SQLAlchemy работает через "
                    "clickhouse-sqlalchemy 0.3.2 с драйвером asynch 0.2.5 на SQLAlchemy 2.0.30 и Python 3.12 — "
                    "на более новых версиях её асинхронный драйвер не запускается."
                )
            items.append("Каждый сценарий написан так, как его пишет документация этой ORM.")
            return items
        items = [
            f"On PostgreSQL every ORM gets a pool of {pool} connections, all opened before anything is timed.",
            "Before a load test, every connection of the pool runs every operation of the load, untimed: "
            "the test measures an application that has been running, not one just started — each "
            "connection is open and has prepared its statements.",
            "Within a run, a scenario is repeated 3–5 times and its fastest repetition counts; the rows a write "
            "scenario puts back are cleared before each repetition, untimed. The deletes and the many-to-many "
            "`add()` change the data for good and run once.",
            "Each ORM runs in a process of its own. The runs go round after round, the ORMs in a shuffled order "
            "each round; a scenario's result is the median over the runs.",
            "SQLAlchemy runs twice on PostgreSQL and SQLite: with a transaction around each session (its default) "
            "and with the engine in autocommit mode, where every statement commits on its own, as in hare and "
            "Django.",
            "A scenario an ORM has no way to write is marked “not in this ORM” on its chart and left out of its "
            "average.",
            "The start is the library's init and its first query, timed with the library already imported; it "
            "is left out of the average.",
        ]
        if "sqlite" in databases:
            items.append(
                "On SQLite every ORM runs on one file the way it does by default; SQLAlchemy's connections wait up "
                "to 30 s for the file's write lock instead of the default 5 s — its pool of five otherwise stops "
                "the write load at “database is locked”. `select_for_update()` isn't measured on SQLite, which has "
                "no row locks."
            )
        if "clickhouse" in databases:
            items.append(
                "The server fills the ClickHouse table before the scenarios; a mutation (`update()`, `delete()`) "
                "finishes before its call returns in every library. SQLAlchemy runs through clickhouse-sqlalchemy "
                "0.3.2 with the asynch 0.2.5 driver on SQLAlchemy 2.0.30 and Python 3.12 — its asynchronous driver "
                "doesn't run on newer versions."
            )
        items.append("Every scenario is written the way that ORM's documentation writes it.")
        return items

    # --- The pages -------------------------------------------------------------------------------------------

    def get_summary_page(self) -> str:
        """The summary page of the docs: the table of every database, and a link to each one's page."""
        databases = self.charts.get_databases()
        titles = self.join([database.title for database in databases])
        intro = self.text(
            f"hare-orm against {self.get_compared_names(databases)} on {titles}: the same scenarios in every "
            "ORM, written the way its documentation writes them, plus a concurrent load test. Each database has "
            "a page of its own with every scenario.",
            f"hare-orm против {self.get_compared_names(databases)} на {titles}: одни и те же сценарии во всех "
            "ORM, написанные так, как их пишет документация каждой ORM, и параллельная нагрузка. У каждой базы "
            "своя страница со всеми сценариями.",
        )
        summary = self.text(
            "How many times slower than hare-orm each ORM is on each database, on average: the ratio of geometric "
            "means of the times over every scenario both run, except the cold start. hare-orm stands for its "
            "fastest driver of the database; a dash marks a database the ORM doesn't run on. The last row is "
            "hare-orm's operations per second in the load test, measured warm: every pool connection has run "
            "every operation of the load before the timing starts.",
            "Во сколько раз каждая ORM в среднем медленнее hare-orm на каждой базе: отношение средних "
            "геометрических времени по всем сценариям, которые выполняют обе, кроме запуска. hare-orm — это его "
            "самый быстрый драйвер на этой базе; прочерк — база, на которой ORM не работает. Последняя строка — "
            "операций в секунду у hare-orm под нагрузкой, измеренных «на горячую»: каждое подключение пула "
            "выполнило каждую операцию нагрузки до начала замера.",
        )
        lines = [
            self.GENERATED_NOTICE,
            "",
            f"# {self.text('Benchmarks', 'Производительность')}",
            "",
            self.wrap(intro),
            "",
            self.heading(2, self.text("Summary", "Сводка"), "summary"),
            "",
            self.wrap(summary),
            "",
            self.get_docs_image("scoreboard", self.get_scoreboard_alternative_text()),
            "",
            self.heading(2, self.text("By database", "По базам"), "by-database"),
            "",
        ]
        for database in databases:
            scenario_count = len(self.charts.get_scenarios(database))
            description = self.text(
                f"{scenario_count} scenarios on {self.get_table_description(database)}",
                f"{scenario_count} {self.get_russian_plural(scenario_count, 'сценарий', 'сценария', 'сценариев')}: "
                f"{self.get_table_description(database)}",
            )
            lines.append(self.wrap(f"- [{database.title}]({database.key}.md) — {description}.", "  "))
        lines += [
            "",
            self.heading(2, self.text("How it was measured", "Как измерялось"), "how-it-was-measured"),
            "",
            self.get_table(self.get_environment_rows()),
            "",
        ]
        lines += [self.wrap(f"- {item}", "  ") for item in self.get_method_items()]
        lines += ["", self.wrap(self.get_footer()), ""]
        return "\n".join(lines)

    def get_footer(self) -> str:
        readme = "README.ru.md" if self.russian else "README.md"
        return self.text(
            f"The benchmark is [`benchmarks/bench.py`]({self.REPOSITORY_URL}benchmarks/bench.py) in the "
            f"repository; its [README]({self.REPOSITORY_URL}benchmarks/{readme}) lists every scenario and how to "
            "run it on your own machine.",
            f"Стенд — [`benchmarks/bench.py`]({self.REPOSITORY_URL}benchmarks/bench.py) в репозитории; в его "
            f"[README]({self.REPOSITORY_URL}benchmarks/{readme}) перечислены все сценарии и описано, как "
            "повторить замер на своей машине.",
        )

    def get_database_page(self, database: Database) -> str:
        """The page of one database: its summary, its load tests and every scenario group."""
        charts = self.charts
        scenario_count = len(charts.get_scenarios(database))
        intro = self.text(
            f"hare-orm against {self.get_compared_names([database])} on {database.title}: {scenario_count} "
            f"scenarios on {self.get_table_description(database)}, and a concurrent load test. A shorter bar is "
            "faster, except in the load test, which counts operations per second.",
            f"hare-orm против {self.get_compared_names([database])} на {database.title}: {scenario_count} "
            f"{self.get_russian_plural(scenario_count, 'сценарий', 'сценария', 'сценариев')}, "
            f"{self.get_table_description(database)}, и параллельная нагрузка. Чем короче столбец, тем быстрее; "
            "исключение — нагрузка, где считаются операции в секунду.",
        )
        lines = [
            self.GENERATED_NOTICE,
            "",
            f"# {database.title}",
            "",
            self.wrap(intro),
            "",
            self.heading(2, self.text("Summary", "Сводка"), "summary"),
            "",
        ]
        if charts.get_baseline(database) is not None:
            summary = self.text(
                f"How many times slower than {self.get_baseline_phrase(database)} each library is, on average: the "
                "ratio of geometric means of the times over every scenario both run, except the cold start.",
                f"Во сколько раз каждая библиотека в среднем медленнее, чем {self.get_baseline_phrase(database)}: "
                "отношение средних геометрических времени по всем сценариям, которые выполняют обе, кроме запуска.",
            )
            alternative = self.text(
                "Times slower than hare-orm, on average", "Во сколько раз медленнее hare-orm, в среднем"
            )
            lines += [self.wrap(summary), "", self.get_docs_image(f"{database.key}-summary-slower", alternative), ""]
        load_tests = charts.get_load_tests(database)
        lines += [
            self.wrap(
                self.text("The load test: ", "Нагрузка: ")
                + self.get_load_description(database)
                + self.text(
                    ". Measured warm: every pool connection runs every operation of the load before the "
                    "timing starts, as in an application that has been running.",
                    ". Замер «на горячую»: до начала замера каждое подключение пула выполняет каждую операцию "
                    "нагрузки, как в уже работающем приложении.",
                )
            ),
            "",
        ]
        for load_test in load_tests:
            title = (
                self.text("Operations per second, the write load", "Операций в секунду, нагрузка с записью")
                if load_test == "write_load_test"
                else self.text("Operations per second", "Операций в секунду")
            )
            lines += [
                f"{title}:",
                "",
                self.get_docs_image(f"{database.key}-summary-{load_test.replace('_', '-')}", title),
                "",
            ]
        scale = self.text(
            "Each scenario is drawn on its own scale: compare the bars within one scenario, not across scenarios. "
            "The fastest time is in bold.",
            "У каждого сценария своя шкала: сравнивайте столбцы внутри одного сценария, а не между сценариями. "
            "Лучшее время выделено жирным.",
        )
        if self.report["runs"] > 1:
            scale += self.text(
                f" A bar is the median of {self.report['runs']} runs, and the thin line over it spans the best run "
                "to the worst.",
                f" Столбец — медиана {self.report['runs']} прогонов, тонкая линия поверх него — разброс от лучшего "
                "прогона к худшему.",
            )
        lines += [self.heading(2, self.text("By scenario", "По сценариям"), "by-scenario"), "", self.wrap(scale), ""]
        for group in database.groups:
            title = charts.get_label(group.english, group.russian)
            lines += [
                self.heading(3, title, group.key),
                "",
                self.get_docs_image(f"{database.key}-{group.key}", title),
                "",
            ]
        lines += [
            self.heading(
                2, self.text("Where and how it was measured", "Где и как измерялось"), "where-and-how-it-was-measured"
            ),
            "",
            self.get_table(self.get_environment_rows() + self.get_database_version_rows(database)),
            "",
        ]
        lines += [self.wrap(f"- {item}", "  ") for item in self.get_method_items()]
        lines += ["", self.wrap(self.get_footer()), ""]
        return "\n".join(lines)

    def get_readme_section(self) -> str:
        """The Benchmarks section of a README, under its heading."""
        databases = self.charts.get_databases()
        page_url = f"{self.SITE_URL}{'ru/' if self.russian else ''}benchmarks/"
        titles = self.join([database.title for database in databases])
        intro = self.text(
            f"hare-orm against {self.get_compared_names(databases)} on {titles}, on the same scenarios: how "
            "many times slower than hare-orm each ORM is on each database, on average, and hare-orm's operations "
            "per second under load, measured warm — a running application, not one just started:",
            f"hare-orm против {self.get_compared_names(databases)} на {titles}, на одних и тех же сценариях: во "
            "сколько раз каждая ORM в среднем медленнее hare-orm на каждой базе и сколько операций в секунду "
            "выполняет hare-orm под нагрузкой — «на горячую», как работающее приложение, а не только что "
            "запущенное:",
        )
        footer = self.text(
            f"Every scenario, the machine and the versions are on the [Benchmarks]({page_url}) pages; the benchmark "
            "itself is [`benchmarks/bench.py`](benchmarks/README.md).",
            f"Все сценарии, машина и версии — на страницах [Производительность]({page_url}) документации; сам "
            "стенд — [`benchmarks/bench.py`](benchmarks/README.ru.md).",
        )
        return "\n".join(
            [
                self.wrap(intro),
                "",
                self.get_readme_image("scoreboard", self.get_scoreboard_alternative_text()),
                "",
                self.wrap(footer),
            ]
        )

    def get_scenario_list(self) -> str:
        """The scenarios of every database by group, as the charts label them, and the load tests."""
        lines = [
            self.wrap(
                self.text(
                    "Every scenario has the same name and does the same work in each ORM, written the way that "
                    f"ORM's documentation writes it (`--size small`: a table of {SIZES['small']} widgets). The "
                    "scenarios, as the charts label them:",
                    "У каждого сценария одно имя и одна и та же работа во всех ORM; он написан так, как его пишет "
                    f"документация этой ORM (`--size small`: таблица из {SIZES['small']} виджетов). Сценарии — так, "
                    "как они подписаны на графиках:",
                )
            ),
            "",
        ]
        for database in self.charts.get_databases():
            lines.append(f"- **{database.title}** — {self.get_table_description(database)}")
            for group in database.groups:
                scenarios = [
                    scenario for scenario in group.scenarios if scenario.key not in database.excluded_scenarios
                ]
                lines.append(
                    f"  - *{self.charts.get_label(group.english, group.russian)}*: "
                    + "; ".join(self.charts.get_scenario_label(scenario) for scenario in scenarios)
                )
            load_title = self.text("Load", "Нагрузка")
            lines.append(self.wrap(f"  - *{load_title}*: {self.get_load_description(database)}.", "    "))
        return "\n".join(lines)

    def get_method_list(self) -> str:
        return "\n".join(self.wrap(f"- {item}", "  ") for item in self.get_method_items())

    @staticmethod
    def replace_block(path: Path, name: str, content: str) -> None:
        """Replaces the text between ``<!-- name:start -->`` and ``<!-- name:end -->`` in a file.

        Raises:
            ValueError: The file has no such markers.
        """
        start, end = f"<!-- {name}:start -->", f"<!-- {name}:end -->"
        text = path.read_text(encoding="utf-8")
        if start not in text or end not in text:
            raise ValueError(f"{path.relative_to(REPOSITORY)} has no {start} ... {end} block")
        before, rest = text.split(start, 1)
        __, after = rest.split(end, 1)
        path.write_text(f"{before}{start}\n{content}\n{end}{after}", encoding="utf-8", newline="\n")

    @classmethod
    def write_all(cls, report: dict[str, Any], docs_directory: Path = DOCS_DIRECTORY, readmes: bool = True) -> None:
        """Writes every page and block, in both languages.

        Args:
            report: The report.
            docs_directory: Where the docs pages go.
            readmes: Whether the README blocks are written too.
        """
        docs_directory.mkdir(parents=True, exist_ok=True)
        for language in ("en", "ru"):
            pages = cls(report, language)
            suffix = ".ru.md" if language == "ru" else ".md"
            (docs_directory / f"index{suffix}").write_text(pages.get_summary_page(), encoding="utf-8", newline="\n")
            for database in pages.charts.get_databases():
                (docs_directory / f"{database.key}{suffix}").write_text(
                    pages.get_database_page(database), encoding="utf-8", newline="\n"
                )
            if readmes:
                cls.replace_block(cls.READMES[language], "benchmarks", pages.get_readme_section())
                cls.replace_block(cls.BENCHMARK_READMES[language], "benchmark-scenarios", pages.get_scenario_list())
                cls.replace_block(cls.BENCHMARK_READMES[language], "benchmark-method", pages.get_method_list())
