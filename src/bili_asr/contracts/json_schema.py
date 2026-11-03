"""Packaged JSON Schema validation; references resolve only from local resources."""
from __future__ import annotations

import json
from functools import lru_cache
from importlib import resources
from typing import Any

from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from bili_asr.contracts.registry import CONTRACTS, SCHEMA_BASE_URI, contract


class ContractValidationError(ValueError):
    """A value violates a registered structural contract."""


def load_schema(identity: str) -> dict[str, Any]:
    entry = contract(identity, capability="validate")
    if entry.schema is None:
        raise ValueError(f"contract has no JSON Schema: {identity}")
    schema: dict[str, Any] = json.loads(
        resources.files("bili_asr.contracts").joinpath("schemas", entry.schema).read_text(encoding="utf-8")
    )
    return schema


@lru_cache(maxsize=1)
def schema_registry() -> Registry:
    entries = []
    for entry in CONTRACTS.values():
        if entry.schema:
            schema = load_schema(entry.identity)
            Draft202012Validator.check_schema(schema)
            resource = Resource.from_contents(schema, default_specification=DRAFT202012)
            entries.append((SCHEMA_BASE_URI + entry.schema, resource))
            if "$id" in schema:
                entries.append((schema["$id"], resource))
    return Registry().with_resources(entries)


@lru_cache(maxsize=None)
def _validator(identity: str) -> Draft202012Validator:
    entry = contract(identity, capability="validate")
    if entry.schema is None:
        raise ValueError(f"contract has no JSON Schema: {identity}")
    schema = load_schema(identity)
    # Older standalone schemas have no $id. Set a base only in the in-memory
    # validation copy; published bytes and existing content identities stay fixed.
    schema.setdefault("$id", SCHEMA_BASE_URI + entry.schema)
    return Draft202012Validator(schema, registry=schema_registry())


def validate_json(identity: str, value: object) -> None:
    error = next(_validator(identity).iter_errors(value), None)
    if error is not None:
        # Do not expose private manuscript text or rejected field values.
        path = "/".join(str(part) for part in error.absolute_path) or "<root>"
        raise ContractValidationError(f"contract {identity}: invalid {path} ({error.validator})")
