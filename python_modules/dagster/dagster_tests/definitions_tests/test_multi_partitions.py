from datetime import datetime
from typing import cast
from unittest import mock

import dagster as dg
import pytest
from dagster import AssetExecutionContext
from dagster._check import CheckError
from dagster._core.definitions.assets.graph.asset_graph import AssetGraph
from dagster._core.definitions.partitions.context import (
    PartitionLoadingContext,
    partition_loading_context,
)
from dagster._core.definitions.partitions.utils import get_time_partitions_def
from dagster._core.definitions.temporal_context import TemporalContext
from dagster._core.storage.tags import get_multidimensional_partition_tag
from dagster._time import create_datetime

DATE_FORMAT = "%Y-%m-%d"


def test_invalid_chars():
    valid_partitions = dg.StaticPartitionsDefinition(["x", "y", "z"])
    with pytest.raises(dg.DagsterInvalidDefinitionError):
        dg.MultiPartitionsDefinition(
            {"abc": dg.StaticPartitionsDefinition(["aasdasd|asdas"]), "blah": valid_partitions}
        )
    with pytest.raises(dg.DagsterInvalidDefinitionError):
        dg.MultiPartitionsDefinition(
            {"abc": dg.StaticPartitionsDefinition(["aasas[asdas"]), "blah": valid_partitions}
        )
    with pytest.raises(dg.DagsterInvalidDefinitionError):
        dg.MultiPartitionsDefinition(
            {"abc": dg.StaticPartitionsDefinition(["aasas]asdas"]), "blah": valid_partitions}
        )
    with pytest.raises(dg.DagsterInvalidDefinitionError):
        dg.MultiPartitionsDefinition(
            {"abc": dg.StaticPartitionsDefinition(["asda", "a,s"]), "blah": valid_partitions}
        )


def test_multi_static_partitions():
    partitions1 = dg.StaticPartitionsDefinition(["a", "b", "c"])
    partitions2 = dg.StaticPartitionsDefinition(["x", "y", "z"])
    composite = dg.MultiPartitionsDefinition({"abc": partitions1, "xyz": partitions2})
    assert composite.get_partition_keys() == [
        "a|x",
        "a|y",
        "a|z",
        "b|x",
        "b|y",
        "b|z",
        "c|x",
        "c|y",
        "c|z",
    ]


def test_multi_dimensional_time_window_static_partitions():
    time_window_partitions = dg.DailyPartitionsDefinition(start_date="2021-05-05")
    static_partitions = dg.StaticPartitionsDefinition(["a", "b", "c"])
    composite = dg.MultiPartitionsDefinition(
        {"date": time_window_partitions, "abc": static_partitions}
    )
    partition_keys = composite.get_partition_keys(
        current_time=datetime.strptime("2021-05-07", DATE_FORMAT)
    )
    assert set(partition_keys) == {
        "a|2021-05-05",
        "b|2021-05-05",
        "c|2021-05-05",
        "a|2021-05-06",
        "b|2021-05-06",
        "c|2021-05-06",
    }

    assert partition_keys[0].keys_by_dimension["date"] == "2021-05-05"
    assert partition_keys[0].keys_by_dimension["abc"] == "a"


def test_tags_multi_dimensional_partitions():
    time_window_partitions = dg.DailyPartitionsDefinition(start_date="2021-05-05")
    static_partitions = dg.StaticPartitionsDefinition(["a", "b", "c"])
    composite = dg.MultiPartitionsDefinition(
        {"date": time_window_partitions, "abc": static_partitions}
    )

    @dg.asset(partitions_def=composite)
    def asset1():
        return 1

    @dg.asset(partitions_def=composite)
    def asset2(asset1):
        return 2

    @dg.repository
    def my_repo():
        return [asset1, asset2, dg.define_asset_job("my_job", partitions_def=composite)]

    with dg.instance_for_test() as instance:
        result = (
            my_repo()
            .get_job("my_job")
            .execute_in_process(
                partition_key=dg.MultiPartitionKey({"abc": "a", "date": "2021-06-01"}),
                instance=instance,
            )
        )
        assert result.success
        assert result.dagster_run.tags[get_multidimensional_partition_tag("abc")] == "a"
        assert result.dagster_run.tags[get_multidimensional_partition_tag("date")] == "2021-06-01"

        asset1_records = instance.fetch_materializations(asset1.key, limit=1000).records
        asset2_records = instance.fetch_materializations(asset2.key, limit=1000).records
        materializations = sorted(
            [*asset1_records, *asset2_records],
            key=lambda x: x.event_log_entry.dagster_event.asset_key,  # type: ignore
        )
        assert len(materializations) == 2

        for materialization in materializations:
            assert materialization.event_log_entry.dagster_event.partition == dg.MultiPartitionKey(
                {"abc": "a", "date": "2021-06-01"}
            )


