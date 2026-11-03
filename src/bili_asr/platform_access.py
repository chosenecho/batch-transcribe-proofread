"""Optional evidence checks for live Bilibili acquisition."""
import os
import re


class PlatformAccessError(ValueError):
    """Strict acquisition policy requires valid evidence references."""


def require_platform_access() -> None:
    from bili_asr.compliance import compliance_enabled
    if not compliance_enabled() and os.environ.get('BILI_PLATFORM_ACCESS_REQUIRE_EVIDENCE') != '1':
        return
    # Opaque references only: no credential, signed URL or correspondence in logs.
    for name in ('BILI_PLATFORM_ACCESS_REFERENCE', 'BILI_CONTENT_PROCESSING_REFERENCE'):
        value = os.environ.get(name, '')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', value):
            raise PlatformAccessError(
                f'platform-access: live Bilibili acquisition disabled; record actual authorization in {name}'
            )
