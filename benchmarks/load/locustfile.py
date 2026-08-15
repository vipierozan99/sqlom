"""The one locustfile every `bench load`/`bench profile load` case runs.

Case identity lives in the service app's route table (`load/registry.py`
derives cases from it); this file is deliberately just the traffic generator.
The route arrives via `LOCUST_ROUTE` — set per subprocess by
`locust.run(route=...)`, the same channel `LOCUST_LIMIT`/`LOCUST_EXPECT`
already use — defaulting to the framework floor `/noop`.
"""

import os

from benchmarks.load.locust import CaseUser


class User(CaseUser):
    path = os.environ.get("LOCUST_ROUTE", "/noop")
