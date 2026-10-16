"""Close real synthetic statistics connections on success and rollback."""
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from test_statistics import STATISTICS


class StatisticsStorageLifetimeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "statistics.sqlite"
        self.candidate = {"statistic_id": "halthy:synthetic_heart_rate", "unit": "bpm",
            "start": datetime(2026, 10, 9, 10, 15, tzinfo=timezone.utc), "value": 70.0}

    def test_success_closes_connection_and_commits_samples_across_repeated_calls(self):
        connections = []
        connect = sqlite3.connect

        def tracking_connect(*args, **kwargs):
            connection = connect(*args, **kwargs)
            connections.append(connection)
            self.addCleanup(connection.close)
            return connection

        with patch.object(STATISTICS.sqlite3, "connect", side_effect=tracking_connect):
            for _ in range(3):
                result = STATISTICS._stored_hourly_samples(self.path, [self.candidate])
                self.assertEqual(next(iter(result[self.candidate["statistic_id"]].values()))["mean"], 70.0)
        self.assertEqual(len(connections), 3)
        for connection in connections:
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute("SELECT 1")
        with closing(connect(self.path)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM samples").fetchone(), (1,))

    def test_aggregation_failure_rolls_back_insert_and_closes_connection(self):
        connections = []
        connect = sqlite3.connect

        class FailingConnection(sqlite3.Connection):
            def execute(self, sql, *args, **kwargs):
                if sql.startswith("SELECT AVG"):
                    raise sqlite3.OperationalError("synthetic aggregation failure")
                return super().execute(sql, *args, **kwargs)

        def failing_connect(*args, **kwargs):
            connection = connect(*args, **kwargs, factory=FailingConnection)
            connections.append(connection)
            self.addCleanup(connection.close)
            return connection

        with patch.object(STATISTICS.sqlite3, "connect", side_effect=failing_connect):
            with self.assertRaises(sqlite3.OperationalError):
                STATISTICS._stored_hourly_samples(self.path, [self.candidate])
        self.assertEqual(len(connections), 1)
        with self.assertRaises(sqlite3.ProgrammingError):
            connections[0].execute("SELECT 1")
        with closing(connect(self.path)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM samples").fetchone(), (0,))
