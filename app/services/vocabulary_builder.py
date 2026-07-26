"""
Threat Vocabulary Builder.

Uses Telethon (already a dependency for the live message stream, see
``app.telegram.telegram_service``) to read through a monitored channel's
*history* -- not just new messages -- looking for words/phrases that
should be added to :mod:`app.services.threat_vocabulary` as additional,
lower-weight, clearly-separate "learned" entries.

## Method (deliberately simple, stdlib-only -- no ML dependency on a phone)

For every historical message (after the same cleaning/ignore-filtering
``message_classifier.py`` applies to live messages):
  1. Extract 1-3 word n-grams as phrase candidates.
  2. A candidate that frequently co-occurs, in the same message, with an
     already-known threat keyword (from ``threat_vocabulary.FAMILY_KEYWORDS``)
     is a candidate THREAT phrase for that keyword's family.
  3. A candidate that frequently co-occurs with a cancellation marker
     (``threat_vocabulary.CANCEL_MARKERS``) is a candidate CANCELLATION phrase.
  4. A candidate that occurs mostly (or only) in messages
     ``message_classifier.is_fully_ignorable`` already flagged is a
     candidate IGNORE phrase.

This is co-occurrence/frequency counting, not semantic understanding --
it will surface some noise, which is why:
  * every candidate needs a minimum absolute frequency AND a minimum
    co-occurrence ratio before it's kept at all (see ``_MIN_*`` below);
  * learned THREAT/CANCEL phrases are applied at a fixed, low, capped
    tier (``_LEARNED_TIER``) that never reaches or exceeds anything
    hand-curated, and are clearly separate in storage/logging from the
    hand-curated table;
  * learned IGNORE-phrase candidates are only ever *logged* (visible in
    Лог) for now, not auto-applied to ``message_classifier`` -- silently
    teaching the classifier to discard more text is a much higher-risk
    mistake (a real threat report gets silently dropped) than a learned
    threat phrase being slightly wrong (it just nudges risk up a little,
    still bounded by ``_LEARNED_TIER``).

## Incrementality / caching

Each channel's highest-seen message id is cached (``VocabularyCache.
progress_by_channel``) so a re-run only fetches messages newer than
that -- "rebuilding everything every time" is exactly what this avoids.
Learned phrases themselves are also cached and re-loaded on startup, so
a phrase discovered once doesn't need re-discovering after a restart.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from app.models.risk_models import ThreatTier
from app.models.vocabulary_models import LearnedPhrase, VocabularyCache
from app.services import message_classifier
from app.services.logger_service import LoggerService
from app.services.threat_vocabulary import CANCEL_MARKERS, FAMILY_KEYWORDS

if TYPE_CHECKING:
    from app.telegram.telegram_service import TelegramService

#: Learned phrases are capped at this tier regardless of how strong their
#: co-occurrence signal is -- see module docstring.
_LEARNED_TIER = ThreatTier.LOW

#: A candidate n-gram needs at least this many total occurrences across
#: the analyzed history before it's considered at all (filters out
#: one-off noise).
_MIN_OCCURRENCES = 4
#: ...and at least this fraction of its occurrences must be alongside a
#: seed keyword/cancel-marker/ignorable-message to be kept as a
#: candidate for that category (filters out generic common words that
#: just happen to co-occur sometimes, e.g. "у", "на", "сьогодні").
_MIN_COOCCURRENCE_RATIO = 0.55

#: How many learned phrases to keep in total, across all families
#: combined -- keeps the extra vocabulary small, reviewable, and cheap
#: to match, not an unbounded pile of loosely-confident guesses.
_MAX_LEARNED_PHRASES = 40
_MAX_LEARNED_IGNORE_PHRASES = 20

#: Stopwords excluded from candidacy outright -- common function words
#: that would otherwise dominate co-occurrence counts without meaning
#: anything on their own.
_STOPWORDS = frozenset(
    {
        "і", "й", "та", "у", "в", "на", "з", "із", "зі", "до", "від", "по", "за", "для",
        "це", "як", "що", "а", "але", "чи", "не", "вже", "ще", "також", "тому",
        "буде", "було", "є", "був", "була", "були", "їх", "його", "її", "їм",
    }
)

_TOKEN_RE = re.compile(r"[а-щьюяіїєґ'\-]+|[a-z0-9\-]+", re.IGNORECASE)

_KNOWN_FAMILY_PHRASES: tuple[str, ...] = tuple(phrase for _, _, phrase, _ in FAMILY_KEYWORDS)


def _tokenize(text: str) -> list[str]:
    """Split already-normalized text into candidate tokens, dropping stopwords/short junk."""
    tokens = _TOKEN_RE.findall(text)
    return [t for t in tokens if len(t) >= 3 and t not in _STOPWORDS]


def _ngrams(tokens: list[str], n: int) -> list[str]:
    return [" ".join(tokens[i : i + n]) for i in range(len(tokens) - n + 1)]


def _candidate_phrases(text: str) -> set[str]:
    """All 1-3 word candidate phrases in one message's normalized text."""
    tokens = _tokenize(text)
    candidates: set[str] = set()
    for n in (1, 2, 3):
        candidates.update(_ngrams(tokens, n))
    return candidates


def _dominant_family_for(text: str) -> str | None:
    """If ``text`` contains a known hand-curated keyword, return its family."""
    for family, _tier, phrase, _whole_word in FAMILY_KEYWORDS:
        if phrase in text:
            return family
    return None


