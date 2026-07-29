"""Minimal HTTP/1.1 keep-alive load generator, ported from the old
`benchmarks/httpload.py`. Every validation gate is kept: 200-only, chunked
responses rejected, `Content-Length` required, per-request `wait_for`,
payload-size invariance across the run, zero-response failure.

No `wrk`/`hey`/`ab` dependency, and an httpx/aiohttp client is heavy enough to
become the bottleneck when the server is pinned to one core — this sends
pre-encoded request bytes over raw asyncio streams and counts responses by
parsing only `Content-Length`.
"""

from __future__ import annotations

import asyncio
import statistics
import time
from dataclasses import dataclass, field

from benchmarks.harness.stats import percentile


class LoadError(Exception):
    """A response the run must not be allowed to count as throughput."""


@dataclass(slots=True)
class HttpLoadResult:
    path: str
    connections: int
    completed: int
    elapsed_s: float
    rps: float
    mean_ms: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    response_bytes: int
    latencies_s: list[float] = field(default_factory=list, repr=False)


async def _one_request(reader, writer, request: bytes) -> int:
    """Send one request, validate the status line, return the body length.

    Validating the status matters more than it looks: a 404 or a 500 is a
    complete, fast, well-formed HTTP response. Counting those as successful
    requests would publish error throughput as endpoint rps — and errors are
    *cheaper* to serve than real work, so the mistake reads as a speedup.
    """
    writer.write(request)
    await writer.drain()

    status = await reader.readline()
    if not status:
        raise LoadError("connection closed before a response")
    parts = status.split(None, 2)
    if len(parts) < 2 or not parts[0].startswith(b"HTTP/"):
        raise LoadError(f"malformed status line: {status[:80]!r}")
    if parts[1] != b"200":
        raise LoadError(f"HTTP {parts[1].decode(errors='replace')}: {status[:80]!r}")

    length = None
    chunked = False
    while True:
        line = await reader.readline()
        if not line:
            raise LoadError("connection closed inside headers")
        if line == b"\r\n":
            break
        lowered = line[:26].lower()
        if lowered.startswith(b"content-length:"):
            length = int(line[15:])
        elif lowered.startswith(b"transfer-encoding:") and b"chunked" in line.lower():
            chunked = True
    if chunked:
        raise LoadError("chunked responses are not supported by this generator")
    if length is None:
        raise LoadError("response had no Content-Length")
    await reader.readexactly(length)
    return length


async def _worker(host, port, path, deadline, latencies, timeout, sizes) -> int:
    request = (
        f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
        f"Accept: */*\r\nConnection: keep-alive\r\n\r\n"
    ).encode()
    reader, writer = await asyncio.open_connection(host, port)
    count = 0
    try:
        while time.perf_counter() < deadline:
            t0 = time.perf_counter()
            length = await asyncio.wait_for(_one_request(reader, writer, request), timeout)
            latencies.append(time.perf_counter() - t0)
            sizes.add(length)
            count += 1
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
    return count


async def run(
    *, host: str = "127.0.0.1", port: int, path: str, connections: int,
    duration: float, warmup: float = 1.0, timeout: float = 30.0,
) -> HttpLoadResult:
    """Closed loop: `connections` keep-alive connections, each issuing
    requests back-to-back until the deadline."""
    latencies: list[float] = []
    sizes: set[int] = set()

    warm_deadline = time.perf_counter() + warmup
    try:
        await _worker(host, port, path, warm_deadline, [], timeout, sizes)
    except (TimeoutError, LoadError, OSError) as exc:
        raise LoadError(f"warmup failed for {path}: {exc}") from exc

    deadline = time.perf_counter() + duration
    t0 = time.perf_counter()
    results = await asyncio.gather(
        *[_worker(host, port, path, deadline, latencies, timeout, sizes) for _ in range(connections)],
        return_exceptions=True,
    )
    wall = time.perf_counter() - t0

    failures = [r for r in results if isinstance(r, BaseException)]
    if failures:
        raise LoadError(
            f"{path}: {len(failures)}/{connections} connections errored, "
            f"first: {failures[0]!r}"
        )
    total = sum(r for r in results if isinstance(r, int))
    if not total:
        raise LoadError(f"{path}: no responses")
    if len(sizes) > 1:
        raise LoadError(f"{path}: response size varied across the run: {sorted(sizes)}")

    latencies.sort()
    return HttpLoadResult(
        path=path, connections=connections, completed=total, elapsed_s=wall,
        rps=total / wall if wall else 0.0,
        mean_ms=statistics.mean(latencies) * 1000 if latencies else 0.0,
        p50_ms=percentile(latencies, 50) * 1000,
        p95_ms=percentile(latencies, 95) * 1000,
        p99_ms=percentile(latencies, 99) * 1000,
        response_bytes=next(iter(sizes), 0),
        latencies_s=latencies,
    )
