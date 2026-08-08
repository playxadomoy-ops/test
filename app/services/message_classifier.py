"""
Pre-classifies Telegram message text before any risk analysis runs.

Telegram channels mix real threat updates with donation asks, ad reads,
greetings, and channel-promotion boilerplate. Feeding all of that into
keyword-based risk scoring is exactly what made the old analyzer noisy:
a message that's 90% "support the channel, here's my Monobank card" and
10% actual content could still match on stray words. This module splits
a message into line/sentence-level chunks, drops the ones that are
themselves ignorable, and hands back only what's left -- if nothing is
left, the whole message is reported as not relevant at all, so it never
creates or reinforces a threat event.

This is intentionally simple pattern matching (the same style as the
rest of this project's analyzers), not a language model -- it will miss
cleverly-worded ads and can occasionally drop a real one-line report
that happens to also mention a bank card in passing. Given the choice
between "misses an edge case" and "requires a network call/heavy
dependency for every incoming message on a phone", this project's
existing rule-based approach was kept, just extended.
"""

from __future__ import annotations

import re

#: Whole line/sentence is dropped if it contains any of these -- each is
#: a stem, matched case-insensitively as a substring (same convention as
#: risk_analyzer.py's KeywordRule).
_IGNORE_PATTERNS: tuple[str, ...] = (
    # Donations / bank details
    "донат", "задонат", "монобанк", "monobank", "картк",  # "картка", "картку", "на картку"
    "реквізит", "patreon", "buy me a coffee", "buymeacoffee", "ko-fi",
    "paypal", "webmoney", "приват24", "privat24", "iban",
    # Channel promotion / subscription asks
    "підписуйтесь", "підписуйся", "підпишись", "підписка на канал",
    "поширте", "поширюйте", "репост цього", "share this", "subscribe",
    "запрошуємо в канал", "приєднуйтесь до каналу", "наш чат", "наша група",
    "реклама", "рекламний", "промокод", "знижка", "sponsor",
    # Greetings / pure filler with no informational content
    "доброго ранку", "доброго дня", "доброго вечора", "на добраніч",
    "гарного дня", "смачної кави", "з днем народження", "вітаємо",
    "дякуємо за підтримку", "дякуємо всім",
)

_IGNORE_RE = re.compile("|".join(re.escape(p) for p in _IGNORE_PATTERNS), re.IGNORECASE)

#: Splits a message into line/sentence-level chunks so a single message
#: that mixes real content and an ignorable plug (e.g. "Шахед на Київ.
#: Підтримати канал: monobank.ua/xxx") can have just the plug removed
#: instead of discarding or keeping the whole thing.
_SEGMENT_SPLIT_RE = re.compile(r"[\n\r]+|(?<=[.!?])\s+")

_URL_RE = re.compile(r"https?://\S+|t\.me/\S+|www\.\S+")
_MENTION_RE = re.compile(r"@\w+")
_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F1E6-\U0001F1FF"
    "\U00002190-\U000021FF"
    "\U00002B00-\U00002BFF"
    "\U0000FE0F"
    "\U0000203C-\U00002049"
    "]+"
)
#: Punctuation stripped during deep normalization. Deliberately does NOT
#: include ``-`` or digits -- both are meaningful inside threat keywords
#: this project matches on ("міг-31к", "х-101", "ту-95"), so stripping
#: them would break keyword matching rather than clean it up.
_PUNCTUATION_RE = re.compile(r"[.,!?:;\"'«»()\[\]{}՚՛‘’“”…\\/|+*#%^&]")

#: A handful of common Russian spellings of threat-relevant terms, seen
#: when a monitored channel posts in Russian rather than Ukrainian --
#: normalized to their Ukrainian equivalent so a single vocabulary
#: (threat_vocabulary.py) can match either. This is a small, explicit,
#: hand-verified list of base word forms (not a full grammatical
#: conjugator) -- it covers the common cases actually seen in practice,
#: not every possible inflection.
_RU_TO_UA_TERMS: tuple[tuple[str, str], ...] = (
    ("тревога", "тривога"),
    ("тревогу", "тривогу"),
    ("тревоги", "тривоги"),
    ("отбой", "відбій"),
    ("взрыв", "вибух"),
    ("взрывы", "вибухи"),
    ("угроза", "загроза"),
    ("опасность", "небезпека"),
    ("миновала", "минула"),
    ("подтверждено", "підтверджено"),
    ("возможно", "можливо"),
    ("вероятно", "ймовірно"),
    ("чисто", "чисто"),
    ("спокойно", "спокійно"),
    ("ракета", "ракета"),
    ("ракеты", "ракети"),
)
_RU_TO_UA_RE = re.compile(
    "|".join(rf"\b{re.escape(ru)}\b" for ru, _ in _RU_TO_UA_TERMS), re.IGNORECASE
)
_RU_TO_UA_MAP = dict(_RU_TO_UA_TERMS)


def clean_text(text: str) -> str:
    """Strip links, @mentions, and emoji -- used before both scoring and vocabulary mining."""
    text = _URL_RE.sub(" ", text)
    text = _MENTION_RE.sub(" ", text)
    text = _EMOJI_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_for_analysis(text: str) -> str:
    """Full normalization pass: lowercase, RU->UA terms, strip punctuation, collapse spaces.

    Applied to whatever text survives :func:`strip_ignorable_segments`,
    right before it reaches keyword matching (risk_analyzer.py) or
    vocabulary mining (vocabulary_builder.py) -- so both consume text in
    the same normalized shape.
    """
    text = clean_text(text).lower()
    text = _RU_TO_UA_RE.sub(lambda m: _RU_TO_UA_MAP[m.group(0).lower()], text)
    text = _PUNCTUATION_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def strip_ignorable_segments(text: str) -> str:
    """Return ``text`` with donation/ad/greeting/promo segments removed and normalized.

    Splits on newlines and sentence boundaries (before punctuation is
    stripped, since periods/newlines are what define those boundaries),
    drops any segment that matches an ignore pattern, rejoins what's
    left, then runs it through :func:`normalize_for_analysis`. A message
    that's ignorable end-to-end comes back as an empty string.
    """
    cleaned = clean_text(text)
    if not cleaned:
        return ""

    segments = [s.strip() for s in _SEGMENT_SPLIT_RE.split(cleaned) if s.strip()]
    kept = [s for s in segments if not _IGNORE_RE.search(s)]
    return normalize_for_analysis(" ".join(kept))


def is_fully_ignorable(text: str) -> bool:
    """True if, after stripping ignorable segments, nothing meaningful remains."""
    return strip_ignorable_segments(text) == ""
