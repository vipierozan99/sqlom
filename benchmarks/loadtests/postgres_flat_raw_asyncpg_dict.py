"""Locust case: postgres backend, flat shape, raw asyncpg + dict baseline."""

from benchmarks.load.locust import CaseUser

CASE = "postgres-flat-raw-asyncpg-dict"


class User(CaseUser):
    path = f"/{CASE}"
