"""
Analyzes one Telegram message's text and returns a structured
:class:`MessageAnalysis` -- what kind of event it's about (family), how
severe that kind of event is (tier), and how certain the message itself
sounds (status: possible / reported / confirmed / cancelled / all-clear).

This replaces the old model, which just summed positive/negative
keyword weights into one float per message and handed that straight to
``AlertService`` to add onto a single running score. That old model is
exactly what produced the problems this redesign was asked to fix:
- risk was driven by *how many* messages arrived, not by what they said;
- one confirmed-sounding message and one vague rumor scored the same way
  a keyword happened to match;
- a duplicate repost of the same report added risk again;
- there was no notion of "this specific threat is now over" -- only a
  generic negative-weight "відбій" that happened to cancel out whatever
  else was accumulated, regardless of whether it was related.

This module stays a *stateless* text -> analysis function (no memory of
previous messages) -- corroboration across messages/channels, dedup, and
time-based decay all live in ``AlertService``, which is what actually
tracks active events over time. That separation mirrors this project's
existing split between stateless analyzers and the stateful service that
aggregates them.
"""

from __future__ import annotations

import hashlib
import re
from typing import Optional

from app.models.alert_models import RiskLevel
from app.models.risk_models import MessageAnalysis, ThreatStatus, ThreatTier
from app.services import message_classifier
from app.services.threat_vocabulary import (
    CANCEL_MARKERS,
    CONFIRMED_MARKERS,
    FAMILY_ALL,
    FAMILY_CANCEL_HINTS,
    FAMILY_KEYWORDS,
    POSSIBLE_MARKERS,
)

#: Tier stepping order for CONFIRMED (+1 step) / POSSIBLE (-1 step) markers.
_TIER_STEPS: tuple[ThreatTier, ...] = (
    ThreatTier.LOW,
    ThreatTier.MEDIUM,
    ThreatTier.HIGH,
    ThreatTier.VERY_HIGH,
)


def _step_tier(tier: ThreatTier, delta: int) -> ThreatTier:
    """Move ``tier`` up/down ``delta`` steps in ``_TIER_STEPS``, clamped at the ends."""
    if tier not in _TIER_STEPS:
        return tier
    index = _TIER_STEPS.index(tier)
    new_index = max(0, min(len(_TIER_STEPS) - 1, index + delta))
    return _TIER_STEPS[new_index]


def _compile(phrase: str, whole_word: bool) -> re.Pattern[str]:
    pattern = rf"\b{re.escape(phrase)}\b" if whole_word else re.escape(phrase)
    return re.compile(pattern, re.IGNORECASE)


