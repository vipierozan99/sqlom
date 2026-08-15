"""Locust case: sqlite backend, flat shape, hand-rolled dict floor (raw aiosqlite)."""

from benchmarks.load.locust import CaseUser

CASE = "sqlite-flat-floor-hand-rolled-dict"


class User(CaseUser):
    path = f"/{CASE}"
