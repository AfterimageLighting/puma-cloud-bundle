"""Shared PUMA operational status rules.

Automated workflows may advance ordinary purchasing/delivery states, but must
never roll a later state backward or overwrite special/manual states.
"""

_STATUS_RANK = {
    "": 0,
    "unapproved": 0,
    "approved": 0,
    "to be ordered": 0,
    "ordered": 1,
    "received": 2,
    "scheduled": 3,
    "delivered": 4,
}


def normalize_status(value) -> str:
    return " ".join(str(value or "").strip().lower().split())


def can_advance_status(current, target) -> bool:
    current_key = normalize_status(current)
    target_key = normalize_status(target)
    if current_key not in _STATUS_RANK or target_key not in _STATUS_RANK:
        return False
    return _STATUS_RANK[target_key] >= _STATUS_RANK[current_key]


def is_later_than(current, target) -> bool:
    current_key = normalize_status(current)
    target_key = normalize_status(target)
    if current_key not in _STATUS_RANK or target_key not in _STATUS_RANK:
        return False
    return _STATUS_RANK[current_key] > _STATUS_RANK[target_key]


def can_auto_omit(current) -> bool:
    """RFA may auto-omit only before an item has been ordered."""
    return normalize_status(current) in {
        "",
        "unapproved",
        "approved",
        "to be ordered",
    }
