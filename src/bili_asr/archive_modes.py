"""Pure archive access and schema policy identities."""
from enum import Enum


class ArchiveAccessMode(Enum):
    READ = "read"
    WRITE = "write"
    BOOTSTRAP = "bootstrap"
    MAINTENANCE = "maintenance"


class ArchiveContract(Enum):
    RUNTIME = "runtime"
    MANUSCRIPT = "manuscript"
    NONE = "none"
