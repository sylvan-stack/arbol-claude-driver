"""Typed boundaries after validating caller input and before publishing output."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, NotRequired, TypedDict

ErrorCode = Literal['invalid_input', 'not_found', 'in_use', 'unsupported', 'not_signed_in', 'credential_rejected', 'rate_limited', 'context_exceeded', 'vendor_unavailable', 'vendor_error', 'tool_missing', 'tool_unsupported', 'tool_failed', 'limit_reached', 'state_newer', 'internal']


class DriverError(TypedDict):
    code: ErrorCode
    message: str
    retryable: bool
    remedy: NotRequired[str]
    retryAfterSeconds: NotRequired[float]
    detail: NotRequired[dict[str, Any]]


class ProcessOwner(TypedDict):
    pid: int
    startedAt: str


@dataclass(frozen=True)
class ValidatedRequest:
    run_id: str
    subscription_id: str
    tools: Literal['all', 'none']
    history: list[dict[str, Any]]
    input: dict[str, Any]
    fields: dict[str, Any]

    def record(self) -> dict[str, Any]:
        return {**self.fields, 'runId': self.run_id, 'subscriptionId': self.subscription_id,
                'tools': self.tools, 'history': self.history, 'input': self.input}


@dataclass(frozen=True)
class EventEnvelope:
    run_id: str
    sequence: int
    timestamp: str
    labels: dict[str, str] | None
    event: dict[str, Any]

    def record(self) -> dict[str, Any]:
        return {'runId': self.run_id, 'seq': self.sequence, 'at': self.timestamp,
                **({'labels': self.labels} if self.labels is not None else {}), **self.event}
