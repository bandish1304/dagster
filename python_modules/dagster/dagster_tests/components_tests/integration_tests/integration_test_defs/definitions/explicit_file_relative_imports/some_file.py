import dagster as dg

from .some_other_file import asset_in_some_other_file as asset_in_some_other_file  # noqa: TID252


@dg.asset
def asset_in_some_file() -> None: ...
