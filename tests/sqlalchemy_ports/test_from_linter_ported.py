"""Inspired by SQLAlchemy's test/sql/test_from_linter.py.

SQLAlchemy's `FromLinter` is an opt-in, *post hoc* graph-connectivity check: you
build a query with `select_from()`/`where()`/joins that may reference several
independent FROM clauses, compile it with `linting=sql.COLLECT_CARTESIAN_PRODUCTS`,
and the linter walks the finished statement looking for FROM entries that are not
transitively connected to the others by an equality (or a join's ON clause) — i.e.
an accidental cartesian product — and warns (or, with `assert_no_cartesian`,
errors).

sqlom has no post hoc linter and needs none: the same class of mistake is a
`ValueError` raised immediately at the *builder call* that introduces the
disconnected reference, not discovered later by walking the finished SQL. That
means most of the shapes `test_from_linter.py` constructs in order to lint them
are not constructible in sqlom in the first place — `Query(a).where(b.col == 5)`
with `b` never joined in raises on the spot (this is `test_plain_cartesian`'s
shape); there is no `select_from()` that silently accepts an unrelated second
table the way SQLAlchemy's does. So rather than porting the linter's internals
(there is nothing here to port — sqlom's equivalent behaviour is already
exercised throughout tests/test_query_sql.py, tests/test_aliases.py and this
package's test_select_ported.py), this file pairs each of the linter's meaningful
*scenarios* with sqlom's build-time equivalent, so the coverage is provable rather
than assumed.

Writing that pairing surfaced a real bug, since `test_plain_cartesian`'s exact
shape has a select-list-only variant nothing previously checked: `Query(a, b)`
or `Query(a.col, b.col)` with *no* `.where()`/`.join()` at all silently rendered
a FROM clause that dropped `b` entirely, rather than raising the same way every
other reference to an unjoined source already did. Fixed by
`Query._check_entities()` (called once per render, alongside the existing
`where()`/`order_by()`/`group_by()`/`join()` checks) — see the dedicated section
below, which is the one part of this file that is a genuine regression test
rather than a "SQLAlchemy would warn, sqlom already refuses" pairing.

One scenario has no sqlom equivalent at all, by design: `test_join_on_true` /
`test_join_on_true_muti_levels` show SQLAlchemy deliberately allowing an
*explicit* cartesian product via `.join(b, true())` — an escape hatch for "yes, I
really mean this". sqlom's `join()` always requires an ON clause that actually
links the new source to one already in the query (see README §12, "the ON clause
links no two tables, so it is a cross join"), and there is no way to spell "link
them anyway" — not a gap, a deliberate design decision, pinned down below as a
`ValueError` rather than silently left untested.

Skipped entirely: `test_lateral_subqueries*` (sqlom has no LATERAL support, see
test_case_labels_windows_ported.py), `test_fn_valued` (table-valued functions,
no sqlom equivalent), `test_dml` (the linter running over UPDATE/DELETE FROM —
already covered by tests/test_update_from.py's own qualification-rule tests),
`TestLinterRoundTrip` (asserting the linter is wired into the real compiler and
does not alter the SQL it lints — an internal SQLAlchemy compiler-pipeline
concern with no sqlom analogue, since there is no separate lint pass to wire in).
"""

import pytest

from sqlom import Alias, Query, count
from tests.conftest import Author, Book, Tag


def sql_of(query, placeholder="$"):
    return query.to_sql(placeholder=placeholder)


# --------------------------------------------------------------------------
# test_plain_cartesian / test_disconnect_between_ab_cd / test_c_and_d_both_
# disconnected: a FROM/WHERE reference to a table never joined in.
# --------------------------------------------------------------------------


