"""
Gazetteer of Ukrainian city/town names to (lat, lon) coordinates.

Used exclusively to resolve a place name that a Telegram message
*explicitly states* (e.g. "з Броварів", "курс на Кременчук") into a map
position. This module performs no inference of its own -- it is a plain
lookup table. If a mentioned place isn't in this table, the movement is
still recorded and shown in the side list, just without a map position
for that point (see ``app.services.movement_parser``).

Keys are lowercase, stripped of any leading "м." / "с." prefix and of
common Ukrainian case endings is handled by the parser (via
``resolve_city``), not here -- this module only holds nominative-case
canonical names and their coordinates.
"""

from __future__ import annotations

import re
from typing import Optional

#: name (nominative case, lowercase) -> (latitude, longitude)
CITY_COORDINATES: dict[str, tuple[float, float]] = {
    # Oblast centers / major cities
    "київ": (50.4501, 30.5234),
    "вінниця": (49.2331, 28.4682),
    "луцьк": (50.7472, 25.3254),
    "дніпро": (48.4647, 35.0462),
    "донецьк": (48.0159, 37.8028),
    "житомир": (50.2547, 28.6587),
    "ужгород": (48.6208, 22.2879),
    "запоріжжя": (47.8388, 35.1396),
    "івано-франківськ": (48.9226, 24.7111),
    "кропивницький": (48.5079, 32.2623),
    "луганськ": (48.5740, 39.3078),
    "львів": (49.8397, 24.0297),
    "миколаїв": (46.9750, 31.9946),
    "одеса": (46.4825, 30.7233),
    "полтава": (49.5883, 34.5514),
    "рівне": (50.6199, 26.2516),
    "суми": (50.9077, 34.7981),
    "тернопіль": (49.5535, 25.5948),
    "харків": (49.9935, 36.2304),
    "херсон": (46.6354, 32.6169),
    "хмельницький": (49.4229, 26.9871),
    "черкаси": (49.4444, 32.0598),
    "чернівці": (48.2921, 25.9358),
    "чернігів": (51.4982, 31.2893),
    "сімферополь": (44.9521, 34.1024),
    "севастополь": (44.6166, 33.5254),
    # Kyiv oblast satellites (very frequently named in shahed/missile alerts)
    "бровари": (50.5108, 30.7909),
    "вишгород": (50.5859, 30.4922),
    "бориспіль": (50.3536, 30.9546),
    "проців": (50.2352, 30.7861),
    "вороньків": (50.2149, 30.9001),
    "біла церква": (49.7950, 30.1310),
    "васильків": (50.1806, 30.3236),
    "фастів": (50.0770, 29.9128),
    "буча": (50.5442, 30.2117),
    "ірпінь": (50.5210, 30.2517),
    "обухів": (50.1094, 30.6217),
    "українка": (50.0472, 30.6825),
    "яготин": (50.2597, 31.7686),
    "переяслав": (50.0592, 31.4517),
    "богуслав": (49.5450, 30.8814),
    "миронівка": (49.6672, 31.0142),
    # Poltava / Cherkasy area
    "кременчук": (49.0669, 33.4247),
    "лубни": (50.0159, 32.9908),
    "миргород": (49.9689, 33.6083),
    "золотоноша": (49.6739, 32.0367),
    "сміла": (49.2222, 31.8828),
    "умань": (48.7481, 30.2222),
    "знам'янка": (48.7128, 32.6614),
    "гадяч": (50.3667, 34.0000),
    # Sumy / Chernihiv area (frequent drone/missile transit)
    "конотоп": (51.2417, 33.2022),
    "ромни": (50.7497, 33.4747),
    "охтирка": (50.3103, 34.8975),
    "шостка": (51.8636, 33.4794),
    "прилуки": (50.5942, 32.3906),
    "ніжин": (51.0475, 31.8867),
    "бахмач": (51.1794, 32.8489),
    "новгород-сіверський": (52.0208, 33.2622),
    "корюківка": (51.7647, 32.2531),
    # Kharkiv oblast
    "ізюм": (49.2075, 37.2508),
    "чугуїв": (49.8375, 36.6864),
    "лозова": (48.8886, 36.3186),
    "куп'янськ": (49.7139, 37.6156),
    "балаклія": (49.4581, 36.8567),
    "богодухів": (50.1656, 35.5231),
    # Dnipropetrovsk / Zaporizhzhia area
    "павлоград": (48.5350, 35.8686),
    "кривий ріг": (47.9105, 33.3918),
    "нікополь": (47.5734, 34.3936),
    "мелітополь": (46.8489, 35.3675),
    "бердянськ": (46.7581, 36.7883),
    "енергодар": (47.4964, 34.6564),
    "марганець": (47.6428, 34.6178),
    "кам'янське": (48.5111, 34.6019),
    "покров": (47.6786, 34.0997),
    "новомосковськ": (48.6294, 35.2214),
    "синельникове": (48.3181, 35.5106),
    "васильківка": (48.2464, 36.0517),
    # Donetsk / Luhansk area (front-line, frequently named)
    "маріуполь": (47.0971, 37.5434),
    "слов'янськ": (48.8681, 37.6053),
    "краматорськ": (48.7389, 37.5844),
    "бахмут": (48.5956, 37.9989),
    "покровськ": (48.2789, 37.1758),
    "мирноград": (48.2517, 37.2072),
    "костянтинівка": (48.5453, 37.7053),
    "лиман": (48.9819, 37.7994),
    "сєвєродонецьк": (48.9483, 38.4931),
    "лисичанськ": (48.9139, 38.4436),
    "старобільськ": (49.2758, 38.9022),
    "рубіжне": (49.0208, 38.3775),
    # Kherson / Mykolaiv area
    "нова каховка": (46.7519, 33.3711),
    "каховка": (46.7897, 33.4986),
    "берислав": (46.8378, 33.4372),
    "скадовськ": (46.1150, 32.9169),
    "очаків": (46.6117, 31.5442),
    "первомайськ": (48.0453, 30.8536),
    "вознесенськ": (47.5619, 31.3308),
    "южноукраїнськ": (47.8153, 31.1728),
    # Odesa area
    "ізмаїл": (45.3517, 28.8361),
    "білгород-дністровський": (46.1928, 30.3453),
    "подільськ": (47.7397, 29.5325),
    "чорноморськ": (46.3050, 30.6489),
    "южне": (46.6317, 31.1911),
    # Vinnytsia / Khmelnytskyi / western oblasts
    "жмеринка": (49.0403, 28.1067),
    "могилів-подільський": (48.4444, 27.7981),
    "тульчин": (48.6789, 28.8433),
    "козятин": (49.7147, 28.8264),
    "шепетівка": (50.1811, 27.0631),
    "кам'янець-подільський": (48.6786, 26.5811),
    "старокостянтинів": (49.7514, 27.2011),
    "славута": (50.3011, 26.8656),
    "коростень": (50.9508, 28.6353),
    "новоград-волинський": (50.5931, 27.6231),
    "звягель": (50.5931, 27.6231),
    "сарни": (51.3378, 26.6006),
    "костопіль": (50.8919, 26.4436),
    "дубно": (50.4211, 25.7472),
    "кременець": (50.1075, 25.7256),
    "чортків": (49.0161, 25.7942),
    "бучач": (49.0619, 25.3897),
    "калуш": (49.0206, 24.3717),
    "коломия": (48.5286, 25.0378),
    "долина": (48.9636, 24.0006),
    "стрий": (49.2597, 23.8508),
    "дрогобич": (49.3494, 23.5075),
    "самбір": (49.5178, 23.1961),
    "червоноград": (50.3833, 24.2333),
    "мукачево": (48.4450, 22.7153),
    "хуст": (48.1719, 23.2986),
    "берегове": (48.2081, 22.6431),
    # Zhytomyr / Rivne satellites
    "малин": (50.7742, 29.2436),
    "олевськ": (51.2222, 27.6600),
    "бердичів": (49.8983, 28.5967),
    "радомишль": (50.4964, 29.2372),
}


