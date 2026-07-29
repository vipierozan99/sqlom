"""Locust case: postgres backend, flat shape, SQLAlchemy async Core via .mappings()."""

from benchmarks.load.locust import CaseUser

CASE = "postgres-flat-sqlalchemy-async-core-mappings"


class User(CaseUser):
    path = f"/{CASE}"
