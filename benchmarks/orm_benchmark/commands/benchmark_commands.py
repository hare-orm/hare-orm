from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys

from orm_benchmark.constants import REPOSITORY, RESULTS_FILE, SIZES
from orm_benchmark.declarations.databases import DATABASES
from orm_benchmark.declarations.targets import TARGET_BY_KEY, TARGETS
from orm_benchmark.running.runner import Runner


class BenchmarkCommands:
    """The command line of ``benchmarks/bench.py``."""

    @staticmethod
    def get_parser() -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(description="hare-orm's comparative ORM benchmark.")
        commands = parser.add_subparsers(dest="command", required=True)
        run_command = commands.add_parser("run", help="One run of one target, printed.")
        run_command.add_argument("target", choices=[target.key for target in TARGETS])
        run_command.add_argument("--size", choices=list(SIZES), default="large")
        run_command.add_argument("--out", help="Writes the run's results to this JSON file.")
        all_command = commands.add_parser("all", help="Every target several times, then results.json and the pages.")
        all_command.add_argument("--runs", type=int, default=1)
        all_command.add_argument("--size", choices=list(SIZES), default="large")
        all_command.add_argument("--databases", nargs="+", choices=[database.key for database in DATABASES])
        all_command.add_argument("--targets", nargs="+", choices=[target.key for target in TARGETS])
        commands.add_parser("charts", help="Draws the charts and writes the pages again, from results.json.")
        return parser

    @classmethod
    def main(cls) -> None:
        arguments = cls.get_parser().parse_args()
        if arguments.command == "run":
            target = TARGET_BY_KEY[arguments.target]
            if not Runner.runs_here(target):
                # The target's library is installed in an environment of its own - its Python runs it.
                raise SystemExit(subprocess.run([Runner.get_interpreter(target), *sys.argv], check=False).returncode)
            result = asyncio.run(Runner.run_once(target, arguments.size))
            if arguments.out:
                with open(arguments.out, "w", encoding="utf-8") as output:
                    json.dump(result, output, indent=2)
            for name, value in result["results"].items():
                print(f"  {name:36s} {'-' if value is None else f'{value:12.2f}'}")
            return
        if arguments.command == "all":
            targets = [TARGET_BY_KEY[key] for key in arguments.targets] if arguments.targets else list(TARGETS)
            if arguments.databases:
                targets = [target for target in targets if target.database in arguments.databases]
            report = Runner.run_all(targets, arguments.runs, arguments.size)
            RESULTS_FILE.parent.mkdir(parents=True, exist_ok=True)
            RESULTS_FILE.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            print(f"wrote {RESULTS_FILE.relative_to(REPOSITORY)}")
        else:
            report = json.loads(RESULTS_FILE.read_text(encoding="utf-8"))
        # Local imports: a target's own environment runs only `run`, without what the pages need.
        from orm_benchmark.output.benchmark_pages import BenchmarkPages
        from orm_benchmark.output.chart_writer import ChartWriter

        ChartWriter.write_all(report)
        BenchmarkPages.write_all(report)
