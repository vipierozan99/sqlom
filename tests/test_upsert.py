"""`ON CONFLICT` — rendering, validation, and executed upserts on sqlite.

The interesting part is `excluded`. Inside `DO UPDATE`, an unqualified column is the
*stored* row and `excluded.col` is the row that failed to insert, so
`hits = hits + excluded.hits` accumulates while `hits = excluded.hits` overwrites.
Getting that backwards produces valid SQL with the wrong result, which is why the
sqlite tests below assert the resulting values rather than only the generated text.
"""

import sqlite3

import pytest

from rowform import Alias, Column, Insert, ModelMeta, Query, excluded

from tests.conftest import Author, Book


class Counter(metaclass=ModelMeta):
    __tablename__ = "t_counter"

    key = Column(str)
    hits = Column(int)
    label = Column(str)


def sql_of(statement, placeholder="$"):
    return statement.to_sql(placeholder=placeholder)[0]


class TestDoNothing:
    def test_targeted(self):
        statement = Insert(Author).values(id=1, name="ada", active=True) \
                                  .on_conflict_do_nothing(Author.id)
        sql, params = statement.to_sql(placeholder="$")
        assert sql == (
            "INSERT INTO t_authors (id, name, active) VALUES ($1, $2, $3) "
            "ON CONFLICT (id) DO NOTHING"
        )
        assert params == (1, "ada", True)

    def test_untargeted_swallows_any_unique_violation(self):
        assert sql_of(Insert(Author).values(id=1).on_conflict_do_nothing()).endswith(
            "ON CONFLICT DO NOTHING"
        )

    def test_several_index_elements(self):
        assert sql_of(
            Insert(Book).values(id=1).on_conflict_do_nothing(Book.author_id, Book.title)
        ).endswith("ON CONFLICT (author_id, title) DO NOTHING")

    def test_index_elements_as_strings(self):
        assert sql_of(
            Insert(Author).values(id=1).on_conflict_do_nothing("id")
        ).endswith("ON CONFLICT (id) DO NOTHING")

    def test_named_constraint(self):
        assert sql_of(
            Insert(Author).values(id=1).on_conflict_do_nothing(
                constraint="t_authors_pkey"
            )
        ).endswith("ON CONFLICT ON CONSTRAINT t_authors_pkey DO NOTHING")

    def test_with_returning(self):
        # No row comes back when the conflict fires, which is one way to find out
        # whether the insert happened.
        assert sql_of(
            Insert(Author).values(id=1).on_conflict_do_nothing(Author.id)
                          .returning(Author.id)
        ).endswith("ON CONFLICT (id) DO NOTHING RETURNING id")


