from __future__ import annotations


class MatrixPorts:
    """The host port of each matrix server, made of its version's digits - the Makefile's ``*_matrix_up``
    targets publish the servers on the same ports:

    - PostgreSQL ``V``: ``54V`` (``5414``);
    - PgBouncer ``1.B`` in front of PostgreSQL ``V``: ``1BV`` (``12118``);
    - ClickHouse ``Y.M``: HTTP ``3YYMM``, native protocol ``4YYMM``, its Keeper ``6YYMM`` (``32508``,
      ``42508``, ``62508``); the first node of its test cluster: HTTP ``5YYMM``, native protocol ``2YYMM``;
    - Redis ``X.Y``: ``16XYY``, RabbitMQ ``X.Y``: ``15XYY``, Kafka ``X.Y``: ``19XYY`` (``16810``).
    """

    @staticmethod
    def split_version(version: str) -> tuple[int, int]:
        major, minor = version.split(".")
        return int(major), int(minor)

    @staticmethod
    def get_postgres_port(postgres_version: str) -> int:
        return int(f"54{postgres_version}")

    @classmethod
    def get_pgbouncer_port(cls, pgbouncer_version: str, postgres_version: str) -> int:
        _, minor = cls.split_version(pgbouncer_version)
        return int(f"1{minor:02d}{postgres_version}")

    @classmethod
    def get_clickhouse_port(cls, clickhouse_version: str, driver: str, cluster: bool) -> int:
        """The port of a ClickHouse server - the interface its driver talks to.

        Args:
            clickhouse_version: The server's version.
            driver: ``clickhouse-connect`` (HTTP) or ``clickhouse-driver`` (the native protocol).
            cluster: Whether the server is the first node of the test cluster.

        Returns:
            The port.
        """
        major, minor = cls.split_version(clickhouse_version)
        prefixes = {("clickhouse-connect", False): 3, ("clickhouse-driver", False): 4}
        prefixes |= {("clickhouse-connect", True): 5, ("clickhouse-driver", True): 2}
        return int(f"{prefixes[driver, cluster]}{major:02d}{minor:02d}")

    @classmethod
    def get_clickhouse_keeper_port(cls, clickhouse_version: str) -> int:
        major, minor = cls.split_version(clickhouse_version)
        return int(f"6{major:02d}{minor:02d}")

    @classmethod
    def get_broker_port(cls, prefix: int, broker_version: str) -> int:
        """The port of a broker: ``prefix`` (16 Redis, 15 RabbitMQ, 19 Kafka), then its version."""
        major, minor = cls.split_version(broker_version)
        return int(f"{prefix}{major}{minor:02d}")
