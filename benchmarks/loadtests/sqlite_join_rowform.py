"""Locust case: sqlite backend, join shape, rowform (the shipped path)."""

from benchmarks.load.locust import CaseUser

CASE = "sqlite-join-rowform"


class User(CaseUser):
    path = f"/{CASE}"