multipartitions_def = dg.MultiPartitionsDefinition(
    {
        "date": dg.DailyPartitionsDefinition(start_date="2015-01-01"),
        "static": dg.StaticPartitionsDefinition(["a", "b", "c", "d"]),
    }
)


def test_multipartitions_backcompat_subset_serialization():
    partitions1 = dg.StaticPartitionsDefinition(["a", "b", "c"])
    partitions2 = dg.StaticPartitionsDefinition(["x", "y", "z"])
    composite = dg.MultiPartitionsDefinition({"abc": partitions1, "xyz": partitions2})

    partition_keys = [
        dg.MultiPartitionKey({"abc": "a", "xyz": "x"}),
        dg.MultiPartitionKey({"abc": "c", "xyz": "z"}),
    ]
    serialization = '["a|x", "c|z"]'
    assert composite.deserialize_subset(serialization).get_partition_keys() == set(partition_keys)

    version_1_serialization = '{"version": 1, "subset": ["a|x", "c|z"]}'
    assert composite.deserialize_subset(version_1_serialization).get_partition_keys() == set(
        partition_keys
    )


def test_multipartitions_subset_serialization():
    partitions1 = dg.StaticPartitionsDefinition(["a", "b", "c"])
    partitions2 = dg.StaticPartitionsDefinition(["x", "y", "z"])
    composite = dg.MultiPartitionsDefinition({"abc": partitions1, "xyz": partitions2})

    partition_keys = [
        dg.MultiPartitionKey({"abc": "a", "xyz": "x"}),
        dg.MultiPartitionKey({"abc": "c", "xyz": "z"}),
    ]
    assert composite.deserialize_subset(
        composite.empty_subset().with_partition_keys(partition_keys).serialize()
    ).get_partition_keys() == set(partition_keys)


def test_multipartitions_subset_equality():
    assert multipartitions_def.empty_subset().with_partition_keys(
        [
            dg.MultiPartitionKey({"static": "a", "date": "2015-01-01"}),
            dg.MultiPartitionKey({"static": "b", "date": "2015-01-05"}),
        ]
    ) == multipartitions_def.empty_subset().with_partition_keys(
        [
            dg.MultiPartitionKey({"static": "a", "date": "2015-01-01"}),
            dg.MultiPartitionKey({"static": "b", "date": "2015-01-05"}),
        ]
    )

    assert multipartitions_def.empty_subset().with_partition_keys(
        [
            dg.MultiPartitionKey({"static": "c", "date": "2015-01-01"}),
            dg.MultiPartitionKey({"static": "b", "date": "2015-01-05"}),
        ]
    ) != multipartitions_def.empty_subset().with_partition_keys(
        [
            dg.MultiPartitionKey({"static": "a", "date": "2015-01-01"}),
            dg.MultiPartitionKey({"static": "b", "date": "2015-01-05"}),
        ]
    )

    assert multipartitions_def.empty_subset().with_partition_keys(
        [
            dg.MultiPartitionKey({"static": "a", "date": "2015-01-01"}),
            dg.MultiPartitionKey({"static": "b", "date": "2015-01-05"}),
        ]
    ) != multipartitions_def.empty_subset().with_partition_keys(
        [
            dg.MultiPartitionKey({"static": "a", "date": "2016-01-01"}),
            dg.MultiPartitionKey({"static": "b", "date": "2015-01-05"}),
        ]
    )