class TestDoUpdate:
    def test_overwrite_from_excluded(self):
        statement = (Insert(Counter).values(key="a", hits=1)
                     .on_conflict_do_update(Counter.key,
                                            set_={"hits": excluded(Counter.hits)}))
        assert sql_of(statement) == (
            "INSERT INTO t_counter (key, hits) VALUES ($1, $2) "
            "ON CONFLICT (key) DO UPDATE SET hits = excluded.hits"
        )

    def test_accumulate_from_the_stored_row(self):
        # A bare column is the stored row; that is what makes this an increment
        # rather than an overwrite.
        statement = (Insert(Counter).values(key="a", hits=1)
                     .on_conflict_do_update(
                         Counter.key,
                         set_={"hits": Counter.hits + excluded(Counter.hits)}))
        # The reference is qualified but the assignment target is not: Postgres has
        # both the table and `excluded` in scope inside DO UPDATE, so a bare `hits`
        # on the right is an ambiguous column reference there (sqlite allows it).
        assert sql_of(statement).endswith(
            "DO UPDATE SET hits = (t_counter.hits + excluded.hits)"
        )

    def test_literal_value_binds_a_parameter(self):
        sql, params = (Insert(Counter).values(key="a", hits=1)
                       .on_conflict_do_update(Counter.key, set_={"label": "seen"})
                       .to_sql(placeholder="$"))
        assert sql.endswith("DO UPDATE SET label = $3")
        assert params == ("a", 1, "seen")

    def test_conditional_update(self):
        statement = (Insert(Counter).values(key="a", hits=5)
                     .on_conflict_do_update(
                         Counter.key,
                         set_={"hits": excluded(Counter.hits)},
                         where=Counter.hits < excluded(Counter.hits)))
        assert sql_of(statement).endswith(
            "DO UPDATE SET hits = excluded.hits "
            "WHERE t_counter.hits < excluded.hits"
        )

    def test_several_assignments_keep_their_order(self):
        statement = (Insert(Counter).values(key="a", hits=1)
                     .on_conflict_do_update(
                         Counter.key,
                         set_={"hits": excluded(Counter.hits), "label": "x"}))
        assert "SET hits = excluded.hits, label = " in sql_of(statement)

    def test_bulk_upsert_is_one_statement(self):
        statement = (Insert(Counter)
                     .values([{"key": "a", "hits": 1}, {"key": "b", "hits": 2}])
                     .on_conflict_do_update(Counter.key,
                                            set_={"hits": excluded(Counter.hits)}))
        sql, params = statement.to_sql(placeholder="$")
        assert "VALUES ($1, $2), ($3, $4)" in sql
        assert sql.count("ON CONFLICT") == 1
        assert params == ("a", 1, "b", 2)

    def test_with_returning(self):
        assert sql_of(
            Insert(Counter).values(key="a", hits=1)
            .on_conflict_do_update(Counter.key, set_={"hits": excluded(Counter.hits)})
            .returning(Counter.key, Counter.hits)
        ).endswith("DO UPDATE SET hits = excluded.hits RETURNING key, hits")

    def test_aliased_target_still_writes_unqualified_set_names(self):
        # Postgres rejects `SET alias.col = ...`, so the assignment target is
        # emitted directly rather than through the resolver.
        alias = Alias(Counter, "c")
        statement = (Insert(alias).values(key="a", hits=1)
                     .on_conflict_do_update(alias.key,
                                            set_={"hits": excluded(Counter.hits)}))
        assert sql_of(statement) == (
            "INSERT INTO t_counter AS c (key, hits) VALUES ($1, $2) "
            "ON CONFLICT (key) DO UPDATE SET hits = excluded.hits"
        )


class TestValidation:
    def test_target_and_constraint_are_exclusive(self):
        with pytest.raises(ValueError, match="either index_elements or constraint"):
            Insert(Author).values(id=1).on_conflict_do_nothing(
                Author.id, constraint="c"
            )

    def test_do_update_needs_a_target(self):
        with pytest.raises(ValueError, match="needs the conflicting column"):
            Insert(Counter).values(key="a").on_conflict_do_update(
                set_={"hits": 1}
            )

    def test_empty_set_points_at_do_nothing(self):
        with pytest.raises(TypeError, match="use on_conflict_do_nothing"):
            Insert(Counter).values(key="a").on_conflict_do_update(
                Counter.key, set_={}
            )

    def test_set_is_a_dict_not_kwargs(self):
        with pytest.raises(TypeError, match="non-empty set_"):
            Insert(Counter).values(key="a").on_conflict_do_update(
                Counter.key, set_="hits"
            )

    def test_unknown_set_column(self):
        with pytest.raises(ValueError, match="has no column 'nope'"):
            Insert(Counter).values(key="a").on_conflict_do_update(
                Counter.key, set_={"nope": 1}
            )

    def test_unknown_index_element(self):
        with pytest.raises(ValueError, match="has no column 'nope'"):
            Insert(Counter).values(key="a").on_conflict_do_nothing("nope")

    def test_index_element_must_be_a_column_or_name(self):
        with pytest.raises(TypeError, match="columns or column names"):
            Insert(Counter).values(key="a").on_conflict_do_nothing(7)

    def test_a_second_on_conflict_is_refused(self):
        with pytest.raises(ValueError, match="already has an ON CONFLICT"):
            (Insert(Counter).values(key="a").on_conflict_do_nothing()
             .on_conflict_do_nothing())

    def test_constraint_must_be_an_identifier(self):
        # Not quoted when rendered, so anything that is not a plain name is
        # refused rather than interpolated.
        with pytest.raises(ValueError, match="not a plain identifier"):
            Insert(Counter).values(key="a").on_conflict_do_nothing(
                constraint="t; DROP TABLE t_counter"
            )

    def test_empty_constraint_name(self):
        with pytest.raises(TypeError, match="non-empty constraint name"):
            Insert(Counter).values(key="a").on_conflict_do_nothing(constraint="")

    def test_where_must_be_a_predicate(self):
        with pytest.raises(TypeError, match="takes a predicate"):
            Insert(Counter).values(key="a").on_conflict_do_update(
                Counter.key, set_={"hits": 1}, where="hits < 1"
            )

    def test_another_table_in_set_is_refused(self):
        with pytest.raises(ValueError, match="not the table being written to"):
            (Insert(Counter).values(key="a")
             .on_conflict_do_update(Counter.key, set_={"hits": Book.id})
             .to_sql())

    def test_excluded_from_another_table_is_refused(self):
        # `excluded(Book.id)` would render `excluded.id`, which names a column of
        # t_counter. Checked by source identity so it cannot pass by sharing a name.
        with pytest.raises(ValueError, match="excluded\\(\\) takes a column of"):
            (Insert(Author).values(id=1)
             .on_conflict_do_update(Author.id, set_={"name": excluded(Book.title)})
             .to_sql())

    def test_excluded_needs_a_column(self):
        with pytest.raises(TypeError, match="takes a model column"):
            excluded("hits")

    def test_excluded_of_an_unknown_column_is_refused(self):
        from rowform import ColumnExpr

        # Same source (Counter), but a name that table does not have. Passes
        # the source-identity check in test_excluded_from_another_table_is_refused,
        # then fails the deeper has-no-column check.
        ghost = ColumnExpr(Counter, "nope", int)
        with pytest.raises(ValueError, match="has no column 'nope'"):
            (Insert(Counter).values(key="a")
             .on_conflict_do_update(Counter.key, set_={"hits": excluded(ghost)})
             .to_sql())


