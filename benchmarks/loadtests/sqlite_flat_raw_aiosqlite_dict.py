"""Locust case: sqlite backend, flat shape, naive dict(zip(...)) baseline."""

from benchmarks.load.locust import CaseUser

CASE = "sqlite-flat-raw-aiosqlite-dict"


class User(CaseUser):
    path = f"/{CASE}"
