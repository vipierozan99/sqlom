"""Locust case: sqlite backend, flat shape, SQLAlchemy ORM."""

from benchmarks.load.locust import CaseUser

CASE = "sqlite-flat-sqlalchemy-orm"


class User(CaseUser):
    path = f"/{CASE}"