@pytest.mark.parametrize(
    "initial, added",
    [
        (["------", "+-----", "------", "------"], ["+-----", "+-----", "------", "------"]),
        (
            ["+--+--", "------", "------", "------"],
            ["+-----", "------", "------", "------"],
        ),
        (
            ["+------", "-+-----", "-++--+-", "+-+++++"],
            ["-+-----", "-+-----", "+-+-+-+", "+++----"],
        ),
        (
            ["+-----+", "------+", "-+++---", "-------"],
            ["+++++++", "-+-+-+-", "-++----", "----+++"],
        ),
    ],
)
def test_multipartitions_subset_addition(initial, added):
    assert len(initial) == len(added)

    static_keys = ["a", "b", "c", "d"]
    daily_partitions_def = dg.DailyPartitionsDefinition(start_date="2015-01-01")
    multipartitions_def = dg.MultiPartitionsDefinition(
        {
            "date": daily_partitions_def,
            "static": dg.StaticPartitionsDefinition(static_keys),
        }
    )
    full_date_set_keys = daily_partitions_def.get_partition_keys()[
        : max(len(keys) for keys in initial)
    ]
    current_day = datetime.strptime(
        daily_partitions_def.get_partition_keys()[: max(len(keys) for keys in initial) + 1][-1],
        daily_partitions_def.fmt,
    )

    initial_subset_keys = []
    added_subset_keys = []
    expected_keys_not_in_updated_subset = []
    for i in range(len(initial)):
        for j in range(len(initial[i])):
            if initial[i][j] == "+":
                initial_subset_keys.append(
                    dg.MultiPartitionKey({"date": full_date_set_keys[j], "static": static_keys[i]})
                )

            if added[i][j] == "+":
                added_subset_keys.append(
                    dg.MultiPartitionKey({"date": full_date_set_keys[j], "static": static_keys[i]})
                )

            if initial[i][j] != "+" and added[i][j] != "+":
                expected_keys_not_in_updated_subset.append(
                    dg.MultiPartitionKey({"date": full_date_set_keys[j], "static": static_keys[i]})
                )

    initial_subset = multipartitions_def.empty_subset().with_partition_keys(initial_subset_keys)
    added_subset = initial_subset.with_partition_keys(added_subset_keys)

    assert initial_subset.get_partition_keys() == set(initial_subset_keys)
    assert added_subset.get_partition_keys() == set(added_subset_keys + initial_subset_keys)
    with partition_loading_context(effective_dt=current_day):
        assert added_subset.get_partition_keys_not_in_subset(multipartitions_def) == set(
            expected_keys_not_in_updated_subset
        )


def test_asset_partition_key_is_multipartition_key():
    class MyIOManager(dg.IOManager):
        def handle_output(self, context, obj):
            assert isinstance(context.asset_partition_key, dg.MultiPartitionKey)

        def load_input(self, context):
            assert isinstance(context.asset_partition_key, dg.MultiPartitionKey)
            return 1

    partitions_def = dg.MultiPartitionsDefinition(
        {"a": dg.StaticPartitionsDefinition(["a"]), "b": dg.StaticPartitionsDefinition(["b"])}
    )

    @dg.asset(
        partitions_def=partitions_def,
        io_manager_key="my_io_manager",
    )
    def my_asset(context):
        return 1

    @dg.asset(
        partitions_def=partitions_def,
        io_manager_key="my_io_manager",
    )
    def asset2(context, my_asset):
        return 2

    dg.materialize(
        [my_asset, asset2],
        resources={"my_io_manager": MyIOManager()},
        partition_key="a|b",
    )


def test_keys_with_dimension_value():
    static_keys = ["a", "b", "c", "d"]
    daily_partitions_def = dg.DailyPartitionsDefinition(start_date="2015-01-01")
    multipartitions_def = dg.MultiPartitionsDefinition(
        {
            "date": daily_partitions_def,
            "static": dg.StaticPartitionsDefinition(static_keys),
        }
    )

    with partition_loading_context(effective_dt=datetime(year=2015, month=1, day=5)):
        assert multipartitions_def.get_multipartition_keys_with_dimension_value("static", "a") == [
            dg.MultiPartitionKey({"static": val[0], "date": val[1]})
            for val in [
                ("a", "2015-01-01"),
                ("a", "2015-01-02"),
                ("a", "2015-01-03"),
                ("a", "2015-01-04"),
            ]
        ]
    with partition_loading_context(effective_dt=datetime(year=2015, month=1, day=5)):
        assert multipartitions_def.get_multipartition_keys_with_dimension_value(
            "date", "2015-01-01"
        ) == [
            dg.MultiPartitionKey({"static": val[0], "date": val[1]})
            for val in [
                ("a", "2015-01-01"),
                ("b", "2015-01-01"),
                ("c", "2015-01-01"),
                ("d", "2015-01-01"),
            ]
        ]


