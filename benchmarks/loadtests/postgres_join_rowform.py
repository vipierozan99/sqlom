"""Locust case: postgres backend, join shape, rowform join (the shipped path)."""

from benchmarks.load.locust import CaseUser

CASE = "postgres-join-rowform"


class User(CaseUser):
    path = f"/{CASE}"