class VocabularyBuilder:
    """Mines new phrase candidates from monitored channels' Telegram history."""

    def __init__(self, telegram_service: "TelegramService", logger: LoggerService) -> None:
        """Create the builder with its Telethon and logging dependencies."""
        self._telegram_service = telegram_service
        self._logger = logger

    async def run_incremental_update(
        self, usernames: list[str], cache: VocabularyCache, messages_per_channel: int = 3000
    ) -> VocabularyCache:
        """Mine new history for ``usernames`` and return an updated cache.

        Only fetches messages newer than each channel's cached progress
        cursor. Existing learned phrases are re-scored alongside any
        newly discovered ones so ranking stays fresh; duplicates
        (including anything already in the hand-curated table) are
        dropped before capping to ``_MAX_LEARNED_PHRASES``.
        """
        known_phrases = set(_KNOWN_FAMILY_PHRASES) | {p.phrase for p in cache.learned_phrases}

        # co_family[phrase][family] = count of messages with that phrase
        # AND that family's seed keyword present
        co_family: dict[str, Counter] = {}
        co_cancel: Counter = Counter()
        co_ignorable: Counter = Counter()
        total_occurrences: Counter = Counter()

        analyzed_count = 0
        new_progress = dict(cache.progress_by_channel)

        for username in usernames:
            last_seen_id = cache.progress_by_channel.get(username, 0)
            highest_id_this_run = last_seen_id
            channel_message_count = 0

            async for message_id, raw_text in self._telegram_service.iter_channel_history(
                username, min_id=last_seen_id, limit=messages_per_channel
            ):
                highest_id_this_run = max(highest_id_this_run, message_id)
                channel_message_count += 1
                analyzed_count += 1

                is_ignorable = message_classifier.is_fully_ignorable(raw_text)
                normalized = (
                    message_classifier.strip_ignorable_segments(raw_text)
                    if not is_ignorable
                    else message_classifier.normalize_for_analysis(raw_text)
                )
                if not normalized:
                    continue

                candidates = _candidate_phrases(normalized)
                if not candidates:
                    continue

                for phrase in candidates:
                    total_occurrences[phrase] += 1

                if is_ignorable:
                    for phrase in candidates:
                        co_ignorable[phrase] += 1
                    continue

                has_cancel = any(marker in normalized for marker in CANCEL_MARKERS)
                family = _dominant_family_for(normalized)

                if has_cancel:
                    for phrase in candidates:
                        co_cancel[phrase] += 1
                elif family is not None:
                    bucket = co_family.setdefault(family, Counter())
                    for phrase in candidates:
                        bucket[phrase] += 1

            new_progress[username] = highest_id_this_run
            if channel_message_count:
                self._logger.info(
                    f"Vocabulary Builder: проаналізовано {channel_message_count} "
                    f"нове(і) повідомлення з '{username}'."
                )

        learned_threat = self._rank_candidates(
            co_family, total_occurrences, known_phrases, exclude={**{p: 1 for p in []}}
        )
        learned_ignore = self._rank_ignore_candidates(co_ignorable, total_occurrences, known_phrases)

        merged_phrases = list(cache.learned_phrases) + learned_threat
        # Keep the highest-scoring instance of each phrase, then cap.
        best_by_phrase: dict[str, LearnedPhrase] = {}
        for lp in merged_phrases:
            existing = best_by_phrase.get(lp.phrase)
            if existing is None or lp.score > existing.score:
                best_by_phrase[lp.phrase] = lp
        ranked_phrases = sorted(best_by_phrase.values(), key=lambda p: p.score, reverse=True)
        ranked_phrases = ranked_phrases[:_MAX_LEARNED_PHRASES]

        merged_ignore = list(dict.fromkeys(list(cache.learned_ignore_phrases) + learned_ignore))
        merged_ignore = merged_ignore[:_MAX_LEARNED_IGNORE_PHRASES]

        if analyzed_count:
            self._logger.info(
                f"Vocabulary Builder: разом проаналізовано {analyzed_count} повідомлень, "
                f"вивчено фраз: {len(ranked_phrases)} (загроза/відбій), "
                f"{len(merged_ignore)} (ігнорувати, лише для перегляду)."
            )

        return VocabularyCache(
            progress_by_channel=new_progress,
            learned_phrases=ranked_phrases,
            learned_ignore_phrases=merged_ignore,
            last_run_at=datetime.now(timezone.utc).isoformat(),
        )

    @staticmethod
    def _rank_candidates(
        co_family: dict[str, Counter],
        total_occurrences: Counter,
        known_phrases: set[str],
        exclude: dict[str, int],
    ) -> list[LearnedPhrase]:
        """Score and filter candidate threat phrases, family by family."""
        results: list[LearnedPhrase] = []
        for family, counter in co_family.items():
            for phrase, co_count in counter.items():
                if phrase in known_phrases or len(phrase.split()) == 0:
                    continue
                total = total_occurrences[phrase]
                if total < _MIN_OCCURRENCES:
                    continue
                ratio = co_count / total
                if ratio < _MIN_COOCCURRENCE_RATIO:
                    continue
                score = co_count * ratio
                results.append(
                    LearnedPhrase(phrase=phrase, family=family, tier_value=_LEARNED_TIER.value, score=score)
                )
        return results

    @staticmethod
    def _rank_ignore_candidates(
        co_ignorable: Counter, total_occurrences: Counter, known_phrases: set[str]
    ) -> list[str]:
        """Score and filter candidate ignore-phrases (donation/ad boilerplate)."""
        scored: list[tuple[float, str]] = []
        for phrase, co_count in co_ignorable.items():
            if phrase in known_phrases:
                continue
            total = total_occurrences[phrase]
            if total < _MIN_OCCURRENCES:
                continue
            ratio = co_count / total
            if ratio < _MIN_COOCCURRENCE_RATIO:
                continue
            scored.append((co_count * ratio, phrase))
        scored.sort(reverse=True)
        return [phrase for _score, phrase in scored]
