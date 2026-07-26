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


async def worker(host, port, path, deadline, latencies):
    request = (f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
               f"Accept: */*\r\nConnection: keep-alive\r\n\r\n").encode()
    reader, writer = await asyncio.open_connection(host, port)
    count = 0
    try:
        while time.perf_counter() < deadline:
            t0 = time.perf_counter()
            writer.write(request)
            await writer.drain()

            # headers
            length = None
            while True:
                line = await reader.readline()
                if not line:
                    return count
                if line == b"\r\n":
                    break
                if line[:15].lower() == b"content-length:":
                    length = int(line[15:])
            if length is None:
                return count
            await reader.readexactly(length)
            latencies.append(time.perf_counter() - t0)
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
    # warmup on one connection so the server's caches and pool are hot
    warm_deadline = time.perf_counter() + args.warmup
    await worker(args.host, args.port, args.path, warm_deadline, [])

    deadline = time.perf_counter() + args.duration
    t0 = time.perf_counter()
    counts = await asyncio.gather(*[
        worker(args.host, args.port, args.path, deadline, latencies)
        for _ in range(args.connections)
    ])
    wall = time.perf_counter() - t0
    total = sum(counts)
    if not total:
        print(f"FAIL {args.path}: no responses", file=sys.stderr)
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
    args = p.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