#: Explicit alternate/inflected forms for names where Ukrainian's "fleeting
#: vowel" declension pattern (і -> о/е) changes the stem itself, so simple
#: prefix matching in ``resolve_city`` can't bridge them (e.g. Бориспіль ->
#: Борисполя). Kept as a short, explicit, auditable table rather than a
#: language model -- every entry here is a real, known place name.
_ALIASES: dict[str, str] = {
    "борисполя": "бориспіль",
    "борисполем": "бориспіль",
    "чернігова": "чернігів",
    "чернігову": "чернігів",
    "тернополя": "тернопіль",
    "тернополем": "тернопіль",
    "ужгорода": "ужгород",
    "кременця": "кременець",
    "харкова": "харків",
    "харкову": "харків",
    "харковом": "харків",
    "києва": "київ",
    "києву": "київ",
    "києвом": "київ",
    "львова": "львів",
    "львову": "львів",
    "кропивницького": "кропивницький",
    "хмельницького": "хмельницький",
    "новомосковська": "новомосковськ",
    "покровська": "покровськ",
    "мирнограда": "мирноград",
    "краматорська": "краматорськ",
    "слов'янська": "слов'янськ",
    "сєвєродонецька": "сєвєродонецьк",
    "лисичанська": "лисичанськ",
    "рубіжного": "рубіжне",
    "енергодара": "енергодар",
    "нікополя": "нікополь",
    "мелітополя": "мелітополь",
    "бердянська": "бердянськ",
    "маріуполя": "маріуполь",
    "ізюма": "ізюм",
    "чугуєва": "чугуїв",
    "лозової": "лозова",
    "куп'янська": "куп'янськ",
    "херсона": "херсон",
    "миколаєва": "миколаїв",
    "одеси": "одеса",
    "дніпра": "дніпро",
    "полтави": "полтава",
    "сум": "суми",
    "рівного": "рівне",
    "луцька": "луцьк",
    "вінниці": "вінниця",
    "житомира": "житомир",
    "черкас": "черкаси",
    "чернівців": "чернівці",
}


