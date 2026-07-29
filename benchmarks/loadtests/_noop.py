"""Not a case — no `CASE` constant, so `load/registry.py` skips it. The
framework floor (`/noop`, no database) that `bench load run` hits directly
for its `/noop`-headroom check.
"""

from benchmarks.load.locust import CaseUser


class User(CaseUser):
    path = "/noop"