# Ported from test/sql/test_from_linter.py::TestFindUnmatchingFroms.test_plain_cartesian (SQLAlchemy 2.0.51)
def test_where_reference_to_an_unjoined_table_is_rejected_immediately():
    # SQLAlchemy's test_plain_cartesian builds `select(a).where(b.col == 5)`
    # and only *later* discovers, by linting the compiled SQL, that `b` is
    # disconnected from `a`. sqlom raises at the where() call itself.
    with pytest.raises(ValueError, match="not part of this query"):
        Query(Author).where(Book.title == "x")


# Ported from test/sql/test_from_linter.py::TestFindUnmatchingFroms.test_plain_cartesian (SQLAlchemy 2.0.51)
def test_order_by_reference_to_an_unjoined_table_is_also_rejected():
    with pytest.raises(ValueError, match="not part of this query"):
        Query(Author).order_by(Book.id)


# Ported from test/sql/test_from_linter.py::TestFindUnmatchingFroms.test_plain_cartesian (SQLAlchemy 2.0.51)
def test_group_by_reference_to_an_unjoined_table_is_also_rejected():
    with pytest.raises(ValueError, match="not part of this query"):
        Query(Author).group_by(Book.author_id)


# Ported from test/sql/test_from_linter.py::TestFindUnmatchingFroms.test_c_and_d_both_disconnected (SQLAlchemy 2.0.51)
def test_a_second_join_still_disconnected_from_a_third_table_is_rejected():
    # Mirrors test_c_and_d_both_disconnected's shape (a joined to b, c and d
    # both floating free) — but sqlom catches it at the join() call for
    # whichever of c/d is added second, rather than after the fact: Tag's ON
    # clause links no source already in the query, so it is a cross join.
    with pytest.raises(ValueError, match="cross join"):
        (Query(Author)
         .join(Book, Book.author_id == Author.id)
         .join(Tag, Tag.label == "x"))  # no link to Author or Book


# --------------------------------------------------------------------------
# The one shape none of where()/order_by()/group_by()/join() protect: the
# select list itself. This was a real bug, found while working through this
# file — plain_cartesian's exact shape, one level up: `Query(a, b)` or
# `Query(a.col, b.col)` with no join at all between a and b used to render
# silently, dropping `b` from FROM entirely rather than raising. Fixed by
# Query._check_entities(), called once per render.
# --------------------------------------------------------------------------


# sqlom-original test (no SQLAlchemy equivalent)
def test_selecting_two_unjoined_models_is_rejected():
    with pytest.raises(ValueError, match="not part of this query's FROM/JOIN"):
        Query(Author, Book).to_sql()


# sqlom-original test (no SQLAlchemy equivalent)
def test_selecting_columns_from_two_unjoined_tables_is_rejected():
    with pytest.raises(ValueError, match="not part of this query"):
        Query(Author.name, Book.title).to_sql()


# sqlom-original test (no SQLAlchemy equivalent)
def test_selecting_two_models_after_joining_them_still_works():
    # The fix must not disturb the ordinary, already-well-tested case.
    sql, _ = Query(Author, Book).join(Book, Book.author_id == Author.id).to_sql()
    assert "t_books.id" in sql and "t_authors.id" in sql


# sqlom-original test (no SQLAlchemy equivalent)
def test_count_of_a_model_still_supplies_its_own_from_with_nothing_else_selected():
    # count(Model) is meant to work with no join at all — it names its own
    # table (see README §6) — and must not be caught by the new check.
    sql, _ = Query(count(Author)).to_sql()
    assert sql == "SELECT count(*) FROM t_authors"


# --------------------------------------------------------------------------
# test_now_connected / test_now_connect_it: adding the missing link clears it.
# --------------------------------------------------------------------------