#: Official Ukrainian Latin transliteration table (Resolution No. 55,
#: 2010), applied letter-by-letter. Used only to derive a Latin spelling
#: for every name already in ``CITY_COORDINATES`` (see ``_LATIN_INDEX``
#: below) so a message written in English/Latin script ("Boryspil",
#: "Bila Tserkva", "Kyiv") can resolve against this same gazetteer
#: without a second, separately-maintained place-name table. This is
#: purely a spelling transform, not a new source of place data -- it can
#: never resolve a name that isn't already a real gazetteer entry.
_TRANSLIT_TABLE: dict[str, str] = {
    "а": "a", "б": "b", "в": "v", "г": "h", "ґ": "g", "д": "d", "е": "e",
    "є": "ie", "ж": "zh", "з": "z", "и": "y", "і": "i", "ї": "i", "й": "i",
    "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
    "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts", "ч": "ch",
    "ш": "sh", "щ": "shch", "ю": "iu", "я": "ia", "ь": "", "'": "", "’": "",
}


def _transliterate(cyrillic: str) -> str:
    """Render one lowercase Cyrillic place name in Latin script.

    Applied per-character via ``_TRANSLIT_TABLE``; spaces/hyphens pass
    through unchanged so multi-word ("біла церква" -> "bila tserkva")
    and hyphenated ("івано-франківськ" -> "ivano-frankivsk") names stay
    recognizable as the same shape in both scripts.
    """
    return "".join(_TRANSLIT_TABLE.get(ch, ch) for ch in cyrillic)


#: name (Latin transliteration, lowercase) -> canonical Cyrillic gazetteer
#: key. Built once from ``CITY_COORDINATES`` at import time -- every city
#: this app already knows about automatically gets a Latin-script match,
#: so adding a new city to the Cyrillic table above is enough; nothing
#: needs to be hardcoded twice.
_LATIN_INDEX: dict[str, str] = {
    _transliterate(cyrillic_key): cyrillic_key for cyrillic_key in CITY_COORDINATES
}

_LATIN_NAME_PATTERN = re.compile(r"^[a-z][a-z\s'\-]*$")


def _resolve_latin_city_key(name: str) -> Optional[str]:
    """Same idea as ``_resolve_city_key``, but for a Latin-script input.

    Tries an exact match against ``_LATIN_INDEX`` first, then falls back
    to the same conservative shared-prefix fuzzy match used for Cyrillic
    input (to absorb minor spelling variants like "Kyiv"/"Kiev" or a
    dropped apostrophe), never inventing a place outside the gazetteer.
    """
    key = name.strip().lower()
    if key in _LATIN_INDEX:
        return _LATIN_INDEX[key]

    if len(key) < 4:
        return None  # too short for a safe fuzzy match

    best_match: Optional[str] = None
    best_shared = 0
    for candidate_latin, candidate_cyrillic in _LATIN_INDEX.items():
        if len(candidate_latin) < 4:
            continue
        shared = _shared_prefix_length(key, candidate_latin)
        if (
            len(key) - shared <= 2
            and len(candidate_latin) - shared <= 2
            and shared > best_shared
        ):
            best_shared = shared
            best_match = candidate_cyrillic

    return best_match


