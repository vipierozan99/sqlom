"""Locust case: postgres backend, flat shape, rowform (the shipped path)."""

from benchmarks.load.locust import CaseUser

CASE = "postgres-flat-rowform"


class User(CaseUser):
    path = f"/{CASE}"