# Ported from test/sql/test_from_linter.py::TestFindUnmatchingFroms.test_now_connected (SQLAlchemy 2.0.51)
def test_adding_the_missing_join_condition_fixes_it():
    # The build-time equivalent of test_now_connected: once every table is
    # actually joined by a real linking condition, the query builds cleanly.
    query = (
        Query(Author, Book, Tag)
        .join(Book, Book.author_id == Author.id)
        .join(Tag, Tag.book_id == Book.id)
        .where(Author.active == True)  # noqa: E712
    )
    sql, params = sql_of(query)
    assert sql == (
        "SELECT t_authors.id, t_authors.name, t_authors.active, "
        "t_books.id, t_books.author_id, t_books.title, "
        "t_tags.id, t_tags.book_id, t_tags.label "
        "FROM t_authors "
        "JOIN t_books ON t_books.author_id = t_authors.id "
        "JOIN t_tags ON t_tags.book_id = t_books.id "
        "WHERE t_authors.active = $1"
    )
    assert params == (True,)


# --------------------------------------------------------------------------
# test_disconnected_subquery / test_now_connect_it: a derived table that is
# not actually linked to anything else in the query.
# --------------------------------------------------------------------------


# Ported from test/sql/test_from_linter.py::TestFindUnmatchingFroms.test_disconnected_subquery (SQLAlchemy 2.0.51)
def test_joining_a_subquery_with_an_unrelated_on_clause_is_rejected():
    busy = Query(Book.author_id).subquery("busy")
    with pytest.raises(ValueError, match="cross join"):
        Query(Author).join(busy, Author.active == True)  # noqa: E712


# Ported from test/sql/test_from_linter.py::TestFindUnmatchingFroms.test_now_connect_it (SQLAlchemy 2.0.51)
def test_joining_a_subquery_with_the_real_link_column_works():
    busy = Query(Book.author_id).subquery("busy")
    query = Query(Author).join(busy, busy.author_id == Author.id)
    sql, _ = sql_of(query)
    assert "JOIN (SELECT author_id FROM t_books) AS busy ON busy.author_id = t_authors.id" in sql


# --------------------------------------------------------------------------
# test_right_nested_join_without_issue / _with_an_issue: a three-way join
# chain, and a fourth table left disconnected from it.
# --------------------------------------------------------------------------


# Ported from test/sql/test_from_linter.py::TestFindUnmatchingFroms.test_right_nested_join_without_issue (SQLAlchemy 2.0.51)
def test_three_way_join_chain_with_no_disconnected_table():
    query = (
        Query(Author, Book, Tag)
        .join(Book, Book.author_id == Author.id)
        .join(Tag, Tag.book_id == Book.id)
    )
    sql, _ = sql_of(query)
    assert sql.count(" JOIN ") == 2
    for source in (Author, Book, Tag):
        assert source in query._sources()


# Ported from test/sql/test_from_linter.py::TestFindUnmatchingFroms.test_right_nested_join_with_an_issue (SQLAlchemy 2.0.51)
def test_a_fourth_disconnected_table_is_rejected_even_after_a_valid_chain():
    other = Alias(Author, "other")
    with pytest.raises(ValueError, match="not part of this query"):
        (Query(Author, Book, Tag)
         .join(Book, Book.author_id == Author.id)
         .join(Tag, Tag.book_id == Book.id)
         .where(other.id == 1))


# --------------------------------------------------------------------------
# test_join_on_true / test_join_on_true_muti_levels: SQLAlchemy's explicit
# "yes, cartesian product, on purpose" escape hatch. sqlom has none.
# --------------------------------------------------------------------------


# Ported from test/sql/test_from_linter.py::TestFindUnmatchingFroms.test_join_on_true (SQLAlchemy 2.0.51)
def test_there_is_no_escape_hatch_for_an_intentional_cartesian_product():
    # Unlike SQLAlchemy's join(b, true()), sqlom's join() always requires an
    # ON clause that actually links the new source to one already present —
    # a deliberate design decision (README §12), not an oversight. There is
    # no sqlom spelling of "cross join, and I mean it".
    with pytest.raises(ValueError, match="cross join"):
        Query(Author).join(Book, Author.active == True)  # noqa: E712
