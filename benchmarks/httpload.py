#!/usr/bin/env python3
"""Minimal HTTP/1.1 keep-alive load generator.

No `wrk`/`hey`/`ab` on this box, and an httpx/aiohttp client is heavy enough to
become the bottleneck when the server is pinned to one core. This sends
pre-encoded request bytes over raw asyncio streams and counts responses by
parsing only Content-Length, which is cheap enough to saturate a single-core
server from one core.

Pin the generator away from the server, or you are measuring the two competing
for CPU rather than the server's throughput.

Usage:
    taskset -c 1 python3 benchmarks/httpload.py --path /sqlom --connections 8 --duration 5
"""

import argparse
import asyncio
import statistics
import sys
import time


class LoadError(Exception):
    """A response the run must not be allowed to count as throughput."""


async def _one_request(reader, writer, request):
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


async def worker(host, port, path, deadline, latencies, timeout, sizes):
    request = (f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
               f"Accept: */*\r\nConnection: keep-alive\r\n\r\n").encode()
    reader, writer = await asyncio.open_connection(host, port)
    count = 0
    try:
        while time.perf_counter() < deadline:
            t0 = time.perf_counter()
            # The loop deadline is only checked between requests, so a stalled
            # drain or read would hang past --duration indefinitely. Bound each
            # request instead of trusting the server to always answer.
            length = await asyncio.wait_for(
                _one_request(reader, writer, request), timeout
            )
            latencies.append(time.perf_counter() - t0)
            sizes.add(length)
            count += 1
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
    return count


async def run(args):
    latencies = []
    sizes = set()
    # warmup on one connection so the server's caches and pool are hot
    warm_deadline = time.perf_counter() + args.warmup
    try:
        await worker(args.host, args.port, args.path, warm_deadline, [],
                     args.timeout, sizes)
    except (LoadError, asyncio.TimeoutError, OSError) as exc:
        print(f"FAIL {args.path}: warmup failed: {exc}", file=sys.stderr)
        return 1

    deadline = time.perf_counter() + args.duration
    t0 = time.perf_counter()
    # return_exceptions so one bad connection doesn't cancel the others
    # mid-measurement; the run is failed explicitly below instead.
    results = await asyncio.gather(*[
        worker(args.host, args.port, args.path, deadline, latencies,
               args.timeout, sizes)
        for _ in range(args.connections)
    ], return_exceptions=True)
    wall = time.perf_counter() - t0

    failures = [r for r in results if isinstance(r, BaseException)]
    if failures:
        print(f"FAIL {args.path}: {len(failures)}/{args.connections} connections "
              f"errored, first: {failures[0]!r}", file=sys.stderr)
        return 1
    total = sum(results)
    if not total:
        print(f"FAIL {args.path}: no responses", file=sys.stderr)
        return 1
    # A route that changes payload size mid-run is not serving one workload.
    if len(sizes) > 1:
        print(f"FAIL {args.path}: response size varied across the run: "
              f"{sorted(sizes)}", file=sys.stderr)
        return 1

    latencies.sort()

    def pct(p):
        return latencies[min(int(len(latencies) * p / 100), len(latencies) - 1)] * 1000

    print(f"RESULT\t{args.path}\t{total / wall:.0f}\t{statistics.mean(latencies) * 1000:.3f}"
          f"\t{pct(50):.3f}\t{pct(95):.3f}\t{pct(99):.3f}\t{total}")
    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--path", default="/sqlom")
    p.add_argument("--connections", type=int, default=8)
    p.add_argument("--duration", type=float, default=5.0)
    p.add_argument("--warmup", type=float, default=1.0)
    p.add_argument("--timeout", type=float, default=30.0,
                   help="per-request timeout; bounds a stalled read/write so a "
                        "hung server fails the run instead of hanging it")
    args = p.parse_args()
    for name in ("connections", "duration", "timeout"):
        if getattr(args, name) <= 0:
            p.error(f"--{name} must be > 0")
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
