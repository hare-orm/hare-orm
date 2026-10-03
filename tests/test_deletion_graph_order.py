"""The deletion graph walks relations in name order - the cascade deletes, and reports its change
events, in the same order in every process, whatever the hash seed of the run."""

import os
import subprocess
import sys

from hare.models.deletion.deletion_graph import DeletionGraph
from tests.testmodels import Author

ORDER_SCRIPT = (
    "import asyncio\n"
    "from hare.contrib.test.helpers import hare_test_context\n"
    "from hare.models.deletion.deletion_graph import DeletionGraph\n"
    "from tests.cascade_batch_models import BatchSoftParent\n"
    "async def main():\n"
    "    async with hare_test_context(['tests.cascade_batch_models'], db_url='sqlite://:memory:'):\n"
    "        print([model.__name__ for model in DeletionGraph.get_cascade_models(BatchSoftParent)])\n"
    "asyncio.run(main())\n"
)


def get_cascade_orders(hash_seeds: list[str]) -> set[str]:
    # The processes run at once.
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", ORDER_SCRIPT],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, "PYTHONHASHSEED": hash_seed},
            stdin=subprocess.DEVNULL,
        )
        for hash_seed in hash_seeds
    ]
    orders = set()
    for process in processes:
        stdout, stderr = process.communicate(timeout=120)
        assert process.returncode == 0, stderr
        orders.add(stdout.strip())
    return orders


def test_backward_relations_are_listed_by_name(db):
    names = [backward_field.model_field_name for backward_field, _ in DeletionGraph.get_backward_relations(Author)]
    assert names == sorted(names)
    assert len(names) > 1


def test_the_cascade_order_is_the_same_in_every_process():
    assert len(get_cascade_orders(["1", "2", "3", "4"])) == 1
