"""Legacy worker credential seam; processing never contacts a platform."""
SESSDATA_ENV_VAR = "BILI_SESSDATA"


def resolve_sessdata(
    flag_value: str | None, environment_value: str | None = None
) -> str | None:
    """Return the credential from the flag, else the environment, else None.

    The value is resolved for gateway construction only and is never
    echoed.  An explicitly blank flag (``""``) forces anonymous access and
    never falls through to the environment; a blank environment value
    likewise resolves to no credential.
    """

    if flag_value is not None:
        return flag_value or None
    return environment_value or None