def test_keys_with_dimension_value_with_dynamic():
    daily_partitions_def = dg.DailyPartitionsDefinition(start_date="2015-01-01")
    dynamic_partitions_def = dg.DynamicPartitionsDefinition(name="dummy")
    multipartitions_def = dg.MultiPartitionsDefinition(
        {
            "date": daily_partitions_def,
            "dynamic": dynamic_partitions_def,
        }
    )

    with dg.instance_for_test() as instance:
        instance.add_dynamic_partitions(dynamic_partitions_def.name, ["a", "b", "c", "d"])  # pyright: ignore[reportArgumentType]

        with partition_loading_context(
            effective_dt=datetime(year=2015, month=1, day=5), dynamic_partitions_store=instance
        ):
            assert multipartitions_def.get_multipartition_keys_with_dimension_value(
                dimension_name="dynamic",
                dimension_partition_key="a",
            ) == [
                dg.MultiPartitionKey({"dynamic": val[0], "date": val[1]})
                for val in [
                    ("a", "2015-01-01"),
                    ("a", "2015-01-02"),
                    ("a", "2015-01-03"),
                    ("a", "2015-01-04"),
                ]
            ]

        with partition_loading_context(
            effective_dt=datetime(year=2015, month=1, day=5),
            dynamic_partitions_store=instance,
        ):
            assert multipartitions_def.get_multipartition_keys_with_dimension_value(
                dimension_name="date",
                dimension_partition_key="2015-01-01",
            ) == [
                dg.MultiPartitionKey({"dynamic": val[0], "date": val[1]})
                for val in [
                    ("a", "2015-01-01"),
                    ("b", "2015-01-01"),
                    ("c", "2015-01-01"),
                    ("d", "2015-01-01"),
                ]
            ]


def test_keys_with_dimension_value_with_dynamic_without_instance():
    daily_partitions_def = dg.DailyPartitionsDefinition(start_date="2015-01-01")
    dynamic_partitions_def = dg.DynamicPartitionsDefinition(name="dummy")
    multipartitions_def = dg.MultiPartitionsDefinition(
        {
            "date": daily_partitions_def,
            "dynamic": dynamic_partitions_def,
        }
    )

    with pytest.raises(CheckError):
        with partition_loading_context(effective_dt=datetime(year=2015, month=1, day=5)):
            multipartitions_def.get_multipartition_keys_with_dimension_value(
                dimension_name="date", dimension_partition_key="2015-01-01"
            )


def test_get_num_partitions():
    static_keys = ["a", "b", "c", "d"]
    daily_partitions_def = dg.DailyPartitionsDefinition(start_date="2015-01-01")
    multipartitions_def = dg.MultiPartitionsDefinition(
        {
            "date": daily_partitions_def,
            "static": dg.StaticPartitionsDefinition(static_keys),
        }
    )
    assert multipartitions_def.get_num_partitions() == len(
        set(multipartitions_def.get_partition_keys())
    )


def test_dynamic_dimension_in_multipartitioned_asset():
    multipartitions_def = dg.MultiPartitionsDefinition(
        {
            "static": dg.StaticPartitionsDefinition(["a", "b", "c"]),
            "dynamic": dg.DynamicPartitionsDefinition(name="dynamic"),
        }
    )

    @dg.asset(partitions_def=multipartitions_def)
    def my_asset(context):
        assert context.partition_key == dg.MultiPartitionKey({"static": "a", "dynamic": "1"})
        return 1

    @dg.asset(partitions_def=multipartitions_def)
    def asset2(context, my_asset):
        return 2

    dynamic_multipartitioned_job = dg.define_asset_job(
        "dynamic_multipartitioned_job", [my_asset, asset2], partitions_def=multipartitions_def
    ).resolve(asset_graph=AssetGraph.from_assets([my_asset, asset2]))

    with dg.instance_for_test() as instance:
        instance.add_dynamic_partitions("dynamic", ["1"])
        assert dg.materialize([my_asset, asset2], partition_key="1|a", instance=instance).success

        assert dynamic_multipartitioned_job.execute_in_process(
            instance=instance, partition_key="1|a"
        ).success