class TestExcluded:
    def test_renders_qualified_by_excluded(self):
        assert excluded(Counter.hits).to_sql(lambda: "?") == ("excluded.hits", ())

    def test_reports_no_source(self):
        # `excluded` is not a table in the statement. Reporting one would make
        # every validator demand a join for it.
        assert excluded(Counter.hits).sources() == ()

    def test_keeps_the_column_type_and_name(self):
        assert excluded(Counter.hits).py_type is int
        assert excluded(Counter.hits).output_name() == "hits"

    def test_repr(self):
        assert repr(excluded(Counter.hits)) == "<excluded.hits>"

    def test_composes_into_arithmetic(self):
        expression = Counter.hits + excluded(Counter.hits) * 2
        assert expression.to_sql(lambda: "?")[0] == "(hits + (excluded.hits * ?))"

    def test_is_hashable(self):
        # Excluded has no __hash__ override of its own; this exercises the
        # base Expression.__hash__ that most other node types override.
        node = excluded(Counter.hits)
        assert hash(node) == id(node)


@pytest.fixture
def upsert_db(tmp_path):
    """A fresh sqlite database with a unique index, so a conflict can actually fire."""
    conn = sqlite3.connect(tmp_path / "upsert.sqlite3")
    conn.execute(
        "CREATE TABLE t_counter (key TEXT PRIMARY KEY, hits INTEGER, label TEXT)"
    )
    conn.commit()
    yield conn
    conn.close()


def run(conn, statement):
    sql, params = statement.to_sql(placeholder="?")
    cursor = conn.execute(sql, params)
    rows = cursor.fetchall() if statement.returns_rows else []
    conn.commit()
    return rows


def contents(conn):
    return conn.execute("SELECT key, hits, label FROM t_counter ORDER BY key").fetchall()


