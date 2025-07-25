import os
import pickle
import shutil
import tempfile
from datetime import datetime
from typing import Optional

import dagster as dg
import pytest
from dagster import AssetsDefinition, DagsterInstance, MetadataValue, PartitionsDefinition
from dagster._core.definitions.assets.graph.asset_graph import AssetGraph
from dagster._core.definitions.partitions.mapping import UpstreamPartitionsResult
from dagster._core.definitions.partitions.subset import PartitionsSubset
from dagster._core.instance import DynamicPartitionsStore
from dagster._core.storage.fs_io_manager import fs_io_manager
from dagster._core.storage.io_manager import IOManagerDefinition
from dagster_shared import seven


def define_job(io_manager: IOManagerDefinition):
    @dg.op
    def op_a(_context):
        return [1, 2, 3]

    @dg.op
    def op_b(_context, _df):
        return 1

    @dg.job(resource_defs={"io_manager": io_manager})
    def asset_job():
        op_b(op_a())

    return asset_job


def test_fs_io_manager():
    with tempfile.TemporaryDirectory() as tmpdir_path:
        io_manager = fs_io_manager.configured({"base_dir": tmpdir_path})
        job_def = define_job(io_manager)

        result = job_def.execute_in_process()
        assert result.success

        handled_output_events = list(filter(lambda evt: evt.is_handled_output, result.all_events))
        assert len(handled_output_events) == 2

        filepath_a = os.path.join(tmpdir_path, result.run_id, "op_a", "result")
        metadata = handled_output_events[0].event_specific_data.metadata  # pyright: ignore[reportOptionalMemberAccess,reportAttributeAccessIssue]
        assert metadata["path"] == MetadataValue.path(filepath_a)
        assert os.path.isfile(filepath_a)
        with open(filepath_a, "rb") as read_obj:
            assert pickle.load(read_obj) == [1, 2, 3]

        loaded_input_events = list(filter(lambda evt: evt.is_loaded_input, result.all_events))
        metadata = loaded_input_events[0].event_specific_data.metadata  # pyright: ignore[reportOptionalMemberAccess,reportAttributeAccessIssue]
        assert len(loaded_input_events) == 1
        assert loaded_input_events[0].event_specific_data.upstream_step_key == "op_a"  # pyright: ignore[reportOptionalMemberAccess,reportAttributeAccessIssue]

        filepath_b = os.path.join(tmpdir_path, result.run_id, "op_b", "result")
        metadata = handled_output_events[1].event_specific_data.metadata  # pyright: ignore[reportOptionalMemberAccess,reportAttributeAccessIssue]
        assert metadata["path"] == MetadataValue.path(filepath_b)
        assert os.path.isfile(filepath_b)
        with open(filepath_b, "rb") as read_obj:
            assert pickle.load(read_obj) == 1


def test_fs_io_manager_base_dir():
    with tempfile.TemporaryDirectory() as tmpdir_path:
        instance = DagsterInstance.ephemeral(tempdir=tmpdir_path)
        io_manager = dg.fs_io_manager
        job_def = define_job(io_manager)

        result = job_def.execute_in_process(instance=instance)
        assert result.success
        assert result.output_for_node("op_a") == [1, 2, 3]

        with open(
            os.path.join(instance.storage_directory(), result.run_id, "op_a", "result"),
            "rb",
        ) as read_obj:
            assert pickle.load(read_obj) == [1, 2, 3]


# lamdba functions can't be pickled (pickle.PicklingError)
lam = lambda x: x * x


