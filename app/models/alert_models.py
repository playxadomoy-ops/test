"""
Data models describing Ukraine's regions and their alert state.

No dictionaries are used where a typed model is appropriate, per the
project's data-handling rules.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class Region(str, Enum):
    """The 27 first-level administrative regions tracked by the app."""

    CHERKASY = "Черкаська область"
    CHERNIHIV = "Чернігівська область"
    CHERNIVTSI = "Чернівецька область"
    CRIMEA = "Автономна Республіка Крим"
    DNIPRO = "Дніпропетровська область"
    DONETSK = "Донецька область"
    IVANO_FRANKIVSK = "Івано-Франківська область"
    KHARKIV = "Харківська область"
    KHERSON = "Херсонська область"
    KHMELNYTSKYI = "Хмельницька область"
    KYIV_OBLAST = "Київська область"
    KYIV_CITY = "м. Київ"
    KIROVOHRAD = "Кіровоградська область"
    LUHANSK = "Луганська область"
    LVIV = "Львівська область"
    MYKOLAIV = "Миколаївська область"
    ODESA = "Одеська область"
    POLTAVA = "Полтавська область"
    RIVNE = "Рівненська область"
    SEVASTOPOL = "м. Севастополь"
    SUMY = "Сумська область"
    TERNOPIL = "Тернопільська область"
    VINNYTSIA = "Вінницька область"
    VOLYN = "Волинська область"
    ZAKARPATTIA = "Закарпатська область"
    ZAPORIZHZHIA = "Запорізька область"
    ZHYTOMYR = "Житомирська область"


class RiskLevel(str, Enum):
    """Discrete danger levels used to color the UI and the map."""

    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def label_uk(self) -> str:
        """Human readable Ukrainian label for this risk level."""
        labels = {
            RiskLevel.NONE: "Загроза відсутня",
            RiskLevel.LOW: "Низький рівень",
            RiskLevel.MEDIUM: "Середній рівень",
            RiskLevel.HIGH: "Високий рівень",
            RiskLevel.CRITICAL: "Критичний рівень",
        }
        return labels[self]

    @property
    def short_label_uk(self) -> str:
        """Compact one-word status used on the main dashboard card."""
        labels = {
            RiskLevel.NONE: "Чисто",
            RiskLevel.LOW: "Уважно",
            RiskLevel.MEDIUM: "Небезпечно",
            RiskLevel.HIGH: "Загроза",
            RiskLevel.CRITICAL: "Критично",
        }
        return labels[self]


class ApiStatus(str, Enum):
    """Status of the official alerts.in.ua per-oblast data source.

    Kept distinct from "no active alerts" so the UI never shows a false
    all-clear ("Чисто") when the official source simply hasn't answered.
    """

    NOT_CONFIGURED = "not_configured"  # no API token entered -- Telegram-only mode, by design
    OK = "ok"                          # last request succeeded and was parsed
    ERROR = "error"                    # last request failed or could not be parsed

    @property
    def label_uk(self) -> str:
        """Human readable Ukrainian label for this status."""
        labels = {
            ApiStatus.NOT_CONFIGURED: "Токен alerts.in.ua не налаштовано (лише Telegram-аналіз)",
            ApiStatus.OK: "Дані отримано",
            ApiStatus.ERROR: "Дані недоступні",
        }
        return labels[self]


@dataclass(slots=True)
class RegionState:
    """Current alert state of a single region."""

    region: Region
    is_active: bool = False
    risk_level: RiskLevel = RiskLevel.NONE
    last_changed: datetime = field(default_factory=datetime.now)
    note: str = ""


@dataclass(slots=True)
class ThreatSnapshot:
    """A point-in-time summary of the overall national threat picture."""

    overall_risk: RiskLevel
    risk_score: float  # 0.0 .. 100.0
    active_regions_count: int
    total_messages_analyzed: int
    last_update: datetime = field(default_factory=datetime.now)
    api_status: ApiStatus = ApiStatus.NOT_CONFIGURED
    api_error_message: str = ""
    #: Whether there is currently an active air threat, at all -- a plain
    #: yes/no signal, deliberately independent of ``risk_score``/
    #: ``overall_risk``. True if the official alerts.in.ua source reports
    #: at least one active oblast, OR the Telegram-derived analyzer
    #: currently tracks at least one non-expired threat event -- see
    #: ``AlertService.snapshot()``. This exists so the UI can show "is
    #: there a threat right now" (Indicator 1) as a separate question
    #: from "how dangerous does it look" (Indicator 2 / risk_score),
    #: per the two-indicator redesign: a LOW-risk event still means
    #: ``has_active_threat=True``, while risk_score can be elevated by a
    #: not-yet-fully-decayed event even after it stops being "active".
    has_active_threat: bool = False
