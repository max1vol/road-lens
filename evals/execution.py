"""Keep observable failure data without exporting credentials or hidden reasoning."""
class EvaluationExecutionError(Exception):
    def __init__(self, cause, run, raw):
        self.cause_type = type(cause).__name__
        self.run = run
        self.raw = raw
        super().__init__(self.cause_type)


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
        calls.append({'model': getattr(message, 'model_name', None), 'parts': parts})
    return calls
