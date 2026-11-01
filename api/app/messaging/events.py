"""Message events (AA-958): the seam the CRM-29a workflow engine and CRM-29b tags build on.

Every SMS / WhatsApp message that arrives or leaves calls :func:`emit` inside the transaction that stored it:

1. a durable ``crm.metric_event`` row on the trip when the conversation has one (``message.received``, with a bell
   item for the trip owner: "a client wrote"; ``message.sent``). ``detail`` holds ids and codes only (channel,
   conversation and message ids, the match method), never a number or text;
2. then each in-process listener registered with :func:`subscribe`, with the same :class:`MessageEvent` (ids and
   codes only). A listener runs in the same transaction: an exception rolls the message back too, so a listener
   that may fail catches its own errors (or queues a ``crm.job``).

No listener is registered by this card.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.proposals import events as trip_events
from app.tenancy import AnyContext

RECEIVED = "received"
SENT = "sent"


@dataclass(frozen=True)
class MessageEvent:
    kind: str  # received | sent
    channel: str  # sms | whatsapp
    message_id: uuid.UUID
    conversation_id: uuid.UUID
    trip_id: uuid.UUID | None
    traveler_id: uuid.UUID | None
    match_method: str | None = None


Listener = Callable[[AnyContext, MessageEvent], Awaitable[None]]
_LISTENERS: list[Listener] = []


def subscribe(listener: Listener) -> Callable[[], None]:
    """Register a listener; returns the function that removes it."""
    _LISTENERS.append(listener)
    return lambda: _LISTENERS.remove(listener) if listener in _LISTENERS else None


async def emit(ctx: AnyContext, event: MessageEvent) -> None:
    if event.trip_id is not None:
        detail = {
            "channel": event.channel,
            "conversation_id": str(event.conversation_id),
            "message_id": str(event.message_id),
        }
        if event.match_method:
            detail["match_method"] = event.match_method
        name = trip_events.MESSAGE_RECEIVED if event.kind == RECEIVED else trip_events.MESSAGE_SENT
        await trip_events.record(ctx, event.trip_id, name, detail=detail, bell=event.kind == RECEIVED)
    for listener in list(_LISTENERS):
        await listener(ctx, event)
