"""uvicorn launcher: one single-worker uvicorn subprocess per worker, each
pinned via `taskset` before the process image is even loaded (PLAN.md §5/§9).

Deliberately not uvicorn's own `--workers`: that forks worker processes from
one supervisor, which means affinity would have to be set *after* the fork —
exactly the "pin before the pool opens" hazard `docs/METHODOLOGY.md` warns
about, since a forked worker's connection pool may already exist by the time
anything could reach in and pin it. `taskset -c ... uvicorn ...` pins before
`exec()` replaces the process image, so there is no such window.
"""

from __future__ import annotations

import asyncio
import socket
import time
from dataclasses import dataclass

from benchmarks.harness.affinity import read_back


@dataclass(slots=True)
class ServiceWorker:
    proc: asyncio.subprocess.Process
    port: int
    cores: list[int]

    async def stop(self) -> None:
        self.proc.terminate()
        try:
            await asyncio.wait_for(self.proc.wait(), timeout=5)
        except TimeoutError:
            self.proc.kill()
            await self.proc.wait()


def _port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


async def launch(
    app_target: str, *, base_port: int, workers: int, cores: list[int],
    env: dict[str, str] | None = None, loop: str = "uvloop", quiet: bool = False,
) -> list[ServiceWorker]:
    """Start `workers` uvicorn subprocesses on consecutive ports from
    `base_port`, each pinned to one physical core from `cores` (round-robin if
    `workers` exceeds `len(cores)` — best-effort per D13, same as `CorePlan`).

    `quiet=True` discards each worker's stdout/stderr instead of inheriting
    the caller's — for an automated run (`bench load`) that prints its own
    per-second CPU lines, uvicorn's own startup/shutdown INFO lines are just
    noise interleaved with them. Discarded rather than piped and captured:
    piping without a reader draining it risks the pipe buffer filling and
    blocking the child on a long run, and the pre-spawn port check above
    already catches the most common early-failure mode (address in use)."""
    import os

    workers_list: list[ServiceWorker] = []
    for i in range(workers):
        port = base_port + i
        # Checked *before* spawning, not just polled for readiness afterward:
        # an already-occupied port still accepts TCP connections (just from
        # whatever else is listening), so a bare "can I connect" readiness
        # check cannot tell "our worker is up" from "something unrelated was
        # already there" — it would silently pass and every request would go
        # to the wrong process. Observed on a shared dev box where port 8000
        # was already bound by an unrelated service.
        if _port_in_use(port):
            raise RuntimeError(
                f"port {port} is already in use by another process — pass a "
                f"different --port (or --base-port). A uvicorn worker failing to "
                f"bind here would be indistinguishable from a slow-starting one "
                f"under a plain 'can I connect' readiness check, so this fails "
                f"before spawning rather than after."
            )
        worker_cores = [cores[i % len(cores)]] if cores else []
        cmd = []
        if worker_cores:
            cmd += ["taskset", "-c", ",".join(str(c) for c in worker_cores)]
        cmd += [
            "uvicorn", app_target, "--port", str(port), "--loop", loop,
            "--http", "httptools", "--no-access-log",
        ]
        worker_env = {**os.environ, **(env or {})}
        stdout = asyncio.subprocess.DEVNULL if quiet else None
        stderr = asyncio.subprocess.DEVNULL if quiet else None
        proc = await asyncio.create_subprocess_exec(*cmd, env=worker_env, stdout=stdout, stderr=stderr)
        workers_list.append(ServiceWorker(proc=proc, port=port, cores=worker_cores))

    await _wait_ready(workers_list)
    return workers_list


async def _wait_ready(workers: list[ServiceWorker], timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    for worker in workers:
        exited = asyncio.ensure_future(worker.proc.wait())
        try:
            while True:
                if exited.done():
                    raise RuntimeError(
                        f"uvicorn worker for port {worker.port} exited early "
                        f"(code {exited.result()}) before becoming ready — see its "
                        f"own output above for the actual error"
                    )
                try:
                    with socket.create_connection(("127.0.0.1", worker.port), timeout=1):
                        break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            f"uvicorn on port {worker.port} did not become ready "
                            f"within {timeout}s"
                        ) from None
                    await asyncio.sleep(0.2)
        finally:
            if not exited.done():
                exited.cancel()


def read_back_affinity(workers: list[ServiceWorker]) -> dict[int, list[int]]:
    """Actual kernel-reported masks per worker pid (PLAN.md §4: "records
    actual masks" — never trust the requested cpuset as evidence it took
    effect). `taskset` execs into the same pid, so `proc.pid` is still correct
    to read back from."""
    return {worker.proc.pid: read_back(worker.proc.pid) for worker in workers}
