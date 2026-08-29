"""Persisted cache for app.services.vocabulary_builder."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.models.risk_models import ThreatTier


@dataclass(frozen=True, slots=True)
class LearnedPhrase:
    """One phrase the Vocabulary Builder discovered, with its ranking score."""

    phrase: str
    family: str
    tier_value: float  # ThreatTier.value -- stored as a plain float for JSON
    score: float  # usefulness ranking -- higher is more useful, see vocabulary_builder.py

    def to_dict(self) -> dict:
        return {
            "phrase": self.phrase,
            "family": self.family,
            "tier_value": self.tier_value,
            "score": self.score,
        }

    @staticmethod
    def from_dict(data: dict) -> "LearnedPhrase":
        return LearnedPhrase(
            phrase=str(data.get("phrase", "")),
            family=str(data.get("family", "")),
            tier_value=float(data.get("tier_value", 0.0)),
            score=float(data.get("score", 0.0)),
        )

    def as_tier(self) -> ThreatTier:
        """Snap the stored numeric tier value back to the nearest ThreatTier member."""
        return min(ThreatTier, key=lambda t: abs(t.value - self.tier_value))


@dataclass(slots=True)
class VocabularyCache:
    """Everything the Vocabulary Builder persists between runs."""

    #: channel username -> highest Telethon message id already analyzed,
    #: so the next run only fetches messages newer than this (incremental).
    progress_by_channel: dict[str, int] = field(default_factory=dict)
    learned_phrases: list[LearnedPhrase] = field(default_factory=list)
    #: ignore-phrase candidates discovered from donation/ad-heavy history
    #: (surfaced for visibility via the Лог; not auto-applied to the
    #: classifier -- see vocabulary_builder.py's module docstring for why).
    learned_ignore_phrases: list[str] = field(default_factory=list)
    last_run_at: str = ""  # ISO timestamp, empty if never run

    def to_dict(self) -> dict:
        return {
            "progress_by_channel": dict(self.progress_by_channel),
            "learned_phrases": [p.to_dict() for p in self.learned_phrases],
            "learned_ignore_phrases": list(self.learned_ignore_phrases),
            "last_run_at": self.last_run_at,
        }

    @staticmethod
    def from_dict(data: dict) -> "VocabularyCache":
        return VocabularyCache(
            progress_by_channel={
                str(k): int(v) for k, v in (data.get("progress_by_channel") or {}).items()
            },
            learned_phrases=[
                LearnedPhrase.from_dict(p) for p in (data.get("learned_phrases") or [])
            ],
            learned_ignore_phrases=[str(p) for p in (data.get("learned_ignore_phrases") or [])],
            last_run_at=str(data.get("last_run_at", "")),
        )