class RiskAnalyzer:
    """Stateless text -> :class:`MessageAnalysis` analyzer.

    ``learned_phrases`` (optional) lets :mod:`app.services.vocabulary_builder`
    supply additional (family, tier, phrase) triples discovered from a
    channel's own history -- matched alongside the hand-curated table, at
    whatever tier the builder assigned (itself capped, see that module),
    never replacing or overriding a hand-curated match. Call
    :meth:`set_learned_phrases` to update them later (e.g. after each
    periodic Vocabulary Builder run) without rebuilding the analyzer.
    """

    def __init__(
        self,
        learned_phrases: Optional[list[tuple[str, ThreatTier, str]]] = None,
    ) -> None:
        """Compile the hand-curated (and optional learned) vocabulary once."""
        self._curated_patterns: list[tuple[str, ThreatTier, re.Pattern[str]]] = [
            (family, tier, _compile(phrase, whole_word))
            for family, tier, phrase, whole_word in FAMILY_KEYWORDS
        ]
        self._learned_patterns: list[tuple[str, ThreatTier, re.Pattern[str]]] = []
        self.set_learned_phrases(learned_phrases or [])

        self._confirmed_re = re.compile(
            "|".join(re.escape(p) for p in CONFIRMED_MARKERS), re.IGNORECASE
        )
        self._possible_re = re.compile(
            "|".join(re.escape(p) for p in POSSIBLE_MARKERS), re.IGNORECASE
        )
        self._cancel_re = re.compile(
            "|".join(re.escape(p) for p in CANCEL_MARKERS), re.IGNORECASE
        )
        self._family_hint_res: list[tuple[str, re.Pattern[str]]] = [
            (family, re.compile(re.escape(hint), re.IGNORECASE)) for family, hint in FAMILY_CANCEL_HINTS
        ]

    def set_learned_phrases(self, learned_phrases: list[tuple[str, ThreatTier, str]]) -> None:
        """Replace the current set of Vocabulary-Builder-learned phrases.

        Replaces (not appends) so a periodic re-run doesn't grow this
        list forever -- ``vocabulary_builder.py`` already re-ranks and
        caps its own output each run; this just mirrors whatever it
        currently considers the best set.
        """
        self._learned_patterns = [
            (family, tier, _compile(phrase, whole_word=False)) for family, tier, phrase in learned_phrases
        ]
        # Cached once here (not recomputed per analyze() call, which runs
        # per incoming message) -- rebuilt only on this comparatively
        # rare event (startup, or each periodic Vocabulary Builder run).
        self._family_patterns = self._curated_patterns + self._learned_patterns

    def analyze(self, text: str) -> MessageAnalysis:
        """Analyze one message's raw text end-to-end.

        Order of operations: (1) strip ignorable ad/donation/greeting
        segments -- if nothing meaningful remains, this message never
        touches risk at all; (2) check for a cancellation/all-clear,
        which takes priority over any weapon keyword also present in
        the same message (an "відбій, шахеди збито" report IS the
        cancellation, not a fresh shahed report); (3) otherwise find the
        strongest-tier family keyword match and apply any confirmed/
        possible status modifier.
        """
        relevant_text = message_classifier.strip_ignorable_segments(text)
        if not relevant_text:
            return MessageAnalysis(
                is_relevant=False,
                relevant_text="",
                family=FAMILY_ALL,
                tier=ThreatTier.ZERO,
                status=ThreatStatus.REPORTED,
            )

        lower = relevant_text.lower()

        cancel_match = self._cancel_re.search(lower)
        if cancel_match:
            matched_family = FAMILY_ALL
            for family, hint_re in self._family_hint_res:
                if hint_re.search(lower):
                    matched_family = family
                    break
            status = ThreatStatus.CANCELLED if matched_family != FAMILY_ALL else ThreatStatus.ALL_CLEAR
            return MessageAnalysis(
                is_relevant=True,
                relevant_text=relevant_text,
                family=matched_family,
                tier=ThreatTier.ZERO,
                status=status,
                matched_terms=(cancel_match.group(0),),
                dedup_fingerprint=_fingerprint(relevant_text),
            )

        matches: list[tuple[str, ThreatTier, str]] = []
        for family, tier, pattern in self._family_patterns:
            found = pattern.search(lower)
            if found:
                matches.append((family, tier, found.group(0)))

        if not matches:
            return MessageAnalysis(
                is_relevant=False,
                relevant_text=relevant_text,
                family=FAMILY_ALL,
                tier=ThreatTier.ZERO,
                status=ThreatStatus.REPORTED,
            )

        # Strongest family wins if a message mentions more than one kind
        # of thing -- this keeps one event per message tractable; see
        # module docstring for why cross-message merging lives elsewhere.
        matches.sort(key=lambda m: m[1].value, reverse=True)
        dominant_family, base_tier, _ = matches[0]
        matched_terms = tuple(dict.fromkeys(m[2] for m in matches))  # de-dup, keep order

        status = ThreatStatus.REPORTED
        tier = base_tier
        if self._confirmed_re.search(lower):
            status = ThreatStatus.CONFIRMED
            tier = _step_tier(base_tier, +1)
        elif self._possible_re.search(lower):
            status = ThreatStatus.POSSIBLE
            tier = _step_tier(base_tier, -1)

        return MessageAnalysis(
            is_relevant=True,
            relevant_text=relevant_text,
            family=dominant_family,
            tier=tier,
            status=status,
            matched_terms=matched_terms,
            dedup_fingerprint=_fingerprint(relevant_text),
        )

    @staticmethod
    def score_to_level(score: float) -> RiskLevel:
        """Map an aggregated 0..100 score to a discrete :class:`RiskLevel`."""
        clamped = max(0.0, min(100.0, score))
        if clamped <= 0.0:
            return RiskLevel.NONE
        if clamped < 25.0:
            return RiskLevel.LOW
        if clamped < 50.0:
            return RiskLevel.MEDIUM
        if clamped < 75.0:
            return RiskLevel.HIGH
        return RiskLevel.CRITICAL

    @staticmethod
    def clamp_score(score: float) -> float:
        """Clamp a running risk score into the valid 0..100 range."""
        return max(0.0, min(100.0, score))


def _fingerprint(text: str) -> str:
    """A short, stable fingerprint of normalized text, for exact/near-duplicate detection.

    Deliberately simple (lowercase + collapse whitespace + hash) rather
    than a fuzzy-similarity measure -- catches the extremely common case
    of the same report copy-pasted or auto-forwarded across channels
    verbatim, without the cost/complexity of real near-duplicate
    detection for a fairly small gain (see module docstring).
    """
    normalized = re.sub(r"\s+", " ", text.strip().lower())
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:16]
