"""Common table expressions: rendering, collection order, and execution on sqlite.

Two things carry most of the risk and get most of the tests.

**Who owns the WITH clause.** Only the outermost render emits one, and it emits
*every* CTE in the graph. A nested render that emitted its own would define the same
CTE twice — which is what the first implementation here did, and the rendering tests
below are what caught it.

**Termination.** A recursive CTE's body refers to the CTE itself, so collecting
references is a walk over a cyclic graph. Without an identity guard it recurses until
the interpreter gives up; `TestRecursive.test_collection_terminates` is that
guard's regression test.
"""

import pytest

from sqlom import (
    CTE,
    Column,
    Delete,
    ModelMeta,
    Query,
    Update,
    count,
    exists,
    recursive_cte,
    sum_,
)

from tests.conftest import Author, Book


def sql_of(query, placeholder="$"):
    return query.to_sql(placeholder=placeholder)[0]


def book_counts(alias="counts"):
    return (Query(Book.author_id, count(Book.id).label("n"))
            .group_by(Book.author_id)
            .cte(alias))


class TestConstruction:
    def test_cte_needs_a_name(self):
        with pytest.raises(TypeError, match="non-empty string alias"):
            Query(Author).cte("")

    def test_cte_name_must_be_an_identifier(self):
        with pytest.raises(ValueError, match="not a valid CTE name"):
            Query(Author).cte("has space")

    def test_unnamed_output_column_is_refused(self):
        # count(id) has no SQL name, so `c.<what>` could not be written. The same
        # rule as Subquery, and for the same reason.
        with pytest.raises(ValueError, match="Add .label"):
            Query(Book.author_id, count(Book.id)).cte("c")

    def test_columns_come_from_the_inner_select(self):
        counts = book_counts()
        assert counts.column_names == ["author_id", "n"]
        assert sorted(counts.__columns__) == ["author_id", "n"]

    def test_unknown_column_names_what_is_available(self):
        counts = book_counts()
        with pytest.raises(AttributeError, match="exposes author_id, n"):
            counts.total

    def test_repr(self):
        assert repr(book_counts()) == "<CTE counts>"
        tree = recursive_cte(
            "tree",
            Query(Book.id, Book.author_id).where(Book.author_id == 1),
            lambda cte: Query(Book.id, Book.author_id).join(cte, Book.id == cte.id),
        )
        assert repr(tree) == "<RECURSIVE CTE tree>"


