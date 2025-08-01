from pathlib import Path

from setuptools import find_packages, setup


def get_version() -> str:
    version: dict[str, str] = {}
    with open(Path(__file__).parent / "dagster_fivetran/version.py", encoding="utf8") as fp:
        exec(fp.read(), version)

    return version["__version__"]


ver = get_version()
# dont pin dev installs to avoid pip dep resolver issues
pin = "" if ver == "1!0+dev" else f"=={ver}"
setup(
    name="dagster-fivetran",
    version=ver,
    author="Dagster Labs",
    author_email="hello@dagsterlabs.com",
    license="Apache-2.0",
    description="Package for integrating Fivetran with Dagster.",
    url="https://github.com/dagster-io/dagster/tree/master/python_modules/libraries/dagster-fivetran",
    classifiers=[
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Programming Language :: Python :: 3.13",
        "License :: OSI Approved :: Apache Software License",
        "Operating System :: OS Independent",
    ],
    packages=find_packages(exclude=["dagster_fivetran_tests*"]),
    include_package_data=True,
    python_requires=">=3.9,<3.14",
    install_requires=[f"dagster{pin}"],
    zip_safe=False,
    entry_points={
        "console_scripts": [
            "dagster-fivetran = dagster_fivetran.cli:main",
        ],
        "dagster_dg_cli.registry_modules": [
            "dagster_fivetran = dagster_fivetran",
        ],
    },
    extras_require={
        "managed": [
            f"dagster-managed-elements{pin}",
        ],
        "test": [
            "pytest-order",
        ],
    },
)
