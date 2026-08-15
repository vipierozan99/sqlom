"""Ephemeral Postgres via docker: `--network host` avoids the
unpinned docker-proxy userspace hop that would confound loopback latency
measurements; `--cpuset-cpus` covers every backend from birth, structurally
removing the "pin before the pool opens" hazard `docs/METHODOLOGY.md` warns
about (affinity is inherited across `fork()`, so pinning after backends have
already forked misses them).

`attach()` is the escape hatch for a server this suite didn't start.
"""

from __future__ import annotations

import asyncio
import socket
import subprocess
import time
import uuid
from dataclasses import dataclass

from sqlalchemy.dialects.postgresql import asyncpg

from benchmarks.harness import seed as seed_module

_DIALECT = asyncpg.dialect()

DEFAULT_DB = "rowform_bench"
DEFAULT_USER = "postgres"
DEFAULT_PASSWORD = "postgres"  # noqa: S105 -- ephemeral container's own throwaway credential, not a secret


def _port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


@dataclass(slots=True)
class EphemeralPostgres:
    container_id: str
    dsn: str
    port: int
    cpuset: str | None
    image: str

    @classmethod
    async def start(
        cls, *, port: int = 5432, cpuset: str | None = None, version: str = "16",
        ssl: bool = False, db: str = DEFAULT_DB,
    ) -> EphemeralPostgres:
        # Host networking means this container binds the port directly —
        # no docker-proxy fallback to silently pick a different one, so a
        # collision must fail loudly rather than start a container nothing
        # can reach.
        if _port_in_use(port):
            raise RuntimeError(
                f"port {port} is already in use — pass --port to bench db up, or "
                f"stop whatever else is listening there"
            )
        image = f"postgres:{version}"
        name = f"rowform-bench-{uuid.uuid4().hex[:8]}"
        cmd = [
            "docker", "run", "-d", "--name", name, "--network", "host",
            "-e", f"POSTGRES_PASSWORD={DEFAULT_PASSWORD}",
            "-e", f"POSTGRES_DB={db}",
            "-e", f"PGPORT={port}",
        ]
        if cpuset:
            cmd += ["--cpuset-cpus", cpuset]
        cmd.append(image)

        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"docker run failed: {stderr.decode().strip()}")
        container_id = stdout.decode().strip()

        dsn = f"postgresql://{DEFAULT_USER}:{DEFAULT_PASSWORD}@127.0.0.1:{port}/{db}"
        if not ssl:
            dsn += "?sslmode=disable"
        instance = cls(container_id=container_id, dsn=dsn, port=port, cpuset=cpuset, image=image)
        try:
            await instance._wait_ready()
        except BaseException:
            # A container that never became ready (timeout, Ctrl-C during
            # polling) would otherwise outlive the failed start and hold the
            # port against every later provision.
            await instance.stop()
            raise
        return instance

    async def _wait_ready(self, timeout: float = 30.0) -> None:
        import asyncpg

        deadline = time.monotonic() + timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                conn = await asyncpg.connect(self.dsn)
                await conn.close()
                return
            except Exception as exc:  # readiness polling: any failure just means "not yet"
                last_error = exc
                await asyncio.sleep(0.5)
        raise TimeoutError(
            f"postgres container did not become ready within {timeout}s: {last_error}"
        )

    async def stop(self) -> None:
        if not self.container_id:
            return  # attached to an external server — nothing to tear down
        proc = await asyncio.create_subprocess_exec(
            "docker", "rm", "-f", self.container_id,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()

    def cpuset_from_outside(self) -> str | None:
        """Read the container's actual cpuset back from docker itself — the
        "verified from outside" half of the phase-1 gate; the requested
        cpuset is not evidence that pinning took effect."""
        if not self.container_id:
            return None
        result = subprocess.run(
            ["docker", "inspect", "-f", "{{.HostConfig.CpusetCpus}}", self.container_id],
            capture_output=True, text=True, check=False,
        )
        return result.stdout.strip() or None

    async def seed(self, shape: str, rows: int) -> int:
        """Drop, recreate, and seed `shape`'s tables. Returns the row count
        inserted (across both tables for "join")."""
        import asyncpg

        conn = await asyncpg.connect(self.dsn)
        try:
            for statement in seed_module.drop_statements(shape, "postgres"):
                await conn.execute(statement)
            for statement in seed_module.ddl_for(shape, "postgres"):
                await conn.execute(statement)
            total = 0
            for table, data in seed_module.rows_for(shape, rows):
                # COPY rather than executemany for the row counts this suite
                # seeds, but through the dialect's bind processors first — an
                # enum column needs its label, not the member, and nothing else
                # would notice until the benchmark read it back.
                records = seed_module.bound_rows(table, data, _DIALECT)
                await conn.copy_records_to_table(table.name, records=records)
                total += len(records)
            for table in seed_module.table_names(shape):
                await conn.execute(f"ANALYZE {table}")
            return total
        finally:
            await conn.close()


def attach(dsn: str) -> EphemeralPostgres:
    """Use an already-running Postgres instead of provisioning one.
    `container_id=""` marks it as attached — `stop()`/`cpuset_from_outside()`
    become no-ops rather than tearing down or introspecting a server this
    suite doesn't own."""
    return EphemeralPostgres(container_id="", dsn=dsn, port=0, cpuset=None, image="(attached)")
