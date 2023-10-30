from pathlib import Path
from types import MappingProxyType
from typing import Dict, FrozenSet, List, NamedTuple, Optional

import networkx as nx
import pydantic
from intervaltree import IntervalTree
from pydantic import BaseModel, Extra

from wake.compiler.solc_frontend import SolcInputSettings, SolcOutputError
from wake.core.solidity_version import SolidityVersion
from wake.ir import SourceUnit
from wake.ir.reference_resolver import ReferenceResolver


class BuildInfoModel(BaseModel):
    class Config:
        extra = Extra.allow
        allow_mutation = False
        arbitrary_types_allowed = True
        json_encoders = {SolidityVersion: str, bytes: lambda b: b.hex()}


class CompilationUnitBuildInfo(BuildInfoModel):
    errors: List[SolcOutputError]


# workaround for pydantic bytes JSON encode bug: https://github.com/pydantic/pydantic/issues/3756
class HexBytes(bytes):
    @classmethod
    def __get_validators__(cls):
        yield cls.validate

    @classmethod
    def validate(cls, v):
        if isinstance(v, bytes):
            return v
        elif isinstance(v, bytearray):
            return bytes(v)
        elif isinstance(v, str):
            return bytes.fromhex(v)
        raise pydantic.errors.BytesError()


class SourceUnitInfo(NamedTuple):
    fs_path: Path
    blake2b_hash: HexBytes


class ProjectBuildInfo(BuildInfoModel):
    compilation_units: Dict[str, CompilationUnitBuildInfo]
    source_units_info: Dict[str, SourceUnitInfo]
    allow_paths: FrozenSet[Path]
    exclude_paths: FrozenSet[Path]
    include_paths: FrozenSet[Path]
    settings: SolcInputSettings
    target_solidity_version: Optional[SolidityVersion]
    wake_version: str
    incremental: bool


class ProjectBuild:
    _interval_trees: Dict[Path, IntervalTree]
    _reference_resolver: ReferenceResolver
    _source_units: Dict[Path, SourceUnit]

    def __init__(
        self,
        interval_trees: Dict[Path, IntervalTree],
        reference_resolver: ReferenceResolver,
        source_units: Dict[Path, SourceUnit],
    ):
        self._interval_trees = interval_trees
        self._reference_resolver = reference_resolver
        self._source_units = source_units

    @property
    def interval_trees(self) -> Dict[Path, IntervalTree]:
        return MappingProxyType(
            self._interval_trees
        )  # pyright: ignore reportGeneralTypeIssues

    @property
    def reference_resolver(self) -> ReferenceResolver:
        return self._reference_resolver

    @property
    def source_units(self) -> Dict[Path, SourceUnit]:
        return MappingProxyType(
            self._source_units
        )  # pyright: ignore reportGeneralTypeIssues