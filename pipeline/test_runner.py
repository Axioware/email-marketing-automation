"""Test runner that never touches the real database.

Tests run against TEST_DATABASE_URL when it is set, otherwise against a throwaway Postgres in Docker that is removed
afterwards. The DATABASE_URL in .env (Supabase) is ignored while testing.
"""
import os
import socket
import subprocess
import time
import uuid

from django.db import connections
from django.test.runner import DiscoverRunner

from emailautomation.settings import database_from_url

POSTGRES_IMAGE = "postgres:16-alpine"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class PostgresDockerRunner(DiscoverRunner):
    container = None

    def setup_databases(self, **kwargs):
        url = os.environ.get("TEST_DATABASE_URL", "").strip()
        if not url:
            url = self.start_postgres()
        settings = database_from_url(url)
        connections["default"].close()
        connections["default"].settings_dict.update(
            {key: settings[key] for key in ("NAME", "USER", "PASSWORD", "HOST", "PORT", "OPTIONS")}
        )
        return super().setup_databases(**kwargs)

    def teardown_databases(self, old_config, **kwargs):
        try:
            super().teardown_databases(old_config, **kwargs)
        finally:
            if self.container:
                subprocess.run(["docker", "rm", "-f", self.container], capture_output=True)

    def start_postgres(self) -> str:
        self.container = f"emailtest-pg-{uuid.uuid4().hex[:8]}"
        port = free_port()
        try:
            subprocess.run(
                ["docker", "run", "-d", "--rm", "--name", self.container, "-e", "POSTGRES_PASSWORD=test",
                 "-p", f"127.0.0.1:{port}:5432", POSTGRES_IMAGE],
                check=True, capture_output=True, timeout=300,
            )
        except (OSError, subprocess.SubprocessError) as error:
            self.container = None
            raise RuntimeError("Tests need Docker (for a throwaway Postgres) or TEST_DATABASE_URL.") from error
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            ready = subprocess.run(["docker", "exec", self.container, "pg_isready", "-U", "postgres", "-h", "127.0.0.1"],
                                   capture_output=True)
            if ready.returncode == 0:
                break
            time.sleep(1)
        else:
            raise RuntimeError("The test Postgres did not start.")
        time.sleep(1)
        return f"postgresql://postgres:test@127.0.0.1:{port}/postgres"
