"""
Hand-curated threat vocabulary for the risk engine: which words/phrases
indicate which *kind* of event (family), how severe that family is on
its own (base tier), and which separate words indicate how *certain*
the message sounds (status markers) or that a threat has ended.

This replaces ``risk_analyzer.py``'s old flat "keyword -> point value,
sum them all" table. Every keyword that table had (шахед, бпла, ракета,
балістик, калібр, кинджал, міг-31к, вибух, каб, тривога, відбій, ...) is
still here -- reorganized by family/tier, not removed -- plus the tiers
requested for the risk-engine redesign (confirmed ballistic/missile/
Kinzhal/Iskander = very high; MiG-31K/Tu-95 takeoff, cruise missiles,
multiple Shahed groups = high; possible launch/unconfirmed = medium;
monitoring/possible activity = low).

``app.services.vocabulary_builder`` can append additional *learned*
phrases at runtime (see that module) -- those are kept in a separate,
clearly-labeled list at a capped, lower weight than anything hand-curated
here, never overriding it.
"""

from __future__ import annotations

from app.models.risk_models import ThreatTier

FAMILY_BALLISTIC = "ballistic"
FAMILY_CRUISE_MISSILE = "cruise_missile"
FAMILY_SHAHED = "shahed"
FAMILY_AIRCRAFT = "aircraft"
FAMILY_EXPLOSION = "explosion"
FAMILY_LAUNCH_GENERIC = "launch_generic"
FAMILY_ALERT_GENERIC = "alert_generic"
#: Special sentinel used by cancellation markers that aren't tied to one
#: specific family -- e.g. a bare "відбій"/"чисто" with no weapon named.
FAMILY_ALL = "*"

#: (family, base_tier, phrase, whole_word) -- ``whole_word`` mirrors
#: risk_analyzer.py's KeywordRule.whole_word (needed for short acronyms
#: like "каб" that would otherwise also match inside unrelated words).
FAMILY_KEYWORDS: tuple[tuple[str, ThreatTier, str, bool], ...] = (
    # --- Ballistic / Kinzhal / Iskander -- HIGH base, CONFIRMED -> VERY_HIGH
    (FAMILY_BALLISTIC, ThreatTier.HIGH, "балістичн", False),
    (FAMILY_BALLISTIC, ThreatTier.HIGH, "баллистич", False),  # RU spelling
    (FAMILY_BALLISTIC, ThreatTier.HIGH, "балістик", False),
    (FAMILY_BALLISTIC, ThreatTier.HIGH, "кинджал", False),
    (FAMILY_BALLISTIC, ThreatTier.HIGH, "іскандер", False),
    (FAMILY_BALLISTIC, ThreatTier.HIGH, "искандер", False),  # RU spelling
    (FAMILY_BALLISTIC, ThreatTier.HIGH, "iskander", False),
    (FAMILY_BALLISTIC, ThreatTier.HIGH, "міг-31к", False),  # carrier for Kinzhal -- same family
    (FAMILY_BALLISTIC, ThreatTier.HIGH, "ballistic missile", False),
    # --- Cruise missiles -- HIGH base
    (FAMILY_CRUISE_MISSILE, ThreatTier.HIGH, "крилат", False),  # "крилата/крилату ракета/ракету"
    (FAMILY_CRUISE_MISSILE, ThreatTier.HIGH, "крылат", False),  # RU spelling
    (FAMILY_CRUISE_MISSILE, ThreatTier.HIGH, "калібр", False),
    (FAMILY_CRUISE_MISSILE, ThreatTier.HIGH, "калибр", False),  # RU spelling
    (FAMILY_CRUISE_MISSILE, ThreatTier.HIGH, "х-101", False),
    (FAMILY_CRUISE_MISSILE, ThreatTier.HIGH, "х-555", False),
    (FAMILY_CRUISE_MISSILE, ThreatTier.HIGH, "cruise missile", False),
    (FAMILY_CRUISE_MISSILE, ThreatTier.MEDIUM, "ракета", False),  # bare "ракета" -- ambiguous type, medium
    (FAMILY_CRUISE_MISSILE, ThreatTier.MEDIUM, "ракети", False),
    (FAMILY_CRUISE_MISSILE, ThreatTier.MEDIUM, "missile", False),  # bare "missile" -- same ambiguous-type tier
    # --- Shahed / UAV -- MEDIUM base, "групи"/multiple -> HIGH
    (FAMILY_SHAHED, ThreatTier.MEDIUM, "шахед", False),
    (FAMILY_SHAHED, ThreatTier.MEDIUM, "shahed", False),
    (FAMILY_SHAHED, ThreatTier.MEDIUM, "камікадзе", False),
    (FAMILY_SHAHED, ThreatTier.MEDIUM, "бпла", False),
    (FAMILY_SHAHED, ThreatTier.MEDIUM, "uav", False),
    (FAMILY_SHAHED, ThreatTier.MEDIUM, "drone", False),
    (FAMILY_SHAHED, ThreatTier.HIGH, "групи бпла", False),
    (FAMILY_SHAHED, ThreatTier.HIGH, "групи шахед", False),
    (FAMILY_SHAHED, ThreatTier.HIGH, "декілька груп", False),
    # --- Strategic aircraft takeoff -- HIGH base
    (FAMILY_AIRCRAFT, ThreatTier.HIGH, "ту-95", False),
    (FAMILY_AIRCRAFT, ThreatTier.HIGH, "зліт", False),
    (FAMILY_AIRCRAFT, ThreatTier.HIGH, "злетів", False),
    (FAMILY_AIRCRAFT, ThreatTier.MEDIUM, "aircraft", False),
    # --- Explosions / guided bombs -- MEDIUM base
    (FAMILY_EXPLOSION, ThreatTier.MEDIUM, "вибух", False),
    (FAMILY_EXPLOSION, ThreatTier.MEDIUM, "explosion", False),
    (FAMILY_EXPLOSION, ThreatTier.MEDIUM, "каб", True),
    # --- Generic/unspecified launch chatter -- MEDIUM base ("possible launch")
    (FAMILY_LAUNCH_GENERIC, ThreatTier.MEDIUM, "пуски", False),
    (FAMILY_LAUNCH_GENERIC, ThreatTier.MEDIUM, "пуск", False),
    (FAMILY_LAUNCH_GENERIC, ThreatTier.MEDIUM, "курс на", False),
    # --- Generic alert chatter with no named weapon -- LOW base
    # ("monitoring", "possible activity", "waiting for confirmation")
    (FAMILY_ALERT_GENERIC, ThreatTier.LOW, "тривог", False),
    (FAMILY_ALERT_GENERIC, ThreatTier.LOW, "загроза", False),
    (FAMILY_ALERT_GENERIC, ThreatTier.LOW, "монітор", False),
    (FAMILY_ALERT_GENERIC, ThreatTier.LOW, "очікуємо підтвердження", False),
    (FAMILY_ALERT_GENERIC, ThreatTier.LOW, "можлива активність", False),
)

