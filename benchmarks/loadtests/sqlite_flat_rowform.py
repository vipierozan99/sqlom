"""Locust case: sqlite backend, flat shape, rowform (the shipped path)."""

from benchmarks.load.locust import CaseUser

CASE = "sqlite-flat-rowform"


class User(CaseUser):
    path = f"/{CASE}"