class TestRendering:
    def test_cte_as_the_primary_source(self):
        counts = book_counts()
        assert sql_of(Query(counts.author_id, counts.n)) == (
            "WITH counts AS (SELECT author_id, count(id) AS n FROM t_books "
            "GROUP BY author_id) SELECT author_id, n FROM counts"
        )

    def test_cte_joined_to_a_table(self):
        counts = book_counts()
        query = (Query(Author, counts.n)
                 .join(counts, counts.author_id == Author.id)
                 .where(counts.n > 1))
        assert sql_of(query) == (
            "WITH counts AS (SELECT author_id, count(id) AS n FROM t_books "
            "GROUP BY author_id) "
            "SELECT t_authors.id, t_authors.name, t_authors.active, counts.n "
            "FROM t_authors JOIN counts ON counts.author_id = t_authors.id "
            "WHERE counts.n > $1"
        )

    def test_the_body_is_defined_once_however_often_it_is_referenced(self):
        counts = book_counts()
        query = (Query(counts.author_id)
                 .where(counts.n > Query(counts.n).limit(1).scalar_subquery()))
        assert sql_of(query).count("WITH") == 1
        assert sql_of(query).count("GROUP BY author_id") == 1

    def test_nested_ctes_are_emitted_in_dependency_order(self):
        inner = book_counts("inner_counts")
        outer = Query(inner.author_id).where(inner.n > 1).cte("outer_ids")
        rendered = sql_of(Query(outer.author_id))
        # One WITH, both entries, inner first — a CTE cannot be referenced before
        # it is defined.
        assert rendered.count("WITH") == 1
        assert rendered.index("inner_counts AS") < rendered.index("outer_ids AS")
        assert rendered == (
            "WITH inner_counts AS (SELECT author_id, count(id) AS n FROM t_books "
            "GROUP BY author_id), "
            "outer_ids AS (SELECT author_id FROM inner_counts WHERE n > $1) "
            "SELECT author_id FROM outer_ids"
        )

    def test_a_cte_referenced_only_inside_a_subquery_is_still_defined(self):
        # The reference is inside an EXISTS, not in FROM or JOIN. A walk that only
        # looked at sources would leave the CTE undefined.
        counts = book_counts()
        query = Query(Author).where(exists(
            Query(counts.author_id).correlate(Author)
                                   .where(counts.author_id == Author.id)
        ))
        rendered = sql_of(query)
        assert rendered.startswith("WITH counts AS (")
        assert rendered.count("WITH") == 1

    def test_cte_in_a_compound_select_is_hoisted_in_front(self):
        counts = book_counts()
        rendered = sql_of(Query(counts.author_id).union(Query(Book.author_id)))
        # In front of the whole compound: an operand carrying its own WITH would be
        # invalid SQL in every position but the first.
        assert rendered == (
            "WITH counts AS (SELECT author_id, count(id) AS n FROM t_books "
            "GROUP BY author_id) SELECT author_id FROM counts "
            "UNION SELECT author_id FROM t_books"
        )

    def test_parameters_are_numbered_with_the_with_clause_first(self):
        body = (Query(Book.author_id, count(Book.id).label("n"))
                .where(Book.id > 5).group_by(Book.author_id).cte("c"))
        sql, params = (Query(body.author_id).where(body.n > 2)
                       .to_sql(placeholder="$"))
        # $1 belongs to the CTE body, $2 to the outer WHERE, matching their order
        # in the text.
        assert sql.index("$1") < sql.index("$2")
        assert params == (5, 2)

    def test_a_query_with_no_cte_is_unchanged(self):
        # The WITH clause is skipped entirely, so every previously rendered query
        # is byte-identical.
        assert sql_of(Query(Author).where(Author.id > 1)) == (
            "SELECT id, name, active FROM t_authors WHERE id > $1"
        )

    def test_with_forces_an_unreferenced_cte_into_the_clause(self):
        counts = book_counts()
        rendered = sql_of(Query(Author).with_(counts))
        assert rendered.startswith("WITH counts AS (")

    def test_with_rejects_a_non_cte(self):
        with pytest.raises(TypeError, match="takes CTEs"):
            Query(Author).with_(Query(Author).subquery("s"))


class TestRecursive:
    def build(self, union_all=True):
        return recursive_cte(
            "tree",
            Query(Book.id, Book.author_id).where(Book.author_id == 1),
            lambda cte: (Query(Book.id, Book.author_id)
                         .join(cte, Book.author_id == cte.id)),
            union_all=union_all,
        )

    def test_renders_with_recursive_and_named_columns(self):
        tree = self.build()
        assert sql_of(Query(tree.id, tree.author_id)) == (
            "WITH RECURSIVE tree(id, author_id) AS ("
            "SELECT id, author_id FROM t_books WHERE author_id = $1 "
            "UNION ALL "
            "SELECT t_books.id, t_books.author_id FROM t_books "
            "JOIN tree ON t_books.author_id = tree.id) "
            "SELECT id, author_id FROM tree"
        )

    def test_union_instead_of_union_all(self):
        assert " UNION SELECT" in sql_of(Query(self.build(union_all=False).id))

    def test_collection_terminates(self):
        # The body refers to the CTE, which refers to the body. An unguarded walk
        # raises RecursionError here rather than producing SQL.
        tree = self.build()
        assert sql_of(Query(tree.id)).count("WITH") == 1

    def test_self_reference_is_not_a_dependency(self):
        # referenced_ctes() drops the CTE itself, or the WITH clause would try to
        # define `tree` before `tree`.
        assert self.build().referenced_ctes() == []

    def test_recursive_marks_the_whole_clause(self):
        tree = self.build()
        counts = book_counts()
        rendered = sql_of(Query(tree.id, counts.n)
                          .join(counts, counts.author_id == tree.id))
        assert rendered.startswith("WITH RECURSIVE ")
        # RECURSIVE is a property of the clause, so the plain CTE rides along.
        assert "counts AS (" in rendered

    def test_step_must_return_a_select(self):
        with pytest.raises(TypeError, match="must be a Query or compound"):
            recursive_cte("t", Query(Book.id), lambda cte: "SELECT 1")


