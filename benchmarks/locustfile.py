#!/usr/bin/env python3
"""Locust load shape for the FastAPI benchmark — an independent second opinion.

Every end-to-end figure in docs/BENCHMARKS.md §13-14 came from
`benchmarks/httpload.py`, a hand-rolled raw-socket generator. That is a single
point of failure in the measurement: if it silently serialised requests, or
mis-timed them, every ratio published from it would be wrong in the same
direction. This file re-measures the same endpoints with an off-the-shelf tool
that shares none of that code.

Design choices, all made so the two generators measure the *same* thing:

* **`FastHttpUser`, not `HttpUser`.** Locust's default user wraps `requests`,
  which costs roughly 1 ms of client CPU per request — on one pinned core that
  makes the *client* the bottleneck well below the server's throughput.
  `FastHttpUser` uses geventhttpclient and is several times cheaper. Even so,
  see the `/noop` calibration below: the client ceiling must be checked, not
  assumed.
* **`wait_time = constant(0)`.** No think time, so N users means N requests in
  flight at all times — the same closed-loop model as `httpload.py --connections
  N`. With any non-zero wait time the two would not be comparable.
* **Response validation.** Every response is checked for the expected byte
  length, so a run that silently 500s cannot be reported as throughput.

Calibrating the client, which matters more than it sounds: `/noop` returns a
constant with no database work, so its throughput is bounded by whichever side
saturates first. If locust reports roughly the same rps for `/noop` as for a
database endpoint, the client is the bottleneck and the comparison is dead.
Always run `/noop` first and confirm it is well above the endpoints under test.

Usage (server on core 0, locust on core 1, Postgres on cores 2-3):
    taskset -c 1 locust -f benchmarks/locustfile.py --headless \
        --host http://127.0.0.1:8000 -u 8 -r 8 -t 15s --only-summary

Environment:
    LOCUST_PATH       endpoint to hit (default /psy-sqlom)
    LOCUST_EXPECT     expected response body length; 0 disables the check
"""

import os

from locust import FastHttpUser, constant, events, task

PATH = os.environ.get("LOCUST_PATH", "/psy-sqlom")
EXPECT = int(os.environ.get("LOCUST_EXPECT", "0"))


class BenchUser(FastHttpUser):
    # Closed loop: N users == N concurrent in-flight requests, matching
    # httpload.py's connection model so the two generators are comparable.
    wait_time = constant(0)
    # geventhttpclient defaults to a 60s network timeout; a stalled request
    # should surface as a failure inside the run, not hang past the deadline.
    network_timeout = 30.0
    connection_timeout = 10.0

    @task
    def get(self):
        with self.client.get(PATH, name=PATH, catch_response=True) as r:
            if EXPECT and len(r.content) != EXPECT:
                r.failure(f"expected {EXPECT} bytes, got {len(r.content)}")


@events.test_start.add_listener
def _announce(environment, **_):
    print(f"locust -> {PATH}  expect={EXPECT or 'unchecked'} bytes  "
          f"users={environment.runner.target_user_count if environment.runner else '?'}")


@events.test_stop.add_listener
def _warn_on_failures(environment, **_):
    stats = environment.stats.total
    if stats.num_failures:
        print(f"WARNING: {stats.num_failures} failed requests — throughput "
              f"figures from this run are not usable")
