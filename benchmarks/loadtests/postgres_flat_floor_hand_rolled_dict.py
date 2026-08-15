"""Locust case: postgres backend, flat shape, hand-rolled dict floor (raw asyncpg)."""

from benchmarks.load.locust import CaseUser

CASE = "postgres-flat-floor-hand-rolled-dict"


class User(CaseUser):
    path = f"/{CASE}"
