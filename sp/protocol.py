"""Version the shell/backend wire contract independently of application releases."""

PROTOCOL_MAJOR = 1
PROTOCOL_MINOR = 0
CAPABILITIES = ("native-error-object-v1",)


def protocol_manifest() -> dict:
    """Return fresh mutable containers; callers cannot change subsequent responses."""
    return {"major": PROTOCOL_MAJOR, "minor": PROTOCOL_MINOR,
            "capabilities": list(CAPABILITIES)}
