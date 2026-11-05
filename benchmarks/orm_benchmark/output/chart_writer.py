from __future__ import annotations

from html import escape
from pathlib import Path
from typing import Any, ClassVar

from orm_benchmark.constants import BENCHMARK_STATUS_LABELS, CHART_DIRECTORY
from orm_benchmark.definitions.benchmark_report import BenchmarkReport
from orm_benchmark.definitions.database import Database
from orm_benchmark.definitions.scenario import Scenario
from orm_benchmark.definitions.target import Target


class ChartWriter:
    """Draws the charts the documentation and the README show, as SVG files - one per language and
    colour scheme: the summary table of every database (how much slower than hare-orm each ORM is on
    average), and per database how much slower each ORM is, the load tests' throughput and one chart
    per scenario group, a group of bars per scenario."""

    FONT = "system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif"
    MONOSPACE = "ui-monospace, SFMono-Regular, Consolas, 'Liberation Mono', monospace"
    #: ``surface`` is the chart's own background - the docs' page colour - so its text stays readable on
    #: any page, whichever of the two variants that page ends up showing.
    THEMES: ClassVar[dict[str, dict[str, str]]] = {
        "light": {
            "ink": "#1e1b15",
            "secondary": "#57503f",
            "muted": "#6e6654",
            "grid": "#e4e0d4",
            "surface": "#faf9f4",
        },
        "dark": {
            "ink": "#ede7d8",
            "secondary": "#c4bba5",
            "muted": "#a89e86",
            "grid": "#2c2922",
            "surface": "#15130f",
        },
    }
    WIDTH = 760
    PADDING = 20
    #: The ratio a summary table's bar is full at - a slower ORM's bar stops there, its number doesn't.
    SCOREBOARD_RATIO_CEILING = 4.0

    def __init__(self, report: dict[str, Any], language: str, theme: str) -> None:
        """
        Args:
            report: The report of ``Runner.run_all()``.
            language: ``en`` or ``ru``.
            theme: ``light`` or ``dark``.
        """
        self.data = BenchmarkReport(report)
        self.language = language
        self.russian = language == "ru"
        self.theme = theme
        self.colours = self.THEMES[theme]
        self.counts = self.data.conditions["counts"]

    # --- The report's figures -------------------------------------------------------------------------

    def get_target_name(self, target: Target) -> str:
        """A target's name in the chart's language."""
        name = target.name
        if self.russian:
            for english, russian in self.data.conditions["russian_name_parts"].items():
                name = name.replace(english, russian)
        return name

    def get_label(self, english: str, russian: str) -> str:
        """A label in the chart's language, its counts filled in."""
        return (russian if self.russian else english).format(**self.counts)

    def get_scenario_label(self, scenario: Scenario) -> str:
        return self.get_label(scenario.english, scenario.russian)

    # --- Drawing ------------------------------------------------------------------------------------------

    def format_number(self, value: float, decimals: int) -> str:
        """A number with the language's decimal and thousands separators."""
        text = f"{value:,.{decimals}f}"
        if self.russian:
            return text.replace(",", " ").replace(".", ",")
        return text

    def format_milliseconds(self, value: float) -> str:
        unit = "мс" if self.russian else "ms"
        decimals = 0 if value >= 100 else 1 if value >= 10 else 2
        return f"{self.format_number(value, decimals)} {unit}"

    def format_ratio(self, ratio: float, is_baseline: bool) -> str:
        # A driver this close to the baseline still shows that it isn't the baseline: ×1.002, not ×1.00.
        decimals = 3 if not is_baseline and round(ratio, 2) == 1 else 2
        return "×" + self.format_number(ratio, decimals)

    def text(self, x: float, y: float, content: str, **attributes: Any) -> str:
        """An SVG text element in the chart's font."""
        size = attributes.pop("size", 13)
        colour = attributes.pop("colour", self.colours["ink"])
        family = attributes.pop("family", self.FONT)
        extra = "".join(f' {name.replace("_", "-")}="{value}"' for name, value in attributes.items())
        return (
            f'<text x="{x:.1f}" y="{y:.1f}" font-family="{family}" font-size="{size}" fill="{colour}"{extra}>'
            f"{escape(content, quote=True)}</text>"
        )

    @staticmethod
    def bar(x: float, y: float, length: float, thickness: float, colour: str) -> str:
        """A horizontal bar, square at the baseline, its end rounded."""
        radius = min(4.0, length / 2, thickness / 2)
        return (
            f'<path d="M{x:.1f},{y:.1f} H{x + length - radius:.1f} Q{x + length:.1f},{y:.1f} {x + length:.1f},'
            f"{y + radius:.1f} V{y + thickness - radius:.1f} Q{x + length:.1f},{y + thickness:.1f} "
            f'{x + length - radius:.1f},{y + thickness:.1f} H{x:.1f} Z" fill="{colour}"/>'
        )

    def rule(self, y: float) -> str:
        return f'<rect x="0" y="{y:.1f}" width="{self.WIDTH}" height="1" fill="{self.colours["grid"]}"/>'

    def get_series_colour(self, target: Target) -> str:
        return target.light_colour if self.theme == "light" else target.dark_colour

    def legend(self, targets: list[Target], y: float) -> tuple[list[str], float]:
        """The legend's items in rows across the chart's width.

        Args:
            targets: The targets.
            y: The top of the legend.

        Returns:
            The SVG elements and the legend's height.
        """
        elements = []
        x, row = 0.0, 0
        for target in targets:
            name = self.get_target_name(target)
            item_width = 22 + len(name) * 7.2 + 18
            if x + item_width > self.WIDTH and x > 0:
                x, row = 0.0, row + 1
            top = y + row * 22
            colour = self.get_series_colour(target)
            elements.append(f'<rect x="{x:.1f}" y="{top + 3:.1f}" width="12" height="12" rx="3" fill="{colour}"/>')
            elements.append(self.text(x + 18, top + 13.5, name, size=13))
            x += item_width
        return elements, (row + 1) * 22

    def document(self, height: float, elements: list[str], title: str) -> str:
        """The SVG file: the chart on its own rounded background, ``PADDING`` in from its edges."""
        width, full_height = self.WIDTH + 2 * self.PADDING, height + 2 * self.PADDING
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {full_height:.0f}" '
            f'width="{width}" height="{full_height:.0f}" role="img">'
            f"<title>{escape(title, quote=True)}</title>"
            f'<rect width="{width}" height="{full_height:.0f}" rx="10" fill="{self.colours["surface"]}"/>'
            f'<g transform="translate({self.PADDING},{self.PADDING})">{"".join(elements)}</g></svg>\n'
        )

    # --- The charts -----------------------------------------------------------------------------------------

    def scoreboard(self) -> str:
        """The summary table: a row per ORM and a column per database, each cell how many times slower
        than hare-orm the ORM is there on average - a bar and its number, a dash where the ORM doesn't
        run on the database - and a last row of hare-orm's operations per second under load."""
        databases = self.data.get_databases()
        label_width, row_height, top = 220.0, 32.0, 30.0
        column_width = (self.WIDTH - label_width) / len(databases)
        bar_room = column_width - 74
        targets_by_cell = {
            (target.family, database.key): target
            for database in databases
            for target in self.data.get_targets(database)
            # One target per ORM and database: hare-orm's fastest driver stands for it.
            if target.family != "hare-orm" or target == self.data.get_baseline(database)
        }
        families = [
            family for family in self.data.conditions["families"] if any(cell[0] == family for cell in targets_by_cell)
        ]
        elements = []
        for column, database in enumerate(databases):
            x = label_width + column * column_width
            elements.append(self.text(x, 16, database.title, size=13.5, font_weight="600"))
        for row, family in enumerate(families):
            y = top + row * row_height
            elements.append(self.rule(y))
            is_hare = family == "hare-orm"
            elements.append(self.text(0, y + 21, family, size=13.5, font_weight="600" if is_hare else "400"))
            for column, database in enumerate(databases):
                x = label_width + column * column_width
                target = targets_by_cell.get((family, database.key))
                if target is None:
                    elements.append(self.text(x, y + 21, "—", size=13, colour=self.colours["muted"]))
                    continue
                baseline = self.data.get_baseline(database)
                ratio = self.data.get_ratio(target, baseline)
                if ratio is None:
                    elements.append(self.text(x, y + 21, "—", size=13, colour=self.colours["muted"]))
                    continue
                length = max(3.0, min(ratio, self.SCOREBOARD_RATIO_CEILING) / self.SCOREBOARD_RATIO_CEILING * bar_room)
                elements.append(self.bar(x, y + 9, length, 14, self.get_series_colour(target)))
                elements.append(
                    self.text(
                        x + length + 6,
                        y + 20.5,
                        self.format_ratio(ratio, target == baseline),
                        size=12.5,
                        family=self.MONOSPACE,
                        colour=self.colours["ink"] if is_hare else self.colours["secondary"],
                        font_weight="700" if is_hare else "400",
                    )
                )
        y = top + len(families) * row_height
        elements.append(self.rule(y))
        load_label = "hare-orm под нагрузкой, оп/с" if self.russian else "hare-orm under load, ops/s"
        elements.append(self.text(0, y + 21, load_label, size=13, colour=self.colours["secondary"]))
        for column, database in enumerate(databases):
            baseline = self.data.get_baseline(database)
            if baseline is None:
                continue
            throughput = self.data.get_median(baseline, "load_test_throughput_ops_per_sec")
            if throughput is not None:
                x = label_width + column * column_width
                elements.append(
                    self.text(x, y + 21, self.format_number(throughput, 0), size=12.5, family=self.MONOSPACE)
                )
        title = "Во сколько раз медленнее hare-orm" if self.russian else "Times slower than hare-orm"
        return self.document(y + row_height, elements, title)

    def ranked_bars(self, title: str, rows: list[tuple[Target, float, str]]) -> str:
        """Bars in rank order, the name on the left and the value at the end - a summary chart.

        Args:
            title: The chart's title, for screen readers.
            rows: The target, its value and the value's text, best first.

        Returns:
            The SVG.
        """
        label_width, row_height = 220.0, 30.0
        maximum = max((value for __, value, __ in rows), default=1.0)
        plot_width = self.WIDTH - label_width - 90
        elements = []
        for index, (target, value, value_text) in enumerate(rows):
            y = index * row_height
            elements.append(
                self.text(label_width - 12, y + 19, self.get_target_name(target), size=13.5, text_anchor="end")
            )
            length = max(2.0, value / maximum * plot_width)
            elements.append(self.bar(label_width, y + 7, length, 16, self.get_series_colour(target)))
            elements.append(
                self.text(
                    label_width + length + 8,
                    y + 19.5,
                    value_text,
                    size=12.5,
                    colour=self.colours["secondary"],
                    family=self.MONOSPACE,
                )
            )
        return self.document(len(rows) * row_height + 6, elements, title)

    def summary_ratio(self, database: Database, baseline: Target) -> str:
        """How many times slower than ``baseline`` every target of a database is, hare-orm's other
        drivers included."""
        rows = []
        for target in self.data.get_targets(database):
            ratio = self.data.get_ratio(target, baseline)
            if ratio is None:
                continue
            rows.append((target, ratio, self.format_ratio(ratio, target == baseline)))
        rows.sort(key=lambda row: row[1])
        name = self.get_target_name(baseline)
        return self.ranked_bars(
            f"Во сколько раз медленнее, чем {name}" if self.russian else f"Times slower than {name}", rows
        )

    def summary_load(self, database: Database, load_test: str) -> str:
        """A load test's operations per second on a database."""
        key = f"{load_test}_throughput_ops_per_sec"
        rows = [
            (target, value, self.format_number(value, 0))
            for target in self.data.get_targets(database)
            if (value := self.data.get_median(target, key)) is not None
        ]
        rows.sort(key=lambda row: -row[1])
        return self.ranked_bars("Операций в секунду" if self.russian else "Operations per second", rows)

    def get_status_label(self, status: str) -> str:
        return BENCHMARK_STATUS_LABELS[status][int(self.russian)]

    def group(self, database: Database, group_key: str) -> str:
        """One scenario group of a database: a legend, then a group of bars per scenario, two columns
        wide, each scenario on its own scale with the time at the end of each bar - a note instead of a
        bar for an ORM with no way to write the scenario."""
        group = next(group for group in database.groups if group.key == group_key)
        scenarios = [scenario for scenario in group.scenarios if scenario.key not in database.excluded_scenarios]
        targets = self.data.get_targets(database)
        legend_elements, legend_height = self.legend(targets, 0)
        elements = list(legend_elements)
        column_gap = 36.0
        column_width = (self.WIDTH - column_gap) / 2
        bar_thickness, bar_gap = 10.0, 4.0
        block_height = 26 + len(targets) * (bar_thickness + bar_gap) + 18
        top = legend_height + 14
        for index, scenario in enumerate(scenarios):
            column, row = index % 2, index // 2
            x = column * (column_width + column_gap)
            y = top + row * block_height
            elements.append(self.text(x, y + 14, self.get_scenario_label(scenario), size=13.5, font_weight="600"))
            results = [self.data.get_result(target, scenario.key) for target in targets]
            measured = [result for result in results if result["status"] == "measured"]
            maximum = max((max(result["runs"]) for result in measured), default=1.0) or 1.0
            fastest = min((result["median"] for result in measured), default=None)
            plot_width = column_width - 88
            for target_index, (target, result) in enumerate(zip(targets, results, strict=True)):
                bar_y = y + 24 + target_index * (bar_thickness + bar_gap)
                if result["status"] != "measured":
                    missing_text = self.get_status_label(result["status"])
                    elements.append(
                        self.text(
                            x, bar_y + 9, missing_text, size=11, colour=self.colours["muted"], font_style="italic"
                        )
                    )
                    continue
                value = result["median"]
                length = max(2.0, value / maximum * plot_width)
                elements.append(self.bar(x, bar_y, length, bar_thickness, self.get_series_colour(target)))
                # The spread of the runs: a thin line from the best run to the worst.
                lowest = min(result["runs"]) / maximum * plot_width
                highest = max(result["runs"]) / maximum * plot_width
                if highest - lowest >= 2:
                    middle = bar_y + bar_thickness / 2
                    elements.append(
                        f'<line x1="{x + lowest:.1f}" y1="{middle:.1f}" x2="{x + highest:.1f}" y2="{middle:.1f}" '
                        f'stroke="{self.colours["ink"]}" stroke-opacity="0.55" stroke-width="1.5"/>'
                    )
                is_fastest = value == fastest
                elements.append(
                    self.text(
                        x + max(length, highest) + 6,
                        bar_y + 9,
                        self.format_milliseconds(value),
                        size=11,
                        family=self.MONOSPACE,
                        colour=self.colours["ink"] if is_fastest else self.colours["muted"],
                        font_weight="700" if is_fastest else "400",
                    )
                )
        rows = (len(scenarios) + 1) // 2
        return self.document(top + rows * block_height, elements, self.get_label(group.english, group.russian))

    def get_charts(self) -> dict[str, str]:
        """Every chart of the report, by file name without the language and theme."""
        charts = {"scoreboard": self.scoreboard()}
        for database in self.data.get_databases():
            baseline = self.data.get_baseline(database)
            if baseline is not None:
                charts[f"{database.key}-summary-slower"] = self.summary_ratio(database, baseline)
            for load_test in self.data.get_load_tests(database):
                charts[f"{database.key}-summary-{load_test.replace('_', '-')}"] = self.summary_load(
                    database, load_test
                )
            for group in database.groups:
                charts[f"{database.key}-{group.key}"] = self.group(database, group.key)
        return charts

    @classmethod
    def render_all(cls, report: dict[str, Any], directory: Path = CHART_DIRECTORY) -> dict[Path, str]:
        """Generate the complete set before any published artifact is replaced."""
        return {
            directory / f"{name}-{language}-{theme}.svg": svg
            for language in ("en", "ru")
            for theme in ("light", "dark")
            for name, svg in cls(report, language, theme).get_charts().items()
        }
