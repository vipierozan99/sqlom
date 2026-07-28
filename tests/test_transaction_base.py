"""The `Transaction` base class: shared plumbing plus the abstract contract
the driver-specific subclasses (AsyncpgTransaction, PsycopgTransaction) fill
in. No database needed — these are the pieces that don't touch a connection.
"""

import pytest

from sqlom.transaction import Transaction


@pytest.fixture
def bare_transaction():
    return Transaction(engine=None, connection=None)


class TestAbstractContract:
    """A driver subclass that forgot to override one of these would otherwise
    fail confusingly deep inside a query, rather than with a clear error at
    the call that needed it."""

    async def test_fetch_rows_is_not_implemented(self, bare_transaction):
        with pytest.raises(NotImplementedError):
            await bare_transaction._fetch_rows("select 1", ())

    async def test_fetch_value_is_not_implemented(self, bare_transaction):
        with pytest.raises(NotImplementedError):
            await bare_transaction._fetch_value("select 1", ())

    async def test_execute_is_not_implemented(self, bare_transaction):
        with pytest.raises(NotImplementedError):
            await bare_transaction.execute("select 1")

    def test_nested_transaction_is_not_implemented(self, bare_transaction):
        with pytest.raises(NotImplementedError):
            bare_transaction.transaction()


class TestRepr:
    def test_outermost_transaction(self, bare_transaction):
        assert repr(bare_transaction) == "<Transaction transaction>"

    def test_savepoint_reports_its_depth(self):
        savepoint = Transaction(engine=None, connection=None, depth=2)
        assert repr(savepoint) == "<Transaction savepoint depth=2>"