def test_invalid_dynamic_partitions_def_in_multipartitioned():
    with pytest.raises(
        dg.DagsterInvalidDefinitionError,
        match="must have a name",
    ):
        dg.MultiPartitionsDefinition(
            {
                "static": dg.StaticPartitionsDefinition(["a", "b", "c"]),
                "dynamic": dg.DynamicPartitionsDefinition(lambda x: ["1", "2", "3"]),
            }
        )


def test_context_partition_time_window():
    partitions_def = dg.MultiPartitionsDefinition(
        {
            "date": dg.DailyPartitionsDefinition(start_date="2020-01-01"),
            "static": dg.StaticPartitionsDefinition(["a", "b"]),
        }
    )

    @dg.asset(partitions_def=partitions_def)
    def my_asset(context: AssetExecutionContext):
        time_partition = get_time_partitions_def(partitions_def)
        if time_partition is None:
            assert False, "expected a time component in the partitions definition"

        time_window = dg.TimeWindow(
            start=create_datetime(year=2020, month=1, day=1, tz=time_partition.timezone),
            end=create_datetime(year=2020, month=1, day=2, tz=time_partition.timezone),
        )
        assert context.partition_time_window == time_window
        return 1

    multipartitioned_job = dg.define_asset_job(
        "my_job", [my_asset], partitions_def=partitions_def
    ).resolve(asset_graph=AssetGraph.from_assets([my_asset]))
    multipartitioned_job.execute_in_process(
        partition_key=dg.MultiPartitionKey({"date": "2020-01-01", "static": "a"})
    )


def test_context_invalid_partition_time_window():
    partitions_def = dg.MultiPartitionsDefinition(
        {
            "static2": dg.StaticPartitionsDefinition(["a", "b"]),
            "static": dg.StaticPartitionsDefinition(["a", "b"]),
        }
    )

    @dg.asset(partitions_def=partitions_def)
    def my_asset(context):
        context.partition_time_window  # noqa: B018

    multipartitioned_job = dg.define_asset_job(
        "my_job", [my_asset], partitions_def=partitions_def
    ).resolve(asset_graph=AssetGraph.from_assets([my_asset]))
    with pytest.raises(
        dg.DagsterInvariantViolationError,
        match=(
            "Expected a TimeWindowPartitionsDefinition or MultiPartitionsDefinition with a single"
            " time dimension"
        ),
    ):
        multipartitioned_job.execute_in_process(
            partition_key=dg.MultiPartitionKey({"static2": "b", "static": "a"})
        )


def test_multipartitions_self_dependency():
    @dg.asset(
        partitions_def=dg.MultiPartitionsDefinition(
            {
                "time": dg.DailyPartitionsDefinition(start_date="2020-01-01"),
                "abc": dg.StaticPartitionsDefinition(["a", "b", "c"]),
            }
        ),
        ins={
            "a": dg.AssetIn(
                partition_mapping=dg.MultiPartitionMapping(
                    {
                        "time": dg.DimensionPartitionMapping(
                            "time", dg.TimeWindowPartitionMapping(start_offset=-1, end_offset=-1)
                        ),
                        "abc": dg.DimensionPartitionMapping("abc", dg.IdentityPartitionMapping()),
                    }
                )
            )
        },
    )
    def a(a):
        return 1

    first_partition_key = dg.MultiPartitionKey({"time": "2020-01-01", "abc": "a"})
    second_partition_key = dg.MultiPartitionKey({"time": "2020-01-02", "abc": "a"})

    class MyIOManager(dg.IOManager):
        def handle_output(self, context, obj): ...

        def load_input(self, context):
            assert context.asset_key.path[-1] == "a"
            if context.partition_key == first_partition_key:
                assert context.asset_partition_keys == []
                assert context.has_asset_partitions
            else:
                assert context.partition_key == second_partition_key
                assert context.asset_partition_keys == [first_partition_key]
                assert context.asset_partition_key == first_partition_key
                assert context.asset_partition_key_range == dg.PartitionKeyRange(
                    first_partition_key, first_partition_key
                )
                assert context.has_asset_partitions

    resources = {"io_manager": MyIOManager()}

    dg.materialize(
        [a],
        partition_key=first_partition_key,
        resources=resources,
    )
    dg.materialize(
        [a],
        partition_key=second_partition_key,
        resources=resources,
    )


