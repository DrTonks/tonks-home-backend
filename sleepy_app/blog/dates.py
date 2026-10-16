"""Select dated blog entries without relying on zero-padded source strings."""
from datetime import datetime


def latest_dated_entry(entries):
    """Return the newest valid entry, preserving source order for equal dates."""
    latest = None
    latest_date = None
    if not isinstance(entries, list):
        return None
    for entry in entries:
        value = entry.get('startDate') if isinstance(entry, dict) else None
        if not isinstance(value, str):
            continue
        try:
            parsed = datetime.strptime(value.strip(), '%Y-%m-%d').date()
        except ValueError:
            continue
        if latest_date is None or parsed > latest_date:
            latest = entry
            latest_date = parsed
    return latest
