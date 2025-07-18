from typing import cast

import dagster as dg
from dagster._core.definitions.metadata import CodeReferencesMetadataSet


def test_source_metadata_set() -> None:
    source_metadata = CodeReferencesMetadataSet(
        code_references=dg.CodeReferencesMetadataValue(
            code_references=[
                dg.LocalFileCodeReference(
                    file_path="/Users/dagster/Documents/my_module/assets/my_asset.py",
                    line_number=12,
                    label="python src",
                )
            ]
        )
    )

    dict_source_metadata = dict(source_metadata)
    assert dict_source_metadata == {"dagster/code_references": source_metadata.code_references}
    source_data = cast(
        "dg.CodeReferencesMetadataValue", dict_source_metadata["dagster/code_references"]
    )
    assert len(source_data.code_references) == 1
    assert isinstance(
        source_data.code_references[0],
        dg.LocalFileCodeReference,
    )
    dg.AssetMaterialization(asset_key="a", metadata=dict_source_metadata)

    splat_source_metadata = {**source_metadata}
    assert splat_source_metadata == {"dagster/code_references": source_metadata.code_references}
    source_data = cast(
        "dg.CodeReferencesMetadataValue", splat_source_metadata["dagster/code_references"]
    )
    assert len(source_data.code_references) == 1
    assert isinstance(
        source_data.code_references[0],
        dg.LocalFileCodeReference,
    )
    dg.AssetMaterialization(asset_key="a", metadata=splat_source_metadata)

    assert dict(
        CodeReferencesMetadataSet(
            code_references=dg.CodeReferencesMetadataValue(code_references=[])
        )
    ) == {"dagster/code_references": dg.CodeReferencesMetadataValue(code_references=[])}
    assert CodeReferencesMetadataSet.extract(
        dict(
            CodeReferencesMetadataSet(
                code_references=dg.CodeReferencesMetadataValue(code_references=[])
            )
        )
    ) == CodeReferencesMetadataSet(
        code_references=dg.CodeReferencesMetadataValue(code_references=[])
    )
