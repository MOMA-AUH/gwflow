"""User-facing names for gwf target and backend states."""


def state_name(state):
    """Use US spelling while preserving gwf's enum members internally."""
    return "canceled" if state.name == "CANCELLED" else state.name.lower()
