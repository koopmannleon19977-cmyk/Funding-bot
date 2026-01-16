"""JSON parsing utilities."""

import json


def dumps(obj, **kwargs) -> str:
    """Serialize obj to a JSON formatted str."""
    return json.dumps(obj, **kwargs)


def loads(s, **kwargs):
    """Deserialize s (a str, bytes or bytearray instance containing a JSON document) to a Python object."""
    return json.loads(s, **kwargs)