# don't run this test on python 3.12
@pytest.mark.skipif(
    seven.IS_PYTHON_3_12 or seven.IS_PYTHON_3_13,
    reason="Test fails consistently on Python 3.12 and Python 3.13, further investigation required.",
)
def test_fs_io_manager_unpicklable():
    @dg.op
    def unpicklable_local_func_output():
        # locally defined functions can't be pickled (AttributeError)
        def local_func():
            return 1

        return local_func

    @dg.op
    def unpicklable_lambda_output():
        return lam

    @dg.op
    def recursion_limit_output():
        # a will exceed the recursion limit of 1000 and can't be pickled (RecursionError)
        a = []
        for _ in range(2000):
            a = [a]
        return a

    @dg.op
    def op_b(_i):
        return 1

    @dg.graph
    def local_func_graph():
        op_b(unpicklable_local_func_output())

    @dg.graph
    def lambda_graph():
        op_b(unpicklable_lambda_output())

    @dg.graph
    def recursion_limit_graph():
        op_b(recursion_limit_output())

    with tempfile.TemporaryDirectory() as tmp_dir:
        with dg.instance_for_test(temp_dir=tmp_dir) as instance:
            io_manager = fs_io_manager.configured({"base_dir": tmp_dir})

            local_func_job = local_func_graph.to_job(resource_defs={"io_manager": io_manager})
            with pytest.raises(
                dg.DagsterInvariantViolationError, match=r"Object .* is not picklable. .*"
            ):
                local_func_job.execute_in_process(instance=instance)

            lambda_job = lambda_graph.to_job(resource_defs={"io_manager": io_manager})
            with pytest.raises(
                dg.DagsterInvariantViolationError, match=r"Object .* is not picklable. .*"
            ):
                lambda_job.execute_in_process(instance=instance)

            recursion_job = recursion_limit_graph.to_job(resource_defs={"io_manager": io_manager})
            with pytest.raises(
                dg.DagsterInvariantViolationError,
                match=r"Object .* exceeds recursion limit and is not picklable. .*",
            ):
                recursion_job.execute_in_process(instance=instance)


def get_assets_job(io_manager_def, partitions_def=None):
    asset1_key_prefix = ["one", "two", "three"]

    @dg.asset(key_prefix=["one", "two", "three"], partitions_def=partitions_def)
    def asset1():
        return [1, 2, 3]

    @dg.asset(
        key_prefix=["four", "five"],
        ins={"asset1": dg.AssetIn(key_prefix=asset1_key_prefix)},
        partitions_def=partitions_def,
    )
    def asset2(asset1):
        return asset1 + [4]

    return dg.Definitions(
        assets=[asset1, asset2],
        resources={"io_manager": io_manager_def},
    ).resolve_implicit_job_def_def_for_assets([asset1.key, asset2.key])


def test_fs_io_manager_handles_assets():
    with tempfile.TemporaryDirectory() as tmpdir_path:
        io_manager_def = fs_io_manager.configured({"base_dir": tmpdir_path})
        job_def = get_assets_job(io_manager_def)

        result = job_def.execute_in_process()  # pyright: ignore[reportOptionalMemberAccess]
        assert result.success

        handled_output_events = list(
            filter(lambda evt: evt.is_handled_output, result.all_node_events)
        )
        assert len(handled_output_events) == 2

        filepath_a = os.path.join(tmpdir_path, "one", "two", "three", "asset1")
        assert os.path.isfile(filepath_a)
        with open(filepath_a, "rb") as read_obj:
            assert pickle.load(read_obj) == [1, 2, 3]

        loaded_input_events = list(filter(lambda evt: evt.is_loaded_input, result.all_node_events))
        assert len(loaded_input_events) == 1
        assert loaded_input_events[0].event_specific_data.upstream_step_key.endswith("asset1")  # pyright: ignore[reportOptionalMemberAccess,reportAttributeAccessIssue]

        filepath_b = os.path.join(tmpdir_path, "four", "five", "asset2")
        assert os.path.isfile(filepath_b)
        with open(filepath_b, "rb") as read_obj:
            assert pickle.load(read_obj) == [1, 2, 3, 4]


def test_fs_io_manager_partitioned():
    with tempfile.TemporaryDirectory() as tmpdir_path:
        io_manager_def = fs_io_manager.configured({"base_dir": tmpdir_path})
        job_def = get_assets_job(
            io_manager_def,
            partitions_def=dg.DailyPartitionsDefinition(start_date="2020-02-01"),
        )

        result = job_def.execute_in_process(partition_key="2020-05-03")  # pyright: ignore[reportOptionalMemberAccess]
        assert result.success

        handled_output_events = list(
            filter(lambda evt: evt.is_handled_output, result.all_node_events)
        )
        assert len(handled_output_events) == 2

        filepath_a = os.path.join(tmpdir_path, "one", "two", "three", "asset1", "2020-05-03")
        assert os.path.isfile(filepath_a)
        with open(filepath_a, "rb") as read_obj:
            assert pickle.load(read_obj) == [1, 2, 3]

        loaded_input_events = list(filter(lambda evt: evt.is_loaded_input, result.all_node_events))
        assert len(loaded_input_events) == 1
        assert loaded_input_events[0].event_specific_data.upstream_step_key.endswith("asset1")  # pyright: ignore[reportOptionalMemberAccess,reportAttributeAccessIssue]

        filepath_b = os.path.join(tmpdir_path, "four", "five", "asset2", "2020-05-03")
        assert os.path.isfile(filepath_b)
        with open(filepath_b, "rb") as read_obj:
            assert pickle.load(read_obj) == [1, 2, 3, 4]


