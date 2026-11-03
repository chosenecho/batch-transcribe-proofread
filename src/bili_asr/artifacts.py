"""Shared vocabulary for complete transcript bundles."""

from collections.abc import Mapping
from pathlib import PurePosixPath
from types import MappingProxyType
from bili_asr.contracts.registry import BUNDLE_SCHEMA, BUNDLE_SCHEMA_V3

REQUIRED_ARTIFACT_KEYS = ("srt_path", "vtt_path", "txt_path", "md_path", "raw_path")
SOURCE_METADATA_KEY = "source_metadata_path"
V3_ARTIFACT_KEYS = (*REQUIRED_ARTIFACT_KEYS, SOURCE_METADATA_KEY)

# A v2 bundle always carries WebVTT; four-product bundles need republishing.

BUNDLE_MARKER_NAME = ".bundle-ready"
BUNDLE_BASENAMES = MappingProxyType({
    "srt_path": "bundle.srt", "vtt_path": "bundle.vtt", "txt_path": "bundle.txt",
    "md_path": "bundle.md", "raw_path": "bundle.raw.json",
})
BUNDLE_BASENAMES_V3 = MappingProxyType({
    **BUNDLE_BASENAMES, SOURCE_METADATA_KEY: "source-metadata.json",
})


def owns_bundle_paths(paths: Mapping[str, str]) -> bool:
    """Require five canonical paths in exactly one transcripts/<identity>/ directory."""
    if set(paths) != set(REQUIRED_ARTIFACT_KEYS):
        return False
    directories = set()
    for key, basename in BUNDLE_BASENAMES.items():
        value = paths[key]
        if not isinstance(value, str) or "\\" in value:
            return False
        parts = tuple(value.split("/"))
        if (len(parts) != 3 or parts[0] != "transcripts" or parts[1] in {"", ".", ".."}
                or parts[2] != basename or PurePosixPath(value).as_posix() != value):
            return False
        directories.add(parts[:2])
    return len(directories) == 1


def bundle_roles(schema: str) -> tuple[str, ...]:
    if schema == BUNDLE_SCHEMA:
        return REQUIRED_ARTIFACT_KEYS
    if schema == BUNDLE_SCHEMA_V3:
        return V3_ARTIFACT_KEYS
    raise ValueError("unsupported archive bundle contract")


def bundle_paths_for_schema(paths: Mapping[str, str], schema: str) -> dict[str, str]:
    roles = bundle_roles(schema)
    if not owns_bundle_paths(paths):
        raise ValueError("invalid archive bundle paths")
    result = {key: paths[key] for key in REQUIRED_ARTIFACT_KEYS}
    if schema == BUNDLE_SCHEMA_V3:
        directory = next(iter({PurePosixPath(value).parent.as_posix() for value in result.values()}))
        result[SOURCE_METADATA_KEY] = f"{directory}/{BUNDLE_BASENAMES_V3[SOURCE_METADATA_KEY]}"
    return {key: result[key] for key in roles}
