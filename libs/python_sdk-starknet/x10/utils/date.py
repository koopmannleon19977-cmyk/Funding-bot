import math
from datetime import UTC, datetime


def utc_now():
    return datetime.now(tz=UTC)


def to_epoch_millis(value: datetime):
    assert value.tzinfo == UTC, "`value` must be in UTC"

    # Use ceiling to match the hash_order logic which uses math.ceil
    return int(math.ceil(value.timestamp() * 1000))