class TestInDml:
    def test_delete_referencing_a_cte(self):
        counts = book_counts()
        statement = Delete(Author).where(
            Author.id.in_(Query(counts.author_id).where(counts.n > 1))
        )
        sql, params = statement.to_sql(placeholder="$")
        assert sql == (
            "WITH counts AS (SELECT author_id, count(id) AS n FROM t_books "
            "GROUP BY author_id) DELETE FROM t_authors "
            "WHERE id IN (SELECT author_id FROM counts WHERE n > $1)"
        )
        assert params == (1,)

    def test_update_referencing_a_cte(self):
        counts = book_counts()
        sql, _ = Update(Author).set(active=False).where(
            Author.id.in_(Query(counts.author_id))
        ).to_sql(placeholder="$")
        assert sql.startswith("WITH counts AS (")
        assert " UPDATE t_authors SET active = " in sql


class TestAgainstSqlite:
    """Executed, not just rendered — a WITH clause in the wrong place parses fine
    often enough that rendering tests alone would not prove much."""

    def test_cte_joined(self, db):
        counts = book_counts()
        query = (Query(Author.name, counts.n)
                 .join(counts, counts.author_id == Author.id)
                 .order_by(Author.name))
        sql, params = query.to_sql(placeholder="?")
        assert db.execute(sql, params).fetchall() == [
            ("ada", 2), ("brian", 1), ("carol", 1)
        ]

    def test_cte_hydrates_a_model(self, db, run_query):
        counts = book_counts()
        rows = run_query(
            Query(Author).join(counts, counts.author_id == Author.id)
                         .where(counts.n > 1)
        )
        assert [author.name for author in rows] == ["ada"]

    def test_nested_ctes(self, db):
        inner = book_counts("inner_counts")
        outer = Query(inner.author_id).where(inner.n > 1).cte("outer_ids")
        sql, params = Query(outer.author_id).to_sql(placeholder="?")
        assert db.execute(sql, params).fetchall() == [(1,)]

    def test_recursive_walk_down_a_tree(self, db):
        # t_books rows are the tree: start at author 1's books, then follow
        # author_id -> id. The data makes this terminate after one step; the point
        # is that sqlite accepts and runs the generated statement.
        tree = recursive_cte(
            "tree",
            Query(Book.id, Book.author_id).where(Book.author_id == 1),
            lambda cte: (Query(Book.id, Book.author_id)
                         .join(cte, Book.author_id == cte.id)),
        )
        sql, params = Query(tree.id).order_by("id").to_sql(placeholder="?")
        assert db.execute(sql, params).fetchall() == [(10,), (11,)]

    def test_recursive_counting_sequence(self, db):
        # A generated series is the clearest proof the recursion actually recurses:
        # this one has to iterate five times to produce five rows.
        class Seq(metaclass=ModelMeta):
            __tablename__ = "t_books"
            id = Column(int)
            author_id = Column(int)
            title = Column(str)

        series = recursive_cte(
            "series",
            Query(Seq.id).where(Seq.id == 10),
            lambda cte: Query((cte.id + 1).label("id")).where(cte.id < 14),
        )
        sql, params = Query(series.id).order_by("id").to_sql(placeholder="?")
        assert db.execute(sql, params).fetchall() == [
            (10,), (11,), (12,), (13,), (14,)
        ]

    def test_cte_used_only_in_a_subquery(self, db):
        counts = book_counts()
        query = Query(Author.name).where(
            Author.id.in_(Query(counts.author_id).where(counts.n > 1))
        ).order_by(Author.name)
        sql, params = query.to_sql(placeholder="?")
        assert db.execute(sql, params).fetchall() == [("ada",)]

    def test_cte_in_a_compound_select(self, db):
        counts = book_counts()
        query = (Query(counts.author_id).where(counts.n > 1)
                 .union(Query(Author.id).where(Author.name == "dan")))
        sql, params = query.to_sql(placeholder="?")
        assert sorted(db.execute(sql, params).fetchall()) == [(1,), (4,)]

    def test_aggregate_over_a_cte(self, db):
        counts = book_counts()
        sql, params = Query(sum_(counts.n).label("total")).to_sql(placeholder="?")
        assert db.execute(sql, params).fetchone() == (4,)


class TestCteType:
    def test_cte_is_the_exported_class(self):
        assert isinstance(book_counts(), CTE)