def test_context_returns_multipartition_keys():
    partitions_def = dg.MultiPartitionsDefinition(
        {
            "a": dg.StaticPartitionsDefinition(["a", "b"]),
            "1": dg.StaticPartitionsDefinition(["1", "2"]),
        }
    )

    @dg.asset(partitions_def=partitions_def)
    def upstream(context):
        assert isinstance(context.partition_key, dg.MultiPartitionKey)

    @dg.asset(partitions_def=partitions_def)
    def downstream(context: AssetExecutionContext, upstream):
        assert isinstance(context.partition_key, dg.MultiPartitionKey)

        input_range = context.asset_partition_key_range_for_input("upstream")
        assert isinstance(input_range.start, dg.MultiPartitionKey)
        assert isinstance(input_range.end, dg.MultiPartitionKey)

        output = context.partition_key_range
        assert isinstance(output.start, dg.MultiPartitionKey)
        assert isinstance(output.end, dg.MultiPartitionKey)

    dg.materialize([upstream, downstream], partition_key="1|a")


def test_multipartitions_range_cartesian_single_key_in_secondary():
    partitions_def = dg.MultiPartitionsDefinition(
        {
            "a": dg.DailyPartitionsDefinition(start_date="2024-01-01"),
            "b": dg.StaticPartitionsDefinition(["1", "2", "3", "4", "5"]),
        }
    )

    partition_range = partitions_def.get_partition_keys_in_range(
        dg.PartitionKeyRange(
            dg.MultiPartitionKey({"a": "2024-01-01", "b": "2"}),
            dg.MultiPartitionKey({"a": "2024-01-03", "b": "2"}),
        )
    )

    assert partition_range == [
        dg.MultiPartitionKey({"a": "2024-01-01", "b": "2"}),
        dg.MultiPartitionKey({"a": "2024-01-02", "b": "2"}),
        dg.MultiPartitionKey({"a": "2024-01-03", "b": "2"}),
    ]


def test_multipartitions_range_cartesian_single_key_in_primary():
    partitions_def = dg.MultiPartitionsDefinition(
        {
            "a": dg.DailyPartitionsDefinition(start_date="2024-01-01"),
            "b": dg.StaticPartitionsDefinition(["1", "2", "3", "4", "5"]),
        }
    )

    partition_range = partitions_def.get_partition_keys_in_range(
        dg.PartitionKeyRange(
            dg.MultiPartitionKey({"a": "2024-01-01", "b": "2"}),
            dg.MultiPartitionKey({"a": "2024-01-01", "b": "4"}),
        )
    )

    assert partition_range == [
        dg.MultiPartitionKey({"a": "2024-01-01", "b": "2"}),
        dg.MultiPartitionKey({"a": "2024-01-01", "b": "3"}),
        dg.MultiPartitionKey({"a": "2024-01-01", "b": "4"}),
    ]


def test_multipartitions_range_cartesian_multiple_keys_in_both_ranges():
    partitions_def = dg.MultiPartitionsDefinition(
        {
            "a": dg.DailyPartitionsDefinition(start_date="2024-01-01"),
            "b": dg.StaticPartitionsDefinition(["1", "2", "3", "4", "5"]),
        }
    )

    partition_range = partitions_def.get_partition_keys_in_range(
        dg.PartitionKeyRange(
            dg.MultiPartitionKey({"a": "2024-01-01", "b": "2"}),
            dg.MultiPartitionKey({"a": "2024-01-03", "b": "4"}),
        )
    )

    assert partition_range == [
        dg.MultiPartitionKey({"a": "2024-01-01", "b": "2"}),
        dg.MultiPartitionKey({"a": "2024-01-01", "b": "3"}),
        dg.MultiPartitionKey({"a": "2024-01-01", "b": "4"}),
        dg.MultiPartitionKey({"a": "2024-01-02", "b": "2"}),
        dg.MultiPartitionKey({"a": "2024-01-02", "b": "3"}),
        dg.MultiPartitionKey({"a": "2024-01-02", "b": "4"}),
        dg.MultiPartitionKey({"a": "2024-01-03", "b": "2"}),
        dg.MultiPartitionKey({"a": "2024-01-03", "b": "3"}),
        dg.MultiPartitionKey({"a": "2024-01-03", "b": "4"}),
    ]