def test_fs_io_manager_partitioned_no_partitions():
    with tempfile.TemporaryDirectory() as tmpdir_path:
        io_manager_def = fs_io_manager.configured({"base_dir": tmpdir_path})

        class NoPartitionsPartitionMapping(dg.PartitionMapping):
            def get_upstream_mapped_partitions_result_for_partitions(
                self,
                downstream_partitions_subset: Optional[PartitionsSubset],
                downstream_partitions_def: Optional[dg.PartitionsDefinition],
                upstream_partitions_def: PartitionsDefinition,
                current_time: Optional[datetime] = None,
                dynamic_partitions_store: Optional[DynamicPartitionsStore] = None,
            ) -> UpstreamPartitionsResult:
                return UpstreamPartitionsResult(
                    partitions_subset=upstream_partitions_def.empty_subset(),
                    required_but_nonexistent_subset=upstream_partitions_def.empty_subset(),
                )

            def validate_partition_mapping(
                self,
                upstream_partitions_def: PartitionsDefinition,
                downstream_partitions_def: Optional[dg.PartitionsDefinition],
            ):
                pass

            def get_downstream_partitions_for_partitions(
                self,
                upstream_partitions_subset,
                upstream_partitions_def,
                downstream_partitions_def,
                current_time: Optional[datetime] = None,
                dynamic_partitions_store: Optional[DynamicPartitionsStore] = None,
            ):
                raise NotImplementedError()

            @property
            def description(self) -> str:
                raise NotImplementedError()

        partitions_def = dg.DailyPartitionsDefinition(start_date="2020-02-01")

        @dg.asset(partitions_def=partitions_def)
        def asset1(): ...

        @dg.asset(
            partitions_def=partitions_def,
            ins={"asset1": dg.AssetIn(partition_mapping=NoPartitionsPartitionMapping())},
        )
        def asset2(asset1):
            assert asset1 is None

        assert dg.materialize(
            [asset1.to_source_assets()[0], asset2],
            partition_key="2020-02-01",
            resources={"io_manager": io_manager_def},
        ).success


def test_fs_io_manager_partitioned_multi_asset():
    with tempfile.TemporaryDirectory() as tmpdir_path:
        io_manager_def = fs_io_manager.configured({"base_dir": tmpdir_path})

        partitions = dg.StaticPartitionsDefinition(["A"])

        @dg.multi_asset(
            partitions_def=partitions,
            outs={
                "out_1": dg.AssetOut(key=dg.AssetKey("upstream_asset_1")),
                "out_2": dg.AssetOut(key=dg.AssetKey("upstream_asset_2")),
            },
        )
        def upstream_asset() -> tuple[dg.Output[int], dg.Output[int]]:
            return (dg.Output(1, output_name="out_1"), dg.Output(2, output_name="out_2"))

        @dg.asset(
            partitions_def=partitions,
        )
        def downstream_asset(upstream_asset_1: int) -> int:
            del upstream_asset_1
            return 2

        foo_job = dg.Definitions(
            assets=[upstream_asset, downstream_asset],
            resources={"io_manager": io_manager_def},
            jobs=[dg.define_asset_job("TheJob")],
        ).resolve_job_def("TheJob")

        result = foo_job.execute_in_process(partition_key="A")
        assert result.success

        handled_output_events = list(
            filter(lambda evt: evt.is_handled_output, result.all_node_events)
        )
        assert len(handled_output_events) == 3


