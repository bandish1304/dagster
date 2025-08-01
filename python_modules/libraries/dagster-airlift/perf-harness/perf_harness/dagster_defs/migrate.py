from dagster import AssetsDefinition, Definitions
from dagster._core.definitions.assets.definition.asset_spec import AssetSpec
from dagster._core.definitions.decorators.asset_decorator import multi_asset
from dagster_airlift.core import (
    AirflowBasicAuthBackend,
    AirflowInstance,
    build_defs_from_airflow_instance,
)
from dagster_airlift.core.top_level_dag_def_api import assets_with_task_mappings

from perf_harness.dagster_defs.constants import (
    AIRFLOW_BASE_URL,
    AIRFLOW_INSTANCE_NAME,
    PASSWORD,
    USERNAME,
)
from perf_harness.shared.constants import get_num_assets, get_num_dags, get_num_tasks

airflow_instance = AirflowInstance(
    auth_backend=AirflowBasicAuthBackend(
        webserver_url=AIRFLOW_BASE_URL, username=USERNAME, password=PASSWORD
    ),
    name=AIRFLOW_INSTANCE_NAME,
)


def build_asset(specs: list[AssetSpec]) -> AssetsDefinition:
    @multi_asset(specs=specs)
    def asset_fn(_):
        pass

    return asset_fn


def build_asset_for_task(task_name: str, prev_asset_specs: list[AssetSpec]) -> AssetsDefinition:
    specs = [
        # Create a bunch of dependencies for each asset.
        AssetSpec(f"{task_name}_asset_{i}", deps=[spec.key for spec in prev_asset_specs])
        for i in range(get_num_assets())
    ]
    return build_asset(specs)


def get_dag_defs() -> Definitions:
    all_dag_defs = []
    prev_asset_specs: list[AssetSpec] = []
    for i in range(get_num_dags()):
        dag_id = f"dag_{i}"
        task_mappings = {}
        for j in range(get_num_tasks()):
            task_name = f"task_{i}_{j}"
            asset = build_asset_for_task(task_name, prev_asset_specs)
            task_mappings[task_name] = [asset]
            prev_asset_specs = asset.specs  # type: ignore
        all_dag_defs.append(
            Definitions(
                assets=assets_with_task_mappings(
                    dag_id=dag_id,
                    task_mappings=task_mappings,
                )
            )
        )
    return Definitions.merge(*all_dag_defs)


defs = build_defs_from_airflow_instance(airflow_instance=airflow_instance, defs=get_dag_defs())
