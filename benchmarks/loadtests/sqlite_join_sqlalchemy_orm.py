"""Locust case: sqlite backend, join shape, SQLAlchemy ORM."""

from benchmarks.load.locust import CaseUser

CASE = "sqlite-join-sqlalchemy-orm"


class User(CaseUser):
    path = f"/{CASE}"