#: Bumps a matched family's tier UP one step (see risk_analyzer.py's
#: ``_step_tier``). "офіційно" alone is deliberately not enough on its
#: own to avoid over-triggering on "офіційно повідомляють" chatter.
CONFIRMED_MARKERS: tuple[str, ...] = (
    "підтверджено",
    "підтверджена інформація",
    "офіційно підтверджено",
    "confirmed",
)

#: Bumps a matched family's tier DOWN one step.
POSSIBLE_MARKERS: tuple[str, ...] = (
    "можливо",
    "можливий",
    "ймовірно",
    "попередньо",
    "не підтверджено",
    "уточнюється",
)

#: A CANCELLED/ALL_CLEAR message alongside one of these family-specific
#: phrases clears just that family; see risk_analyzer.py's ``analyze``.
#: Includes English forms too (this project's parser now also handles
#: English-language channel messages, e.g. "Shahed", "cruise missile").
FAMILY_CANCEL_HINTS: tuple[tuple[str, str], ...] = (
    (FAMILY_SHAHED, "шахед"),
    (FAMILY_SHAHED, "бпла"),
    (FAMILY_SHAHED, "shahed"),
    (FAMILY_SHAHED, "uav"),
    (FAMILY_SHAHED, "drone"),
    (FAMILY_BALLISTIC, "балістичн"),
    (FAMILY_BALLISTIC, "балістик"),
    (FAMILY_BALLISTIC, "кинджал"),
    (FAMILY_BALLISTIC, "ballistic"),
    (FAMILY_CRUISE_MISSILE, "крилат"),
    (FAMILY_CRUISE_MISSILE, "ракет"),
    (FAMILY_CRUISE_MISSILE, "cruise missile"),
    (FAMILY_CRUISE_MISSILE, "missile"),
    (FAMILY_AIRCRAFT, "міг"),
    (FAMILY_AIRCRAFT, "ту-95"),
    (FAMILY_AIRCRAFT, "aircraft"),
    (FAMILY_EXPLOSION, "вибух"),
    (FAMILY_EXPLOSION, "explosion"),
)

#: A cancellation/all-clear message with NONE of the above family hints
#: present clears every currently active family at once (a generic
#: "відбій тривоги"/"чисто" with no weapon named).
#:
#: This includes destruction/interception verbs ("знищено", "збито",
#: "shot down", "destroyed", "intercepted", "eliminated", "neutralized")
#: -- a report that a specific target was destroyed is, for risk
#: purposes, exactly the same kind of thing as an explicit "відбій" for
#: that family: the airborne threat it described is no longer active,
#: so it must never ALSO count as a fresh, additional sighting of that
#: same family (which is what happened before this word list existed --
#: "cruise missile ... destroyed" matched only the "cruise missile"
#: family keyword and nothing here, so it fell through to the
#: fresh-event path and increased risk a second time for a target that
#: had just been eliminated).
CANCEL_MARKERS: tuple[str, ...] = (
    "відбій тривоги",
    "відбій",
    "чисто",
    "загроза минула",
    "небезпека минула",
    "все спокійно",
    "знищено",
    "знищили",
    "знищила",
    "знищив",
    "збито",
    "збили",
    "збила",
    "збив",
    "перехоплено",
    "перехопили",
    "нейтралізовано",
    "нейтралізували",
    "ліквідовано",
    "shot down",
    "destroyed",
    "intercepted",
    "eliminated",
    "neutralized",
)
