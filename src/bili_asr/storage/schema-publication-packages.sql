-- Explicit optional publication-packages/v1 extension. Never installed on read.
CREATE TABLE publication_packages_contract (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    version INTEGER NOT NULL CHECK (version = 1)
);
CREATE TRIGGER immutable_publication_packages_contract_update
BEFORE UPDATE ON publication_packages_contract BEGIN
    SELECT RAISE(ABORT, 'publication package contract is immutable');
END;
CREATE TRIGGER immutable_publication_packages_contract_delete
BEFORE DELETE ON publication_packages_contract BEGIN
    SELECT RAISE(ABORT, 'publication package contract is immutable');
END;
CREATE TABLE publication_package_legacy (
    release_id TEXT PRIMARY KEY REFERENCES publication_releases(release_id) ON DELETE RESTRICT
);
CREATE TRIGGER immutable_publication_package_legacy_insert
BEFORE INSERT ON publication_package_legacy
WHEN EXISTS (SELECT 1 FROM publication_packages_contract) BEGIN
    SELECT RAISE(ABORT, 'publication package baseline is frozen');
END;
CREATE TRIGGER immutable_publication_package_legacy_update
BEFORE UPDATE ON publication_package_legacy BEGIN
    SELECT RAISE(ABORT, 'publication package baseline is immutable');
END;
CREATE TRIGGER immutable_publication_package_legacy_delete
BEFORE DELETE ON publication_package_legacy BEGIN
    SELECT RAISE(ABORT, 'publication package baseline is immutable');
END;
CREATE TABLE IF NOT EXISTS publication_packages (
    release_id TEXT PRIMARY KEY REFERENCES publication_releases(release_id) ON DELETE RESTRICT,
    relative_path TEXT NOT NULL UNIQUE,
    package_sha256 TEXT NOT NULL CHECK (
        length(package_sha256) = 64 AND package_sha256 NOT GLOB '*[^0-9a-f]*'
    )
);
CREATE TRIGGER IF NOT EXISTS immutable_publication_packages_update
BEFORE UPDATE ON publication_packages BEGIN
    SELECT RAISE(ABORT, 'publication packages are immutable');
END;
CREATE TRIGGER IF NOT EXISTS immutable_publication_packages_delete
BEFORE DELETE ON publication_packages BEGIN
    SELECT RAISE(ABORT, 'publication packages are immutable');
END;