def test_fs_io_manager_partitioned_graph_backed_asset():
    with tempfile.TemporaryDirectory() as tmpdir_path:
        io_manager_def = fs_io_manager.configured({"base_dir": tmpdir_path})
        partitions_def = dg.StaticPartitionsDefinition(["A"])

        @dg.asset(key_prefix=["the", "cool", "prefix"], partitions_def=partitions_def)
        def one():
            return 1

        @dg.op
        def add_1(inp):
            return inp + 1

        @dg.graph
        def four(inp):
            return add_1(add_1(add_1(inp)))

        four_asset = AssetsDefinition.from_graph(
            four,
            keys_by_input_name={"inp": dg.AssetKey(["the", "cool", "prefix", "one"])},
            partitions_def=partitions_def,
        )

        result = dg.materialize(
            assets=[one, four_asset],
            resources={"io_manager": io_manager_def},
            partition_key="A",
        )

        assert result.success

        handled_output_events = list(
            filter(lambda evt: evt.is_handled_output, result.all_node_events)
        )
        assert len(handled_output_events) == 4

        filepath_a = os.path.join(tmpdir_path, "the", "cool", "prefix", "one", "A")
        assert os.path.isfile(filepath_a)
        with open(filepath_a, "rb") as read_obj:
            assert pickle.load(read_obj) == 1

        loaded_input_events = list(filter(lambda evt: evt.is_loaded_input, result.all_node_events))
        assert len(loaded_input_events) == 3
        assert loaded_input_events[0].event_specific_data.upstream_step_key.endswith("one")  # pyright: ignore[reportOptionalMemberAccess,reportAttributeAccessIssue]

        filepath_b = os.path.join(tmpdir_path, "four", "A")
        assert os.path.isfile(filepath_b)
        with open(filepath_b, "rb") as read_obj:
            assert pickle.load(read_obj) == 4


def test_fs_io_manager_partitioned_self_dep():
    with tempfile.TemporaryDirectory() as tmpdir_path:
        io_manager_def = fs_io_manager.configured({"base_dir": tmpdir_path})

        @dg.asset(
            partitions_def=dg.DailyPartitionsDefinition(start_date="2020-01-01"),
            ins={
                "a": dg.AssetIn(
                    partition_mapping=dg.TimeWindowPartitionMapping(start_offset=-1, end_offset=-1)
                )
            },
        )
        def a(a: Optional[int]) -> int:
            return 1 if a is None else a + 1

        result = dg.materialize(
            [a], partition_key="2020-01-01", resources={"io_manager": io_manager_def}
        )
        assert result.success
        assert result.output_for_node("a") == 1

        result2 = dg.materialize(
            [a], partition_key="2020-01-02", resources={"io_manager": io_manager_def}
        )
        assert result2.success
        assert result2.output_for_node("a") == 2


def test_fs_io_manager_none():
    with tempfile.TemporaryDirectory() as tmpdir_path:
        io_manager_def = fs_io_manager.configured({"base_dir": tmpdir_path})

        @dg.asset
        def asset1() -> None:
            pass

        @dg.asset(deps=[asset1])
        def asset2() -> None:
            pass

        result = dg.materialize(
            dg.with_resources([asset1, asset2], resource_defs={"io_manager": io_manager_def})
        )

        assert not os.path.exists(os.path.join(tmpdir_path, "asset1"))
        assert not os.path.exists(os.path.join(tmpdir_path, "asset2"))
        handled_output_events = list(
            filter(lambda evt: evt.is_handled_output, result.all_node_events)
        )
        assert len(handled_output_events) == 0


def test_fs_io_manager_ops_none():
    with tempfile.TemporaryDirectory() as tmpdir_path:
        io_manager_def = fs_io_manager.configured({"base_dir": tmpdir_path})

        @dg.op
        def op1() -> None:
            pass

        @dg.op(ins={"abc": dg.In(dg.Nothing)})
        def op2() -> None:
            pass

        @dg.job(resource_defs={"io_manager": io_manager_def})
        def job1():
            op2(op1())

        result = job1.execute_in_process()

        handled_output_events = list(
            filter(lambda evt: evt.is_handled_output, result.all_node_events)
        )
        assert len(handled_output_events) == 0


def test_fs_io_manager_none_value_no_metadata():
    with tempfile.TemporaryDirectory() as tmpdir_path:
        io_manager_def = fs_io_manager.configured({"base_dir": tmpdir_path})

        @dg.asset
        def asset1():
            pass

        result = dg.materialize(
            dg.with_resources([asset1], resource_defs={"io_manager": io_manager_def})
        )

        assert os.path.exists(os.path.join(tmpdir_path, "asset1"))
        handled_output_events = list(
            filter(lambda evt: evt.is_handled_output, result.all_node_events)
        )
        assert len(handled_output_events) == 1
        metadata = handled_output_events[0].event_specific_data.metadata  # pyright: ignore[reportOptionalMemberAccess,reportAttributeAccessIssue]
        assert "path" not in metadata


