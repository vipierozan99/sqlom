from dataclasses import fields
from typing import assert_type

import rowform as rf


@rf.model
class MyModel:
    id: rf.Column[int] = rf.Column(int)
    name: rf.Column[str] = rf.Column(str)
    email: rf.Column[str] = rf.Column(str)
    is_active: rf.Column[bool] = rf.Column(bool)


print(type(MyModel.email))
assert_type(MyModel.email, rf.ColumnExpr[str])

m: MyModel = MyModel(id=1, name="John Doe", email="john.doe@example.com", is_active=True)
print(m)
print(fields(m))
assert m.id == 1
