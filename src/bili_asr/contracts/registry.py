"""Stable contract identities and ownership; no storage or runtime imports."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from types import MappingProxyType
from typing import Any

from bili_asr.contracts.identities import (
    BILIBILI_V1 as BILIBILI_V1,
    UNIVERSAL_V2 as UNIVERSAL_V2,
    RUNTIME_CONTRACTS as RUNTIME_CONTRACTS,
    WORKFLOW_PAYLOAD_VERSION as WORKFLOW_PAYLOAD_VERSION,
    ASR_PROFILE_VERSION as ASR_PROFILE_VERSION,
    ASR_EVIDENCE_VERSION as ASR_EVIDENCE_VERSION,
    BUNDLE_SCHEMA as BUNDLE_SCHEMA,
    BUNDLE_SCHEMA_V3 as BUNDLE_SCHEMA_V3,
    ORIGIN_PROFILE as ORIGIN_PROFILE,
    ORIGIN_PROFILE_V2 as ORIGIN_PROFILE_V2,
    DESCRIPTION_SUPPLEMENT_POLICY as DESCRIPTION_SUPPLEMENT_POLICY,
    IMPORT_EXTENSION as IMPORT_EXTENSION,
    LEGACY_FACTS_POLICY as LEGACY_FACTS_POLICY,
    STORED_VTT_POLICY as STORED_VTT_POLICY,
    SOURCE_SUPPLEMENT_POLICY as SOURCE_SUPPLEMENT_POLICY,
    ARTIFACT_STORAGE as ARTIFACT_STORAGE,
    ARTIFACT_ONLINE as ARTIFACT_ONLINE,
    SCHEMA_BASE_URI as SCHEMA_BASE_URI,
)


@dataclass(frozen=True)
class Contract:
    identity: str
    owner: str
    authority: tuple[str, ...]
    consumers: tuple[str, ...]
    capabilities: tuple[str, ...]
    dependencies: tuple[str, ...] = ()
    schema: str | None = None


def _json(identity: str, filename: str, owner: str = "publication",
          dependencies: tuple[str, ...] = ()) -> Contract:
    return Contract(identity, owner, (f"src/bili_asr/contracts/schemas/{filename}",),
                    ("export_snapshot", "external-reader"), ("validate", "export"), dependencies, filename)


_CONTRACTS = (
    Contract("workflow-batches/v1", "workflow", ("src/bili_asr/storage/workflow_batches.py", "src/bili_asr/storage/schema-workflow-batches.sql"),
             ("storage.workflow", "storage.snapshots", "cli.workflow"), ("extension", "snapshot", "claim-filter")),
    Contract("publication-packages/v1", "publication", ("src/bili_asr/storage/publication_packages.py", "src/bili_asr/storage/schema-publication-packages.sql"),
             ("publication", "publication_export", "storage.snapshots"), ("extension", "snapshot")),
    Contract("archive-task-state-backup/v2", "archive", ("src/bili_asr/services/maintenance_task_state.py",
             "src/bili_asr/services/maintenance_media.py"),
             ("services.maintenance_state_reader", "services.maintenance_backup"), ("validate", "save", "read"),
             ("archive-task-state-backup/v1", "artifact-storage-v1", "artifact-package-v2", "ssh-directory/v1"),
             "archive-task-state-backup-v2.schema.json"),
    Contract("artifact-policy-v2", "storage", ("src/bili_asr/services/audio_retention_policy.py",),
             ("cli.artifacts", "services.artifact_consumer"), ("validate", "read", "write"),
             ("artifact-online-v1", "artifact-package-v2"), "artifact-policy-v2.schema.json"),
    Contract("artifact-package-v2", "storage", ("src/bili_asr/artifact_packages.py",),
             ("services.artifact_transfer", "services.artifact_access", "services.artifact_restore"),
             ("validate", "read", "write"), ("artifact-storage-v1",), "artifact-package-v2.schema.json"),
    Contract("artifact-provenance/v1", "provenance", ("src/bili_asr/provenance_contract.py",
             "src/bili_asr/provenance_metadata.py"),
             ("cli.provenance", "publication_export", "external-reader"), ("read", "validate", "export"),
             ("frozen-input/v1", "frozen-input/v2", "asr-evidence/v1", "metadata-acceptance/v1",
              "source-observations/v1"), "artifact-provenance-v1.schema.json"),
    _json("public-provenance-summary/v1", "public-provenance-summary-v1.schema.json", "provenance"),
    _json("editorial-provenance-export-manifest/v1", "editorial-provenance-export-manifest-v1.schema.json", "provenance",
          dependencies=("artifact-provenance/v1", "editorial-export-manifest/v1")),
    Contract("retry-control/v2", "workflow", ("src/bili_asr/services/retry_control.py",),
             ("services.reference_backup", "services.workflow_retry", "services.retry_preflight", "services.maintenance_task_state"),
             ("validate", "capture", "read"), ("workflow-retry-control/v1",), "retry-control-v2.schema.json"),
    Contract("archive-reference-backup/v2", "archive", ("src/bili_asr/services/reference_backup.py",),
             ("cli.remote_storage", "services.archive_recovery"), ("validate", "save", "check", "plan", "restore"),
             ("archive-reference-backup/v1", "retry-control/v2"), "archive-reference-backup-v2.schema.json"),
    Contract("metadata-acceptance/v1", "sources", ("src/bili_asr/metadata_acceptance.py",
             "src/bili_asr/sources/bilibili_metadata_codec.py", "src/bili_asr/storage/metadata_acceptance.py"),
             ("cli.local_input", "storage.metadata", "storage.snapshots", "provenance-reader"),
             ("validate", "accept", "read", "snapshot"), (UNIVERSAL_V2, "source-observations/v1")),
    Contract("source-observations/v1", "sources", ("src/bili_asr/source_observations.py",
             "src/bili_asr/storage/schema-source-observations.sql"),
             ("storage.source_observations", "storage.snapshots", "cli.local_input"),
             ("extension", "snapshot", "read", "explicit-upgrade")),
    Contract("source-superset/v1", "sources", ("src/bili_asr/source_observations.py",),
             ("storage.source_observations", "cli.local_input"),
             ("read", "reproject", "private-export"), ("source-observations/v1",)),
    Contract("source-superset/v2", "sources", ("src/bili_asr/source_observations.py",),
             ("storage.source_observations", "cli.local_input"),
             ("read", "reproject", "private-export"), ("source-observations/v1",)),
    Contract("archive-task-state-backup/v1", "archive", ("src/bili_asr/services/maintenance_task_state.py",),
             ("services.maintenance_state_reader", "services.maintenance_backup"), ("validate", "save", "read"),
             (BILIBILI_V1, UNIVERSAL_V2), "archive-task-state-backup-v1.schema.json"),
    Contract("archive-maintenance-package/v2", "archive", ("src/bili_asr/services/maintenance_package_core.py", "src/bili_asr/storage/maintenance_scope.py"),
             ("cli.maintenance_package", "services.maintenance_import"), ("validate", "export", "inspect", "verify", "import-plan", "import-apply", "import-check"),
             ("archive-maintenance-package/v1",), "archive-maintenance-package-v2.schema.json"),
    Contract("archive-maintenance-package/v1", "archive", ("src/bili_asr/services/maintenance_package_core.py", "src/bili_asr/storage/maintenance_selection.py"),
             ("cli.maintenance_package", "services.maintenance_package"), ("validate", "export", "inspect", "verify", "import-plan", "import-apply", "import-check"),
             (BILIBILI_V1, UNIVERSAL_V2), "archive-maintenance-package-v1.schema.json"),
    Contract("archive-reference-backup/v1", "archive", ("src/bili_asr/services/reference_backup.py",),
             ("cli.remote_storage", "services.archive_recovery"), ("validate", "save", "check", "plan", "restore"),
             ("artifact-storage-v1", "artifact-package-v1"), "archive-reference-backup-v1.schema.json"),
    Contract("ssh-directory/v1", "storage", ("src/bili_asr/remote_storage.py", "src/bili_asr/ssh_storage_agent.py"),
             ("services.remote_artifacts", "services.reference_backup"), ("validate", "upload", "read", "range-read"),
             ("artifact-package-v1",), "ssh-directory-v1.schema.json"),
    Contract("artifact-policy-v1", "storage", ("src/bili_asr/services/artifact_policy.py",),
             ("cli.artifacts", "services.artifact_consumer"), ("validate", "read", "write"),
             ("artifact-online-v1",), "artifact-policy-v1.schema.json"),
    Contract("artifact-online-v1", "storage", ("src/bili_asr/storage/schema-artifact-online.sql",),
             ("services.artifact_policy", "services.artifact_consumer", "storage.snapshots"),
             ("extension", "snapshot", "explicit-upgrade"), ("artifact-storage-v1",)),
    Contract("artifact-state-v1", "storage", ("src/bili_asr/services/artifact_state.py",),
             ("cli.artifacts",), ("read", "validate"), ("artifact-storage-v1",), "artifact-state-v1.schema.json"),
    Contract("artifact-publication-state-v1", "storage", ("src/bili_asr/services/artifact_state.py",),
             ("coverage_report", "integrity", "services.workflow_projection"), ("read", "validate"),
             ("artifact-storage-v1",), "artifact-publication-state-v1.schema.json"),
    Contract("artifact-storage-v1", "storage", ("src/bili_asr/storage/schema-artifact-storage.sql",),
             ("storage.artifact_catalog", "services.artifact_catalog_upgrade", "storage.snapshots"),
             ("extension", "snapshot", "explicit-upgrade")),
    Contract("artifact-storage-v2", "storage", ("src/bili_asr/storage/schema-artifact-storage.sql",
             "src/bili_asr/storage/schema-artifact-storage-v2-upgrade.sql"),
             ("storage.artifact_catalog", "storage.artifact_s3_upgrade", "storage.snapshots"),
             ("extension", "snapshot", "explicit-maintenance-upgrade"), ("artifact-storage-v1",)),
    Contract("s3-compatible/v1", "storage", ("src/bili_asr/s3_storage.py",),
             ("cli.remote_storage", "services.remote_artifacts", "services.reference_backup"),
             ("read", "write", "validate"), ("artifact-storage-v2",), "s3-compatible-v1.schema.json"),
    Contract("artifact-inventory-v1", "storage", ("src/bili_asr/services/artifact_inventory_service.py",),
             ("cli.artifacts",), ("validate", "read"), (), "artifact-inventory-v1.schema.json"),
    Contract("artifact-offload-plan-v1", "storage", ("src/bili_asr/services/artifact_inventory_service.py",),
             ("cli.artifacts", "services.artifact_transfer"), ("validate", "read", "write"),
             ("artifact-storage-v1", "artifact-inventory-v1"), "artifact-offload-plan-v1.schema.json"),
    Contract("artifact-copy-plan-v1", "storage", ("src/bili_asr/services/artifact_inventory_service.py",),
             ("cli.artifacts", "services.artifact_transfer", "services.audio_retention"), ("validate", "read", "write"),
             ("artifact-storage-v1", "artifact-inventory-v1"), "artifact-copy-plan-v1.schema.json"),
    Contract("artifact-package-v1", "storage", ("src/bili_asr/artifact_packages.py",),
             ("services.artifact_transfer", "services.artifact_access", "services.artifact_restore"),
             ("validate", "read", "write"), ("artifact-storage-v1",), "artifact-package-v1.schema.json"),
    Contract(BILIBILI_V1, "storage", ("src/bili_asr/storage/schema.sql", "src/bili_asr/storage/schema-transcripts.sql",
             "src/bili_asr/storage/schema-workflow.sql", "src/bili_asr/storage/schema-editorial.sql"),
             ("archive_session", "storage.snapshots"), ("runtime", "snapshot")),
    Contract(UNIVERSAL_V2, "storage", ("src/bili_asr/storage/archive_contracts.py", "src/bili_asr/storage/schema-content-v2.sql",
             "src/bili_asr/storage/schema-observations.sql", "src/bili_asr/storage/schema-source-sync.sql"),
             ("archive_session", "storage.snapshots", "services.archive_migration"),
             ("runtime", "snapshot", "migration-target"), (BILIBILI_V1,)),
    Contract("bilibili-migration-source/v1", "storage", ("src/bili_asr/storage/migration-source-bilibili-v1.json",),
             ("storage.migration_source", "services.archive_migration"), ("migration-source",)),
    Contract(IMPORT_EXTENSION, "storage", ("src/bili_asr/storage/schema-preserved-body-import.sql",),
             ("storage.import_origins", "services.preserved_body_import", "storage.snapshots"),
             ("extension", "snapshot"), (UNIVERSAL_V2,)),
    Contract(SOURCE_SUPPLEMENT_POLICY, "sources", ("src/bili_asr/source_supplements.py",
             "src/bili_asr/storage/schema-source-supplements.sql"),
             ("storage.source_supplements", "publication_origins", "storage.snapshots"),
             ("extension", "snapshot", "export"), (IMPORT_EXTENSION,)),
    Contract(LEGACY_FACTS_POLICY, "publication", ("src/bili_asr/contracts/content_policies.py",
             "src/bili_asr/services/preserved_body_import.py"),
             ("storage.import_origins", "publication_origins"), ("content-upgrade", "historical-read"),
             (IMPORT_EXTENSION, "publication-content/v1", "publication-content/v2")),
    Contract(STORED_VTT_POLICY, "transcripts", ("src/bili_asr/services/transcript_derivatives.py", "src/bili_asr/cues.py"),
             ("cli.archive",), ("derive", "export")),
    Contract("workflow-payload/v1", "workflow", ("src/bili_asr/workflow_payloads.py",),
             ("storage.workflow", "workflow"), ("read", "write")),
    Contract("workflow-failure-policy/v1", "workflow", ("src/bili_asr/workflow_failure_policy.py",),
             ("services.workflow_retry", "services.workflow_status", "cli.workflow"),
             ("classify", "retry-decision", "historical-read"), ("workflow-payload/v1",)),
    Contract("workflow-failure-status/v1", "workflow", ("src/bili_asr/services/workflow_status.py",
             "src/bili_asr/storage/workflow_status.py"), ("cli.workflow", "operator-tools"),
             ("read", "report", "diagnostic"), ("workflow-failure-policy/v1",)),
    Contract("workflow-retry-control/v1", "workflow", ("src/bili_asr/services/workflow_retry.py",),
             ("cli.workflow", "operator-tools"), ("read", "validate", "retry-decision"),
             ("workflow-failure-policy/v1",)),
    Contract("asr-profile/v2", "workflow", ("src/bili_asr/workflow_models.py", "src/bili_asr/storage/schema-workflow.sql"),
             ("storage.workflow", "workflow_runtime"), ("read", "write")),
    Contract("asr-evidence/v1", "transcripts", ("src/bili_asr/storage/transcripts.py", "src/bili_asr/storage/schema-transcripts.sql"),
             ("storage.transcripts",), ("read", "write")),
    Contract("canonical-json/v1", "identity", ("src/bili_asr/canonical_json.py",),
             ("editorial", "publication_identity", "storage.migration_artifacts"), ("hash", "historical-read")),
    Contract(BUNDLE_SCHEMA, "transcripts", ("src/bili_asr/artifacts.py",),
             ("archive", "services.bundle_verification", "services.archive_snapshot"), ("read", "write")),
    Contract(BUNDLE_SCHEMA_V3, "transcripts", ("src/bili_asr/artifacts.py", "src/bili_asr/archive.py"),
             ("archive", "services.bundle_verification", "services.archive_snapshot"), ("read", "write"),
             (BUNDLE_SCHEMA, "source-metadata/v1")),
    Contract("frozen-input/v1", "editorial", ("src/bili_asr/editorial.py", "src/bili_asr/storage/migration_artifacts.py"),
             ("storage.editorial", "manuscript_templates"), ("historical-read", "write"), ("canonical-json/v1",)),
    Contract("frozen-input/v2", "editorial", ("src/bili_asr/storage/editorial.py",),
             ("editorial_runtime", "publication_content_v2"), ("read", "write"), (UNIVERSAL_V2, "canonical-json/v1")),
    Contract("publication-content/v1", "publication", ("src/bili_asr/publication_content.py",),
             ("storage.publication", "publication_export"), ("historical-read", "write"), ("canonical-json/v1",)),
    Contract("publication-content/v2", "publication", ("src/bili_asr/publication_content_v2.py",),
             ("storage.publication", "publication_export"), ("read", "write"), (UNIVERSAL_V2, "canonical-json/v1")),
    Contract("ai-draft-v1", "editorial", ("src/bili_asr/manuscript_templates.py",),
             ("storage.editorial", "storage.migration_artifacts"), ("render", "historical-read"), ("frozen-input/v1",)),
    Contract("ai-draft-v2", "editorial", ("src/bili_asr/publication_content_v2.py",),
             ("manuscript_templates",), ("render",), ("frozen-input/v2",)),
    Contract("publish-v1", "publication", ("src/bili_asr/manuscript_templates.py",),
             ("publication_identity", "storage.migration_artifacts"), ("render", "historical-read"), ("publication-content/v1",)),
    Contract("publish-v2", "publication", ("src/bili_asr/publication_content_v2.py",),
             ("publication", "publication_export"), ("render",), ("publication-content/v2",)),
    Contract("archive-snapshot/v1", "archive", ("src/bili_asr/services/snapshot_protocol.py", "src/bili_asr/services/archive_snapshot.py"),
             ("services.archive_snapshot", "services.archive_recovery"), ("save", "check", "restore")),
    _json("source-metadata/v1", "source-metadata-v1.schema.json", "sources"),
    Contract("youtube-source-fields/v1", "sources", ("src/bili_asr/storage/source_projection.py",),
             ("storage.sources", "storage.metadata", "search_index.source_store"), ("write", "read"),
             (UNIVERSAL_V2, "source-observations/v1")),
    _json("source-metadata/v2", "source-metadata-v2.schema.json", "sources", dependencies=("source-metadata/v1",)),
    _json("publication-catalog/v2", "publication-catalog.schema.json", dependencies=("publish-v1",)),
    _json("publication-draft-catalog/v2", "publication-draft-catalog.schema.json"),
    _json("publication-catalog/v3", "publication-catalog-v3.schema.json",
          dependencies=("publication-catalog/v2", "source-metadata/v1", "publish-v2")),
    _json("publication-draft-catalog/v3", "publication-draft-catalog-v3.schema.json",
          dependencies=("publication-draft-catalog/v2", "source-metadata/v1")),
    _json("publication-catalog/v4", "publication-catalog-v4.schema.json",
          dependencies=("publication-catalog/v3", "source-metadata/v2")),
    _json("publication-draft-catalog/v4", "publication-draft-catalog-v4.schema.json",
          dependencies=("publication-draft-catalog/v3", "source-metadata/v2")),
    _json("publication-export-manifest/v1", "publication-export-manifest.schema.json"),
    _json("publication-draft-export-manifest/v1", "publication-draft-export-manifest.schema.json"),
    _json("content-rights/v1", "content-rights.schema.json"),
    _json("publication-origin-manifest/v2", "publication-origin-manifest-v2.schema.json",
          dependencies=(ORIGIN_PROFILE,)),
    _json(ORIGIN_PROFILE, "publication-origins-v1.schema.json",
          dependencies=("publication-content/v2", SOURCE_SUPPLEMENT_POLICY)),
    _json(ORIGIN_PROFILE_V2, "publication-origins-v2.schema.json",
          dependencies=(ORIGIN_PROFILE, DESCRIPTION_SUPPLEMENT_POLICY)),
    _json("publication-origin-manifest/v3", "publication-origin-manifest-v3.schema.json",
          dependencies=(ORIGIN_PROFILE_V2,)),
    Contract(DESCRIPTION_SUPPLEMENT_POLICY, "sources", ("src/bili_asr/source_supplements.py",
             "src/bili_asr/contracts/content_policies.py"),
             ("storage.source_supplements", "publication_origins"),
             ("content-upgrade", "export"), (SOURCE_SUPPLEMENT_POLICY,)),
    _json("editorial-export-manifest/v1", "editorial-export-manifest.schema.json"),
    _json("editorial-import-export-manifest/v1", "editorial-import-export-manifest.schema.json",
          dependencies=(IMPORT_EXTENSION, "editorial-export-manifest/v1")),
    _json("editorial-review/v1", "editorial-review.schema.json"),
    _json("editorial-source-lineage/v1", "editorial-source-lineage-v1.schema.json"),
    _json("editorial-issue-review/v1", "editorial-issue-review-v1.schema.json", "editorial"),
    _json("editorial-sample-evidence/v1", "editorial-sample-evidence-v1.schema.json", "editorial",
          dependencies=("editorial-source-lineage/v1", "editorial-issue-review/v1")),
    _json("editorial-review-universal/v1", "editorial-review-universal.schema.json", dependencies=("ai-draft-v2",)),
    _json("publication-series/v1", "publication-series.schema.json"),
    _json("publication-series-editorial/v1", "publication-series-editorial.schema.json"),
    Contract("production-acceptance/v1", "operations",
             ("src/bili_asr/production_acceptance.py", "src/bili_asr/contracts/schemas/production-acceptance-v1.schema.json"),
             ("cli.production_acceptance", "operator-tools"), ("validate", "record", "verify", "show"),
             schema="production-acceptance-v1.schema.json"),
    Contract("production-acceptance-input/v1", "operations",
             ("src/bili_asr/production_acceptance.py", "src/bili_asr/contracts/schemas/production-acceptance-input-v1.schema.json"),
             ("cli.production_acceptance", "operator-tools"), ("validate", "record"),
             schema="production-acceptance-input-v1.schema.json"),
)
CONTRACTS = MappingProxyType({entry.identity: entry for entry in _CONTRACTS})


def contract(identity: str, *, capability: str | None = None) -> Contract:
    try:
        entry = CONTRACTS[identity]
    except (KeyError, TypeError) as exc:
        raise ValueError(f"unsupported contract: {identity}") from exc
    if capability is not None and capability not in entry.capabilities:
        raise ValueError(f"contract {identity} does not support {capability}")
    return entry


def catalog_contract(version: int, *, draft: bool = False) -> str:
    if type(version) is not int:
        raise ValueError("catalog version must be an integer")
    identity = f"publication{'-draft' if draft else ''}-catalog/v{version}"
    return contract(identity, capability="validate").identity


def manifest_contract(kind: str, profile: str | None, *, imported: bool = False) -> str:
    if kind not in {"publication-export", "publication-draft-export", "editorial-export"}:
        raise ValueError("unsupported export kind")
    if profile is not None:
        if profile == "private-provenance-v1" and kind == "editorial-export":
            return "editorial-provenance-export-manifest/v1"
        if profile not in {ORIGIN_PROFILE, ORIGIN_PROFILE_V2} or kind == "editorial-export":
            raise ValueError("unsupported export profile")
        return "publication-origin-manifest/v3" if profile == ORIGIN_PROFILE_V2 else "publication-origin-manifest/v2"
    if imported and kind == "editorial-export":
        return "editorial-import-export-manifest/v1"
    return kind + "-manifest/v1"


def catalog() -> dict[str, Any]:
    from bili_asr.contracts.content_policies import policy_catalog
    return {"format_version": 1, "contracts": [asdict(entry) for entry in _CONTRACTS],
            "upgrades": [asdict(entry) for entry in UPGRADE_EDGES], "content_policies": policy_catalog(),
            "release_acceptance": {"frozen_files": [asdict(entry) for entry in FROZEN_RELEASE_FILES],
                "baseline": RELEASE_BASELINE,
                "required_tests": RELEASE_REQUIRED_TESTS,
                "upgrades": [asdict(entry) for entry in UPGRADE_ACCEPTANCE],
                "transitions": [asdict(entry) for entry in CONTRACT_TRANSITIONS]}}


@dataclass(frozen=True)
class UpgradeEdge:
    """An explicit directed conversion, independent of executable services."""

    identity: str
    source: tuple[str, ...]
    target: tuple[str, ...]
    converter: str
    revision: int
    authority: tuple[str, ...]
    source_reader: str
    target_validator: str
    allowed_changes: tuple[str, ...]
    sample: str
    reversible: bool = False


UPGRADE_EDGES = (
    UpgradeEdge("bilibili-to-universal/v1", (BILIBILI_V1,), (UNIVERSAL_V2,),
                "legacy-migrate", 1, ("services/archive_migration.py", "storage/archive_contracts.py"),
                "bilibili-migration-source/v1", "snapshot-database",
                ("neutral-source-mapping", "frozen-version-registration", "migration-audit"),
                "tests/fixtures/data/bilibili-v1-frozen.zip"),
    UpgradeEdge("preserved-body-extension/v1", (UNIVERSAL_V2,), (UNIVERSAL_V2, IMPORT_EXTENSION),
                "install-preserved-body", 1,
                ("services/preserved_body_import.py", "storage/schema-preserved-body-import.sql"),
                "snapshot-database", "snapshot-database", ("empty-extension-tables",),
                "tests/test_archive_upgrade.py"),
    UpgradeEdge("source-supplement-extension/v1", (UNIVERSAL_V2, IMPORT_EXTENSION),
                (UNIVERSAL_V2, IMPORT_EXTENSION, SOURCE_SUPPLEMENT_POLICY),
                "install-source-supplement", 1,
                ("services/source_supplement.py", "storage/schema-source-supplements.sql"),
                "snapshot-database", "snapshot-database", ("empty-extension-tables",),
                "tests/test_archive_upgrade.py"),
    *(UpgradeEdge("artifact-storage-" + suffix + "/v1", combination, (*combination, ARTIFACT_STORAGE),
                  "install-artifact-storage", 1,
                  ("services/artifact_catalog_upgrade.py", "storage/schema-artifact-storage.sql"),
                  "snapshot-database", "snapshot-database", ("artifact-object-and-replica-backfill",),
                  "tests/test_archive_upgrade.py")
      for suffix, combination in (
          ("native", (UNIVERSAL_V2,)),
          ("preserved", (UNIVERSAL_V2, IMPORT_EXTENSION)),
          ("supplemented", (UNIVERSAL_V2, IMPORT_EXTENSION, SOURCE_SUPPLEMENT_POLICY)))),
    *(UpgradeEdge("artifact-online-" + suffix + "/v1", (*combination, ARTIFACT_STORAGE),
                  (*combination, ARTIFACT_STORAGE, ARTIFACT_ONLINE), "install-artifact-online", 1,
                  ("storage/artifact_online.py", "storage/schema-artifact-online.sql"),
                  "snapshot-database", "snapshot-database", ("empty-extension-tables",),
                  "tests/test_archive_upgrade.py")
      for suffix, combination in (
          ("native", (UNIVERSAL_V2,)),
          ("preserved", (UNIVERSAL_V2, IMPORT_EXTENSION)),
          ("supplemented", (UNIVERSAL_V2, IMPORT_EXTENSION, SOURCE_SUPPLEMENT_POLICY)))),
)

# Observation storage is independent of the existing optional extensions. Its
# marker is last in the canonical combination, so old edge identities remain
# unchanged and copied authority rows retain their existing fingerprints.
_OBSERVATION_BASES = tuple(dict.fromkeys(
    combination for edge in UPGRADE_EDGES for combination in (edge.source, edge.target)
    if combination[0] == UNIVERSAL_V2))
_OBSERVATION_EDGES = tuple(
    UpgradeEdge("source-observations-" + "-".join(item.replace("/", "-") for item in combination) + "/v1", combination,
                (*combination, "source-observations/v1"), "install-source-observations", 1,
                ("storage/source_observations.py", "storage/schema-source-observations.sql"),
                "snapshot-database", "snapshot-database", ("empty-extension-tables",),
                "tests/test_source_observations.py")
    for combination in _OBSERVATION_BASES)
UPGRADE_EDGES = (*UPGRADE_EDGES, *_OBSERVATION_EDGES, *(
    UpgradeEdge(edge.identity + "-with-observations", (*edge.source, "source-observations/v1"),
                (*edge.target, "source-observations/v1"), edge.converter, edge.revision,
                edge.authority, edge.source_reader, edge.target_validator, edge.allowed_changes,
                "tests/test_source_observations.py")
    for edge in UPGRADE_EDGES if edge.converter in {"install-artifact-storage", "install-artifact-online"}))


@dataclass(frozen=True)
class FrozenReleaseFile:
    path: str
    sha256: str


RELEASE_BASELINE = "tests/fixtures/data/contracts-release-baseline-v1.json"
FROZEN_RELEASE_FILES = (
    FrozenReleaseFile("tests/fixtures/data/bilibili-v1-frozen.zip",
        "20790dfc0acec3dda5707cb534907374c458f99b04f1947dc865ffe67b93770b"),
    FrozenReleaseFile("tests/fixtures/data/bilibili-v1-frozen.json",
        "2e2d67cac953c115d541e210cd663c91adc07327ff3c158947eb0e9621849844"),
    FrozenReleaseFile(RELEASE_BASELINE, "7ea6da1930354dbb5e871ae84828b189138d88a2b1467c33edd75174913f194e"),
)


@dataclass(frozen=True)
class UpgradeAcceptance:
    edge: str
    frozen_files: tuple[str, ...]
    # A function node admits itself and its parametrizations, never similarly
    # named tests. Exact parametrized node IDs protect each concrete edge.
    test_nodes: tuple[str, ...]


_HISTORY = tuple(entry.path for entry in FROZEN_RELEASE_FILES[:2])
_UPGRADE_TEST = "tests/test_archive_upgrade.py::"
UPGRADE_ACCEPTANCE = (
    *(UpgradeAcceptance(edge, _HISTORY, (
        _UPGRADE_TEST + "test_frozen_legacy_upgrades_all_registered_combinations_and_snapshots[" + case + "]",
        _UPGRADE_TEST + "test_changed_or_invalid_inputs_fail_before_target_install"))
      for edge, case in (("bilibili-to-universal/v1", "native"),
                        ("preserved-body-extension/v1", "preserved"),
                        ("source-supplement-extension/v1", "supplemented"))),
    *(UpgradeAcceptance("artifact-" + extension + "-" + case + "/v1", _HISTORY, tuple(
        _UPGRADE_TEST + "test_artifact_catalog_is_reachable_from_each_actual_universal_combination[" + mode + "-" + origin + "-" + case + "]"
        for origin in ("historical", "native")))
      for extension, mode in (("storage", "stored"), ("online", "online"))
      for case in ("native", "preserved", "supplemented")),
)
UPGRADE_ACCEPTANCE = (*UPGRADE_ACCEPTANCE, *(
    UpgradeAcceptance(edge.identity, _HISTORY, (
        "tests/test_source_observations.py::test_explicit_upgrade_preserves_history_and_raw_observations",))
    for edge in UPGRADE_EDGES if edge.sample == "tests/test_source_observations.py"))


@dataclass(frozen=True)
class ContractTransition:
    """A deliberate replacement/reader retirement, with an executable path."""
    contract: str
    kind: str
    source: tuple[str, ...]
    target: tuple[str, ...]
    path: tuple[str, ...]
    acceptance: tuple[str, ...]
    reason: str


# New independent contracts need no artificial predecessor. Removing a known
# identity, capability or consumer does require a reviewed declaration here.
CONTRACT_TRANSITIONS: tuple[ContractTransition, ...] = ()
RELEASE_REQUIRED_TESTS = (
    "tests/test_installed_cli.py::test_installed_contract_resources_resolve_offline_outside_checkout",
    "tests/test_installed_cli.py::test_installed_contract_authorities_include_registered_sql_and_json",
    "tests/test_upgrade_result_reuse.py::test_formal_upgrade_reuses_completed_ai_jobs_and_preserves_pending_work",
    "tests/test_upgrade_result_reuse.py::test_explicit_refresh_or_changed_configuration_creates_current_input",
    "tests/test_upgrade_result_reuse.py::test_upgrade_keeps_committed_chunks_and_only_explicit_retry_runs_missing_chunks",
    "tests/test_upgrade_result_reuse.py::test_replanning_does_not_revive_terminal_historical_editorial_job[failed]",
    "tests/test_upgrade_result_reuse.py::test_replanning_does_not_revive_terminal_historical_editorial_job[cancelled]",
    "tests/test_content_upgrade_policies.py::test_formal_upgrade_and_registered_content_conversion_keep_original_review_and_body",
    "tests/test_source_supplement.py::test_title_only_change_preserves_current_human_edits_and_historical_ai",
    "tests/test_source_supplement.py::test_export_pair_and_snapshot_restore_keep_title_evidence",
    *(_UPGRADE_TEST + "test_frozen_legacy_upgrades_all_registered_combinations_and_snapshots[" + case + "]"
      for case in ("native", "preserved", "supplemented")),
)


def upgrade_path(source: tuple[str, ...], target: tuple[str, ...], *,
                 selected: tuple[str, ...] | None = None,
                 edges: tuple[UpgradeEdge, ...] = UPGRADE_EDGES) -> tuple[UpgradeEdge, ...]:
    """Select only registered paths; ambiguity requires an exact edge sequence."""
    for identity in (*source, *target):
        contract(identity)
    if source == target:
        raise ValueError("source already has the requested contracts; use snapshot for a copy")
    paths: list[tuple[UpgradeEdge, ...]] = []

    def visit(current: tuple[str, ...], trail: tuple[UpgradeEdge, ...],
              seen: frozenset[tuple[str, ...]]) -> None:
        if current == target:
            paths.append(trail)
            return
        for edge in edges:
            if edge.source == current and edge.target not in seen:
                visit(edge.target, (*trail, edge), seen | {edge.target})

    visit(source, (), frozenset({source}))
    if selected is not None:
        paths = [path for path in paths if tuple(edge.identity for edge in path) == selected]
    if not paths:
        raise ValueError("no registered upgrade path for the exact contract combination")
    if len(paths) != 1:
        raise ValueError("ambiguous upgrade path; select the complete edge sequence explicitly")
    return paths[0]
