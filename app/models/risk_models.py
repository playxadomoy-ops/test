"""
Shared types for the risk-scoring redesign.

Replaces the old model (a single float "contribution" added per message,
straight into a global additive+decay score) with a richer, structured
analysis of each message: what *kind* of threat it's about (family),
how severe that kind of event is (tier), and how certain the message
itself sounds (status). ``AlertService`` (see ``app/services/alert_service.py``)
turns a stream of these into a small set of "currently active threat
events" and derives the overall risk from the strongest ones still
active -- not from how many messages have ever been analyzed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class ThreatTier(Enum):
    """How severe a given *kind* of confirmed event is, on its own.

    Values are the 0-100 severity a single, fully-confirmed,
    multi-channel-corroborated event of that tier reaches -- not what a
    single unconfirmed message immediately produces (see
    ``ThreatStatus``/``CONFIDENCE_BY_CORROBORATION`` in
    ``risk_engine_constants`` for why one message never jumps straight
    to this number).
    """

    ZERO = 0.0
    LOW = 22.0
    MEDIUM = 45.0
    HIGH = 72.0
    VERY_HIGH = 96.0


class ThreatStatus(Enum):
    """How certain the message itself sounds, independent of severity."""

    POSSIBLE = "possible"      # "можливо", "ймовірно" -- explicitly hedged
    REPORTED = "reported"      # a plain statement, no hedge or confirmation word
    CONFIRMED = "confirmed"    # "підтверджено", official/officer-style confirmation
    CANCELLED = "cancelled"    # "відбій" -- this specific event/family is over
    ALL_CLEAR = "all_clear"    # "чисто", generic all-clear not tied to one family


#: Event "family" -- a coarse bucket used to (a) merge duplicate/repeated
#: reports of *the same kind of thing* into one active event instead of
#: stacking them, and (b) let a CANCELLED/ALL_CLEAR message target the
#: right family instead of clearing everything indiscriminately.
EventFamily = str  # one of the FAMILY_* constants in threat_vocabulary.py


@dataclass(frozen=True, slots=True)
class MessageAnalysis:
    """The structured result of analyzing one Telegram message's text.

    ``is_relevant=False`` means the message was recognized as fully
    off-topic (donation/ad/greeting/etc, see ``message_classifier.py``)
    -- callers should not create or reinforce any event for it at all,
    not even a zero-weight one.
    """

    is_relevant: bool
    relevant_text: str
    family: EventFamily
    tier: ThreatTier
    status: ThreatStatus
    matched_terms: tuple[str, ...] = field(default_factory=tuple)
    dedup_fingerprint: str = ""
