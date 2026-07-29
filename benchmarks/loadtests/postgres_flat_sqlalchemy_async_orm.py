"""Locust case: postgres backend, flat shape, SQLAlchemy async ORM."""

from benchmarks.load.locust import CaseUser

CASE = "postgres-flat-sqlalchemy-async-orm"


class User(CaseUser):
    path = f"/{CASE}"
