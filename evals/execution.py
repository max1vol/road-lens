"""Keep observable failure data without exporting credentials or hidden reasoning."""
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
import json


def json_default(value):
    """Preserve Decimal accuracy and reject arbitrary object reprs in exports."""
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError('Non-finite Decimal cannot be exported')
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if hasattr(value, 'model_dump'):
        return value.model_dump(mode='json')
    raise TypeError('Unsupported evaluation export type: ' + type(value).__name__)


def json_safe(value):
    return json.loads(json.dumps(value, default=json_default, allow_nan=False))


class EvaluationExecutionError(Exception):
    def __init__(self, cause, run, raw):
        self.cause_type = cause if isinstance(cause, str) else type(cause).__name__
        self.run = run
        self.raw = raw
        super().__init__(self.cause_type)

    def __reduce__(self):
        # Modal serializes exceptions between processes. Retain only the safe
        # cause class and explicit observations, not provider exception objects.
        return type(self), (self.cause_type, self.run, self.raw)

    def as_result(self):
        return json_safe({'ok': False, 'error': {'type': self.cause_type}, 'run': self.run, 'raw_model_calls': self.raw})


def visible_messages(messages):
    calls = []
    for message in messages:
        if getattr(message, 'kind', '') != 'response':
            continue
        parts = []
        for part in message.parts:
            if getattr(part, 'part_kind', '') == 'text':
                parts.append({'text': part.content})
            elif getattr(part, 'part_kind', '') == 'tool-call':
                parts.append({'function_call': {'name': part.tool_name, 'args': part.args, 'id': part.tool_call_id}})
        usage = getattr(message, 'usage', None)
        calls.append({'model': getattr(message, 'model_name', None), 'parts': parts,
            'finish_reason': getattr(message, 'finish_reason', None),
            'usage': json_safe(usage) if usage is not None else None})
    return calls
