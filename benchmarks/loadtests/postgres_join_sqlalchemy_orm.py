"""Locust case: postgres backend, join shape, SQLAlchemy async ORM join."""

from benchmarks.load.locust import CaseUser

CASE = "postgres-join-sqlalchemy-orm"


class User(CaseUser):
    path = f"/{CASE}"