def test_multipartitions_fs_io_manager():
    with tempfile.TemporaryDirectory() as tmpdir_path:
        io_manager_def = fs_io_manager.configured({"base_dir": tmpdir_path})
        multipartitions_def = dg.MultiPartitionsDefinition(
            {
                "a": dg.StaticPartitionsDefinition(["a", "b"]),
                "1": dg.StaticPartitionsDefinition(["1", "2"]),
            }
        )

        @dg.asset(
            partitions_def=multipartitions_def,
            io_manager_def=io_manager_def,
        )
        def asset1():
            return 1

        @dg.asset(io_manager_def=io_manager_def, partitions_def=multipartitions_def)
        def asset2(asset1):
            return asset1

        my_job = dg.define_asset_job("my_job", [asset1, asset2]).resolve(
            asset_graph=AssetGraph.from_assets([asset1, asset2])
        )

        result = my_job.execute_in_process(partition_key=dg.MultiPartitionKey({"a": "a", "1": "1"}))

        handled_output_events = list(
            filter(lambda evt: evt.is_handled_output, result.all_node_events)
        )
        assert len(handled_output_events) == 2


def test_backcompat_multipartitions_fs_io_manager():
    src_dir = dg.file_relative_path(
        __file__, "backcompat_multipartitions_fs_io_manager/backcompat_materialization"
    )
    with tempfile.TemporaryDirectory() as test_dir:
        os.mkdir(os.path.join(test_dir, "multipartitioned"))

        io_manager_def = fs_io_manager.configured({"base_dir": test_dir})
        dest_file_path = os.path.join(test_dir, "multipartitioned", "c|2020-04-22")
        shutil.copyfile(src_dir, dest_file_path)

        composite = dg.MultiPartitionsDefinition(
            {
                "abc": dg.StaticPartitionsDefinition(["a", "b", "c", "d", "e", "f"]),
                "date": dg.DailyPartitionsDefinition(start_date="2020-01-01"),
            }
        )

        @dg.asset(
            partitions_def=composite,
            io_manager_def=io_manager_def,
        )
        def multipartitioned(context):
            return 1

        @dg.asset(
            partitions_def=composite,
            io_manager_def=io_manager_def,
        )
        def downstream_of_multipartitioned(multipartitioned):
            return 1

        # Upstream partition was never materialized, so this run should error
        # the error will have the old backcompat path mentioned, because the UPathIOManager will first try to use the normal path, catch the error, and then try to load from the backcompat path, which will cause the actual raised error
        with pytest.raises(FileNotFoundError, match="c/2020-04-21"):
            my_job = dg.define_asset_job(
                "my_job", [multipartitioned, downstream_of_multipartitioned]
            ).resolve(
                asset_graph=AssetGraph.from_assets(
                    [multipartitioned, downstream_of_multipartitioned]
                )
            )
            result = my_job.execute_in_process(
                partition_key=dg.MultiPartitionKey({"abc": "c", "date": "2020-04-21"}),
                asset_selection=[dg.AssetKey("downstream_of_multipartitioned")],
            )

        my_job = dg.define_asset_job(
            "my_job", [multipartitioned, downstream_of_multipartitioned]
        ).resolve(
            asset_graph=AssetGraph.from_assets([multipartitioned, downstream_of_multipartitioned])
        )
        result = my_job.execute_in_process(
            partition_key=dg.MultiPartitionKey({"abc": "c", "date": "2020-04-22"}),
            asset_selection=[dg.AssetKey("downstream_of_multipartitioned")],
        )
        assert result.success

        result = my_job.execute_in_process(
            partition_key=dg.MultiPartitionKey({"abc": "c", "date": "2020-04-22"}),
        )
        assert result.success
        materializations = result.asset_materializations_for_node("multipartitioned")
        assert len(materializations) == 1

        get_path_metadata_entry = lambda materialization: materialization.metadata["path"]
        assert "c/2020-04-22" in get_path_metadata_entry(materializations[0]).path

        materializations = result.asset_materializations_for_node("downstream_of_multipartitioned")
        assert len(materializations) == 1
        assert "c/2020-04-22" in get_path_metadata_entry(materializations[0]).path