class TestAgainstSqlite:
    def test_do_nothing_keeps_the_existing_row(self, upsert_db):
        run(upsert_db, Insert(Counter).values(key="a", hits=1, label="first"))
        run(upsert_db, Insert(Counter).values(key="a", hits=99, label="second")
                       .on_conflict_do_nothing(Counter.key))
        assert contents(upsert_db) == [("a", 1, "first")]

    def test_without_on_conflict_the_conflict_raises(self, upsert_db):
        run(upsert_db, Insert(Counter).values(key="a", hits=1, label="first"))
        with pytest.raises(sqlite3.IntegrityError):
            run(upsert_db, Insert(Counter).values(key="a", hits=2, label="second"))

    def test_do_nothing_returning_yields_no_row_on_conflict(self, upsert_db):
        first = run(upsert_db, Insert(Counter).values(key="a", hits=1)
                    .on_conflict_do_nothing(Counter.key).returning(Counter.key))
        second = run(upsert_db, Insert(Counter).values(key="a", hits=1)
                     .on_conflict_do_nothing(Counter.key).returning(Counter.key))
        assert first == [("a",)]
        assert second == []

    def test_overwrite(self, upsert_db):
        run(upsert_db, Insert(Counter).values(key="a", hits=1, label="first"))
        run(upsert_db, Insert(Counter).values(key="a", hits=7, label="second")
                       .on_conflict_do_update(
                           Counter.key,
                           set_={"hits": excluded(Counter.hits),
                                 "label": excluded(Counter.label)}))
        assert contents(upsert_db) == [("a", 7, "second")]

    def test_accumulate(self, upsert_db):
        statement = (Insert(Counter).values(key="a", hits=1)
                     .on_conflict_do_update(
                         Counter.key,
                         set_={"hits": Counter.hits + excluded(Counter.hits)}))
        for _ in range(4):
            run(upsert_db, statement)
        # The stored value grew by the incoming one each time: bare `hits` really
        # is the stored row.
        assert contents(upsert_db) == [("a", 4, None)]

    def test_conditional_update_leaves_lower_values_alone(self, upsert_db):
        def keep_max(hits):
            return (Insert(Counter).values(key="a", hits=hits)
                    .on_conflict_do_update(
                        Counter.key,
                        set_={"hits": excluded(Counter.hits)},
                        where=Counter.hits < excluded(Counter.hits)))

        run(upsert_db, keep_max(5))
        run(upsert_db, keep_max(9))
        assert contents(upsert_db) == [("a", 9, None)]
        run(upsert_db, keep_max(3))
        assert contents(upsert_db) == [("a", 9, None)]

    def test_bulk_upsert_mixes_inserts_and_updates(self, upsert_db):
        run(upsert_db, Insert(Counter).values(key="a", hits=1))
        run(upsert_db, Insert(Counter)
            .values([{"key": "a", "hits": 10}, {"key": "b", "hits": 20}])
            .on_conflict_do_update(Counter.key,
                                   set_={"hits": excluded(Counter.hits)}))
        assert contents(upsert_db) == [("a", 10, None), ("b", 20, None)]

    def test_upsert_returning_reports_the_stored_row(self, upsert_db):
        run(upsert_db, Insert(Counter).values(key="a", hits=1))
        rows = run(upsert_db, Insert(Counter).values(key="a", hits=4)
                   .on_conflict_do_update(
                       Counter.key,
                       set_={"hits": Counter.hits + excluded(Counter.hits)})
                   .returning(Counter.key, Counter.hits))
        assert rows == [("a", 5)]

    def test_returning_hydrates_a_model(self, upsert_db):
        from rowform import SQLITE_CONVERTERS, compile_batch_hydrator

        statement = (Insert(Counter).values(key="a", hits=1)
                     .on_conflict_do_update(Counter.key, set_={"hits": 2})
                     .returning(Counter))
        rows = run(upsert_db, statement)
        hydrate = compile_batch_hydrator(statement.model, SQLITE_CONVERTERS)
        stored = hydrate(rows)
        # The table was empty, so this took the insert path; hits is the inserted
        # 1, not the 2 the DO UPDATE would have written.
        assert (stored[0].key, stored[0].hits) == ("a", 1)

    def test_a_cte_can_feed_the_conflict_condition(self, upsert_db):
        # Not a common shape, but it proves the WITH clause lands in front of
        # INSERT rather than inside it.
        top = Query(Book.author_id).group_by(Book.author_id).cte("top")
        statement = (Insert(Counter).values(key="a", hits=1)
                     .on_conflict_do_update(
                         Counter.key,
                         set_={"hits": excluded(Counter.hits)},
                         where=Counter.hits.in_(Query(top.author_id))))
        assert statement.to_sql(placeholder="?")[0].startswith("WITH top AS (")
