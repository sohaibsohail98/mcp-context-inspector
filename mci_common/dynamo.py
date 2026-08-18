from decimal import Decimal


def clean_decimal(value):
    """DynamoDB returns Decimal for every number; JSON callers can't
    serialize Decimal directly."""
    if isinstance(value, list):
        return [clean_decimal(v) for v in value]
    if isinstance(value, dict):
        return {k: clean_decimal(v) for k, v in value.items()}
    if isinstance(value, Decimal):
        return int(value) if value % 1 == 0 else float(value)
    return value
