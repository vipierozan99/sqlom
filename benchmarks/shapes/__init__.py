"""Query shapes benchmarked across every backend and tier.

Kept as separate modules per PLAN.md §4 ("measure more than one shape"): `flat`
backs every published single-table figure, `join` exercises a two-entity
hydrate. Merging them would let a change made for one move a number in the
other — see `join.py`'s docstring.
"""