def test_basic_pagination():
    """Test basic pagination works correctly."""
    dimension_a = dg.StaticPartitionsDefinition(["a1", "a2", "a3"])
    dimension_b = dg.StaticPartitionsDefinition(["b1", "b2"])

    multi_partitions = dg.MultiPartitionsDefinition({"dim_a": dimension_a, "dim_b": dimension_b})

    paginated_results = multi_partitions.get_paginated_partition_keys(
        context=PartitionLoadingContext(
            temporal_context=TemporalContext(effective_dt=datetime.now(), last_event_id=None),
            dynamic_partitions_store=None,
        ),
        limit=3,
        ascending=True,
        cursor=None,
    )

    assert len(paginated_results.results) == 3
    assert paginated_results.has_more

    expected_keys = [
        {"dim_a": "a1", "dim_b": "b1"},
        {"dim_a": "a1", "dim_b": "b2"},
        {"dim_a": "a2", "dim_b": "b1"},
    ]
    for i, key in enumerate(paginated_results.results):
        assert isinstance(key, dg.MultiPartitionKey)
        assert cast("dg.MultiPartitionKey", key).keys_by_dimension == expected_keys[i]

    paginated_results2 = multi_partitions.get_paginated_partition_keys(
        context=PartitionLoadingContext(
            temporal_context=TemporalContext(effective_dt=datetime.now(), last_event_id=None),
            dynamic_partitions_store=None,
        ),
        limit=3,
        ascending=True,
        cursor=paginated_results.cursor,
    )

    assert len(paginated_results2.results) == 3
    assert not paginated_results2.has_more

    expected_keys2 = [
        {"dim_a": "a2", "dim_b": "b2"},
        {"dim_a": "a3", "dim_b": "b1"},
        {"dim_a": "a3", "dim_b": "b2"},
    ]
    for i, key in enumerate(paginated_results2.results):
        assert isinstance(key, dg.MultiPartitionKey)
        assert cast("dg.MultiPartitionKey", key).keys_by_dimension == expected_keys2[i]


def test_reverse_pagination():
    """Test reverse pagination works correctly."""
    dimension_a = dg.StaticPartitionsDefinition(["a1", "a2", "a3"])
    dimension_b = dg.StaticPartitionsDefinition(["b1", "b2"])

    multi_partitions = dg.MultiPartitionsDefinition({"dim_a": dimension_a, "dim_b": dimension_b})

    paginated_results = multi_partitions.get_paginated_partition_keys(
        context=PartitionLoadingContext(
            temporal_context=TemporalContext(effective_dt=datetime.now(), last_event_id=None),
            dynamic_partitions_store=None,
        ),
        limit=3,
        ascending=False,
        cursor=None,
    )

    assert len(paginated_results.results) == 3
    assert paginated_results.has_more
    expected_keys = [
        {"dim_a": "a3", "dim_b": "b2"},
        {"dim_a": "a3", "dim_b": "b1"},
        {"dim_a": "a2", "dim_b": "b2"},
    ]
    for i, key in enumerate(paginated_results.results):
        assert isinstance(key, dg.MultiPartitionKey)
        assert cast("dg.MultiPartitionKey", key).keys_by_dimension == expected_keys[i]

    paginated_results2 = multi_partitions.get_paginated_partition_keys(
        context=PartitionLoadingContext(
            temporal_context=TemporalContext(effective_dt=datetime.now(), last_event_id=None),
            dynamic_partitions_store=None,
        ),
        limit=3,
        ascending=False,
        cursor=paginated_results.cursor,
    )

    assert len(paginated_results2.results) == 3
    assert not paginated_results2.has_more

    expected_keys2 = [
        {"dim_a": "a2", "dim_b": "b1"},
        {"dim_a": "a1", "dim_b": "b2"},
        {"dim_a": "a1", "dim_b": "b1"},
    ]
    for i, key in enumerate(paginated_results2.results):
        assert isinstance(key, dg.MultiPartitionKey)
        assert cast("dg.MultiPartitionKey", key).keys_by_dimension == expected_keys2[i]


