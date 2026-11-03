"""Deployment opt-in for compliance workflow restrictions."""
import os

COMPLIANCE_ENABLED_ENV_VAR = 'BILI_COMPLIANCE_ENABLED'


def compliance_enabled() -> bool:
    return os.environ.get(COMPLIANCE_ENABLED_ENV_VAR) == '1'