def _resolve_city_key(name: str) -> Optional[str]:
    """Look up a raw, possibly-inflected city name and return its
    canonical (nominative-case, lowercase) gazetteer key, or ``None``.

    Ukrainian grammar inflects place names by case ("з Броварів", "у
    напрямку Борисполя", "на Кременчук"), so an exact match against this
    gazetteer's nominative-case keys often misses. This tries an exact
    match first, then a small explicit alias table for names with
    vowel-changing declension, then falls back to matching by shared word
    stem (longest common prefix, ignoring the last 1-2 letters where
    simple case endings live). This is a plain string-similarity
    fallback, not a language model, and it never invents a place: if
    nothing in the gazetteer shares a long enough stem, it returns
    ``None`` and the caller must not guess. Shared by both
    ``resolve_city`` (coordinates) and ``resolve_city_name`` (display
    name) so the matching logic exists in exactly one place.
    """
    key = name.strip().lower()
    key = key.removeprefix("м. ").removeprefix("с. ").removeprefix("смт ")
    if key in CITY_COORDINATES:
        return key
    if key in _ALIASES:
        return _ALIASES[key]

    if _LATIN_NAME_PATTERN.match(key):
        return _resolve_latin_city_key(key)

    if len(key) < 4:
        return None  # too short for a safe fuzzy match

    best_match: Optional[str] = None
    best_shared = 0
    for candidate_name in CITY_COORDINATES:
        if len(candidate_name) < 4:
            continue
        shared = _shared_prefix_length(key, candidate_name)
        # Both words must be within 2 characters of the shared stem -- not
        # just the shorter one -- so a short city name can't trivially
        # "match" as a mere prefix of a much longer, different word (e.g.
        # "херсон" must NOT match "херсонщини", the oblast name).
        if (
            len(key) - shared <= 2
            and len(candidate_name) - shared <= 2
            and shared > best_shared
        ):
            best_shared = shared
            best_match = candidate_name

    if best_match is not None:
        return best_match

    # A large family of Ukrainian settlement names ends in "-ів"
    # (a possessive-adjective form, e.g. "Вороньків", "Васильків",
    # "Фастів") and declines like an adjective rather than a regular
    # noun: the oblique-case stem drops that "і" and adds "ов" before
    # the case ending (genitive "з Воронькова", "з Василькова", "з
    # Фастова") -- a bigger, non-suffix change the plain shared-prefix
    # match above can't bridge, which is why it was previously missing
    # even common satellite towns around Kyiv routinely named in real
    # shahed/missile reports. Handled generally here (try every "-ів"
    # gazetteer entry under its "-ов" stem) rather than one hardcoded
    # alias per town, so any such name -- not just a pre-listed few --
    # resolves.
    for candidate_name in CITY_COORDINATES:
        if not candidate_name.endswith("ів") or len(candidate_name) < 4:
            continue
        declined_stem = candidate_name[:-2] + "ов"
        shared = _shared_prefix_length(key, declined_stem)
        if (
            shared == len(declined_stem)
            and len(key) - shared <= 3  # room for "а"/"у"/"им"/"ому" endings
        ):
            return candidate_name

    return None


def resolve_city(name: str) -> Optional[tuple[float, float]]:
    """Look up a raw, possibly-inflected city name and return (lat, lon).

    See ``_resolve_city_key`` for the matching rules -- this is just that
    lookup's coordinates. Unchanged public behavior/signature.
    """
    key = _resolve_city_key(name)
    return CITY_COORDINATES[key] if key is not None else None


def resolve_city_name(name: str) -> Optional[str]:
    """Look up a raw, possibly-inflected city name and return a
    presentable, capitalized canonical name (e.g. "борисполя" -> "Бориспіль"),
    or ``None`` if it doesn't resolve.

    Added for movement routes to show a real settlement name ("Бориспіль
    → Київ") instead of the raw inflected text matched out of the
    message ("борисполя" / "києва"). Same "never invents a place" rule
    as ``resolve_city`` -- this is the canonical key for the exact same
    match ``resolve_city`` would make for ``name``, not a separate guess.
    """
    key = _resolve_city_key(name)
    return key.capitalize() if key is not None else None


def _shared_prefix_length(a: str, b: str) -> int:
    """Return how many leading characters two strings have in common."""
    count = 0
    for ca, cb in zip(a, b):
        if ca != cb:
            break
        count += 1
    return count