def test_pagination_accumulation():
    """Test multiple pagination calls accumulate the full cross product."""
    dimension_a = dg.StaticPartitionsDefinition(["a1", "a2", "a3", "a4"])
    dimension_b = dg.StaticPartitionsDefinition(["b1", "b2", "b3", "b4", "b5"])

    multi_partitions = dg.MultiPartitionsDefinition({"dim_a": dimension_a, "dim_b": dimension_b})
    partition_context = PartitionLoadingContext(
        temporal_context=TemporalContext(effective_dt=datetime.now(), last_event_id=None),
        dynamic_partitions_store=None,
    )

    all_results = []
    cursor = None
    has_more = True

    # Paginate through all results
    while has_more:
        paginated_results = multi_partitions.get_paginated_partition_keys(
            context=partition_context,
            ascending=True,
            cursor=cursor,
            limit=4,
        )
        all_results.extend(paginated_results.results)
        cursor = paginated_results.cursor
        has_more = paginated_results.has_more

    assert len(all_results) == 20

    expected_combinations = []
    for a_val in ["a1", "a2", "a3", "a4"]:
        for b_val in ["b1", "b2", "b3", "b4", "b5"]:
            expected_combinations.append({"dim_a": a_val, "dim_b": b_val})

    result_dicts = [key.keys_by_dimension for key in all_results]
    for expected in expected_combinations:
        assert expected in result_dicts
    assert len(result_dicts) == len(expected_combinations)

    # paginate the reverse results
    reverse_results = []
    cursor = None
    has_more = True
    while has_more:
        paginated_results = multi_partitions.get_paginated_partition_keys(
            context=partition_context,
            ascending=False,
            cursor=cursor,
            limit=4,
        )
        reverse_results.extend(paginated_results.results)
        cursor = paginated_results.cursor
        has_more = paginated_results.has_more

    assert len(reverse_results) == 20
    assert reverse_results == [str(key) for key in reversed(all_results)]


def test_empty_dimension():
    """Test behavior when one dimension is empty."""
    dimension_a = dg.StaticPartitionsDefinition(["a1", "a2"])
    dimension_b = dg.StaticPartitionsDefinition([])  # Empty dimension

    multi_partitions = dg.MultiPartitionsDefinition({"dim_a": dimension_a, "dim_b": dimension_b})
    partition_context = PartitionLoadingContext(
        temporal_context=TemporalContext(effective_dt=datetime.now(), last_event_id=None),
        dynamic_partitions_store=None,
    )
    paginated_results = multi_partitions.get_paginated_partition_keys(
        context=partition_context,
        ascending=True,
        cursor=None,
        limit=10,
    )
    assert len(paginated_results.results) == 0
    assert not paginated_results.has_more


def test_large_cross_product_memory_usage():
    """Test memory efficiency with large dimensions."""
    large_dimension_a = dg.StaticPartitionsDefinition([f"a{i}" for i in range(1000)])
    large_dimension_b = dg.StaticPartitionsDefinition([f"b{i}" for i in range(1000)])

    multi_partitions = dg.MultiPartitionsDefinition(
        {"dim_a": large_dimension_a, "dim_b": large_dimension_b}
    )
    partition_context = PartitionLoadingContext(
        temporal_context=TemporalContext(effective_dt=datetime.now(), last_event_id=None),
        dynamic_partitions_store=None,
    )

    with (
        mock.patch("itertools.product") as mock_product,
        mock.patch.object(multi_partitions, "get_partition_keys") as mock_get_partition_keys,
    ):
        paginated_results = multi_partitions.get_paginated_partition_keys(
            context=partition_context,
            ascending=True,
            cursor=None,
            limit=10,
        )

        # Verify we got the right number of results
        assert len(paginated_results.results) == 10
        assert paginated_results.has_more
        mock_product.assert_not_called()
        mock_get_partition_keys.assert_not_called()


@pytest.mark.parametrize(
    "key, expected",
    [
        ("a|2", True),
        ("c|1", True),
        ("2|a", False),
        ("a|b", False),
        ("abc", False),
        ("super1@#^k-INVALID", False),
    ],
)
def test_has_partition_key(key: str, expected: bool) -> None:
    dim1 = dg.StaticPartitionsDefinition(["a", "b", "c"])
    dim2 = dg.StaticPartitionsDefinition(["1", "2", "3"])

    multi_partitions = dg.MultiPartitionsDefinition({"dim1": dim1, "dim2": dim2})
    assert multi_partitions.has_partition_key(key) == expected
