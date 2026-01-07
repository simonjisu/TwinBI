from __future__ import annotations

from typing import Any, Dict, Optional


def event_to_filter(event: Dict[str, Any], mapping: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    dimension = mapping.get("dimension")
    if not dimension:
        return None

    value = None
    if event.get("customdata"):
        value = event["customdata"][0]
    elif "x" in event:
        value = event["x"]

    if value is None:
        return None

    return {"member": dimension, "operator": "equals", "values": [value]}

