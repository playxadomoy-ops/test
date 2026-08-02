"""
Central coordination service for the national threat picture.

Combines two independent signals into one ``ThreatSnapshot``:
  1. the official alerts.in.ua per-oblast air-raid status (when the user
     has configured an API token), and
  2. a small set of "currently active threat events" derived from
     structured Telegram-message analysis (see
     :mod:`app.services.risk_analyzer`) -- not a single incrementally-
     summed score.

Each active event tracks: which *family* of threat it is (shahed,
ballistic, cruise missile, aircraft, explosion, ...), its current
severity tier, which distinct channels have corroborated it, when it
was first/last reported, and decays and expires on its own if nothing
reinforces it. The overall Telegram-derived risk is the strongest
*currently effective* active event, not a running total of every
message ever seen -- this is the redesign requested to fix risk being
driven by message *count* rather than the actual current situation.

Network or parsing failures on either signal are logged and degrade
gracefully — they never raise out of this service.
"""

from __future__ import annotations

import traceback
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

import httpx

from app.config import ALERTS_OBLAST_ORDER, ALERTS_STATUS_URL, DEFAULTS
from app.models.alert_models import ApiStatus, Region, RegionState, RiskLevel, ThreatSnapshot
from app.models.risk_models import EventFamily, MessageAnalysis, ThreatStatus, ThreatTier
from app.services.logger_service import LoggerService
from app.services.risk_analyzer import RiskAnalyzer
from app.services.threat_vocabulary import FAMILY_ALL

OnSnapshotChanged = Callable[[ThreatSnapshot, dict], None]
#: Called with the list of watched regions that just transitioned from
#: "no alert" to "air raid alert" in a single refresh (see
#: ``_apply_api_payload`` / ``set_watched_regions``) -- never for a
#: region leaving alert, never repeatedly while a region stays alerted,
#: and only once even if several watched regions flip together.
OnRegionAlertTriggered = Callable[[list[Region]], None]

#: How many *distinct* channels must independently report the same
#: event family within its lifetime to reach full confidence. Capped at
#: this count -- a 4th, 5th, etc. corroborating channel doesn't push it
#: any further. One single channel alone caps below 100% of its tier's
#: severity (see CONFIDENCE_BY_CHANNEL_COUNT) -- this is the concrete
#: mechanism behind "one message should never immediately produce
#: 80-100% risk" and "multiple trusted confirmations should increase
#: confidence".
_MAX_CORROBORATION_CHANNELS = 3
CONFIDENCE_BY_CHANNEL_COUNT: dict[int, float] = {1: 0.62, 2: 0.85, 3: 1.0}

#: If nothing reinforces an active event for this long, it's fully
#: decayed and removed -- an old spike doesn't stay elevated forever
#: just because nothing explicitly cancelled it. Higher-severity tiers
#: get a longer window (a confirmed ballistic launch stays relevant
#: longer than a vague "possible activity" mention) -- per "improve
#: event expiration so old threats disappear naturally".
_EVENT_EXPIRY_SECONDS = 1200.0
_EVENT_EXPIRY_SECONDS_BY_TIER: dict[ThreatTier, float] = {
    ThreatTier.VERY_HIGH: 1800.0,
    ThreatTier.HIGH: 1200.0,
    ThreatTier.MEDIUM: 720.0,
    ThreatTier.LOW: 420.0,
}

#: Status characters returned by the alerts.in.ua compact oblast endpoint.
#: "A" = full-oblast air raid alert. "P" = partial (raion/hromada level)
#: alert -- treated as active too, since it still means real danger
#: somewhere in that oblast. "N" = no alert.
_ACTIVE_STATUS_CHARS = frozenset({"A", "P"})


@dataclass(slots=True)
class ActiveThreatEvent:
    """One currently-tracked threat event for a given family.

    Exposed (read-only in practice) mainly so the Лог/diagnostics can
    describe what's driving the current risk score if ever needed --
    the UI itself only ever reads the aggregated ``ThreatSnapshot``.
    """

    family: EventFamily
    tier: ThreatTier
    status: ThreatStatus
    first_seen: datetime
    last_seen: datetime
    #: Every channel that has ever mentioned this event -- informational
    #: (used for logging), NOT what drives confidence; see
    #: ``distinct_reports`` below for why.
    corroborating_channels: set[str] = field(default_factory=set)
    #: Distinct message *fingerprints* seen for this event. Confidence is
    #: driven by this, not by channel count: two channels relaying the
    #: exact same forwarded text are one report, not two independent
    #: corroborations -- "merge duplicate reports from different
    #: channels" from the redesign brief. Only genuinely different
    #: wording (a different observer describing the same event in their
    #: own words) increases confidence.
    distinct_reports: set[str] = field(default_factory=set)

    def effective_score(self, now: datetime) -> float:
        """This event's current contribution to overall risk, 0..tier value.

        Driven ONLY by two things: (1) how many genuinely distinct
        messages have corroborated this event so far (``confidence``,
        via ``distinct_reports`` -- grows only when
        ``AlertService.apply_message_analysis()`` is called for a new
        message/report, never on its own), and (2) how long it's been
        since the last report (``decay`` -- can only ever pull the score
        DOWN over time, never up). There is deliberately no separate
        time-since-first-seen "ramp" factor here anymore: an earlier
        version scaled the score up over the first few minutes purely
        from the clock ticking, which meant risk kept climbing on its
        own (e.g. 30 -> 31 -> 32 -> ...) even with zero new Telegram
        messages or API data -- a wall-clock-driven increase, not an
        information-driven one. "One message shouldn't immediately
        produce 80-100% risk" is already fully satisfied by
        ``confidence`` alone (a single report is capped at 62% of the
        tier's target, see CONFIDENCE_BY_CHANNEL_COUNT) without needing
        a second, buggy, time-based multiplier on top of it.
        """
        target = self.tier.value
        if target <= 0.0:
            return 0.0

        report_count = min(len(self.distinct_reports) or 1, _MAX_CORROBORATION_CHANNELS)
        confidence = CONFIDENCE_BY_CHANNEL_COUNT.get(report_count, 0.5)

        expiry_seconds = _EVENT_EXPIRY_SECONDS_BY_TIER.get(self.tier, _EVENT_EXPIRY_SECONDS)
        silence_seconds = max(0.0, (now - self.last_seen).total_seconds())
        decay = max(0.0, 1.0 - silence_seconds / expiry_seconds)

        return target * confidence * decay

    def is_expired(self, now: datetime) -> bool:
        return self.effective_score(now) <= 0.01


class AlertService:
    """Owns the current region states and the aggregated threat snapshot."""

    def __init__(self, logger: LoggerService, risk_analyzer: RiskAnalyzer) -> None:
        """Create the service with its logging and analysis dependencies."""
        self._logger = logger
        self._risk_analyzer = risk_analyzer
        self._region_states: dict[Region, RegionState] = {
            region: RegionState(region=region) for region in Region
        }
        self._active_events: dict[EventFamily, ActiveThreatEvent] = {}
        self._messages_analyzed: int = 0
        self._listener: Optional[OnSnapshotChanged] = None
        self._last_update: datetime = datetime.now()
        self._api_status: ApiStatus = ApiStatus.NOT_CONFIGURED
        self._api_error_message: str = ""
        #: Which regions should trigger a sound notification when they
        #: enter alert -- the same set of oblasts the user picks (either
        #: from the Рух загроз chips or Налаштування's checkboxes; both
        #: write through to the same AppSettings.watched_regions). Empty
        #: means no region triggers a notification, regardless of
        #: ``notifications_enabled`` (that switch is checked by the
        #: caller in main.py, not here).
        self._watched_regions: set[Region] = set()
        self._on_region_alert: Optional[OnRegionAlertTriggered] = None

    def set_listener(self, listener: Optional[OnSnapshotChanged]) -> None:
        """Register a single callback invoked whenever the snapshot changes."""
        self._listener = listener

    def set_watched_regions(self, regions: set[Region]) -> None:
        """Configure which regions should trigger a region-alert notification."""
        self._watched_regions = set(regions)

    def set_region_alert_listener(self, listener: Optional[OnRegionAlertTriggered]) -> None:
        """Register the callback fired when a watched region enters alert.

        Single slot, like :meth:`set_listener` -- assigned exactly once
        in main.py, so there is no possibility of the same transition
        firing more than once through duplicate registrations.
        """
        self._on_region_alert = listener

    @property
    def region_states(self) -> dict[Region, RegionState]:
        """Return the current per-region alert states."""
        return dict(self._region_states)

    def snapshot(self) -> ThreatSnapshot:
        """Build a fresh :class:`ThreatSnapshot` from the current state."""
        active_count = sum(1 for state in self._region_states.values() if state.is_active)
        api_component = min(100.0, active_count * 12.0)
        telegram_component = self._current_telegram_risk_score()
        overall_score = max(telegram_component, api_component)
        overall_score = self._risk_analyzer.clamp_score(overall_score)

        # Indicator 1 ("Статус загрози"): a plain yes/no, independent of
        # the calculated risk score -- true the moment either signal says
        # something is currently active, regardless of how severe.
        now = datetime.now()
        has_active_event = any(
            not event.is_expired(now) for event in self._active_events.values()
        )
        has_active_threat = active_count > 0 or has_active_event

        return ThreatSnapshot(
            overall_risk=self._risk_analyzer.score_to_level(overall_score),
            risk_score=overall_score,
            active_regions_count=active_count,
            total_messages_analyzed=self._messages_analyzed,
            last_update=self._last_update,
            api_status=self._api_status,
            api_error_message=self._api_error_message,
            has_active_threat=has_active_threat,
        )

    def _current_telegram_risk_score(self) -> float:
        """The strongest currently-active event's effective score, or 0 if none.

        Deliberately the MAX across active events, not their sum -- per
        the redesign, risk reflects the single most serious thing
        happening right now, not an accumulation of everything reported
        recently (that accumulation is exactly what made one quiet
        channel's rumor stack with an unrelated real event before).
        """
        now = datetime.now()
        if not self._active_events:
            return 0.0
        return max(event.effective_score(now) for event in self._active_events.values())

    @property
    def active_events(self) -> dict[EventFamily, ActiveThreatEvent]:
        """Read-only view of currently tracked threat events, for diagnostics/logging."""
        return dict(self._active_events)

    def apply_message_analysis(self, analysis: MessageAnalysis, channel_username: str) -> None:
        """Fold in one message's structured analysis (see risk_analyzer.py).

        Messages the classifier found fully irrelevant (ads, donations,
        greetings, ...) still count toward ``total_messages_analyzed``
        (they WERE analyzed -- that's how their irrelevance was
        determined) but never create or touch an active event.
        """
        self._messages_analyzed += 1
        self._last_update = datetime.now()

        if not analysis.is_relevant:
            self._notify()
            return

        now = datetime.now()

        if analysis.status in (ThreatStatus.CANCELLED, ThreatStatus.ALL_CLEAR):
            if analysis.family == FAMILY_ALL:
                cleared = len(self._active_events)
                self._active_events.clear()
                if cleared:
                    self._logger.info(
                        f"Ризик: загальний відбій/чисто -- скинуто {cleared} активну(і) подію(ї)."
                    )
            elif analysis.family in self._active_events:
                self._active_events.pop(analysis.family, None)
                self._logger.info(f"Ризик: відбій для '{analysis.family}' -- подію знято.")
            self._notify()
            return

        event = self._active_events.get(analysis.family)
        if event is None:
            event = ActiveThreatEvent(
                family=analysis.family,
                tier=analysis.tier,
                status=analysis.status,
                first_seen=now,
                last_seen=now,
            )
            self._active_events[analysis.family] = event
        else:
            event.last_seen = now
            # Escalate if this report is more severe than what's already
            # tracked (e.g. "можливий пуск" MEDIUM later confirmed as
            # VERY_HIGH) -- but never silently de-escalate on a vaguer
            # follow-up; only an explicit cancellation reduces an event.
            if analysis.tier.value > event.tier.value:
                event.tier = analysis.tier
                event.status = analysis.status
            elif analysis.status is ThreatStatus.CONFIRMED and event.status is not ThreatStatus.CONFIRMED:
                # Same tier, but now confirmed -- still worth upgrading so
                # the event's tracked status (surfaced via active_events
                # for the Лог/diagnostics) accurately reflects reality,
                # even though status no longer changes effective_score()
                # itself (see that method's docstring).
                event.status = ThreatStatus.CONFIRMED

        event.corroborating_channels.add(channel_username)
        event.distinct_reports.add(analysis.dedup_fingerprint)

        self._notify()

    def decay_events(self, elapsed_seconds: float) -> None:
        """Prune fully-decayed/expired events and refresh the UI.

        Decay itself is computed continuously from each event's
        timestamps in :meth:`ActiveThreatEvent.effective_score` -- this
        method's job is just to (a) drop events that have decayed to
        ~0 so they stop appearing in ``active_events``, and (b) call the
        UI listener periodically so the displayed score keeps counting
        back down even when no new message arrives at all.

        PERFORMANCE: this runs every second from main.py's tick loop for
        as long as the app is open, including long idle stretches with no
        active threats at all. Unconditionally notifying here used to push
        a full snapshot + region-state update -- and, downstream, a full
        Ukraine-map canvas rebuild/repaint -- once a second forever, even
        when nothing was decaying or changing. There's nothing to animate
        when ``_active_events`` is already empty and nothing expired on
        this tick, so that case now skips the notify entirely. Behavior
        while a threat is actually active/decaying is unchanged (notify
        still fires every tick so the displayed score keeps counting down
        smoothly), as is the one notify that reflects an event expiring
        (the active -> idle transition).
        """
        if elapsed_seconds <= 0.0:
            return
        now = datetime.now()
        had_active_events = bool(self._active_events)
        expired = [family for family, event in self._active_events.items() if event.is_expired(now)]
        for family in expired:
            del self._active_events[family]
        if had_active_events or expired:
            self._notify()

    # Backwards-compatible alias -- main.py's periodic tick loop calls this
    # name; kept so that call site doesn't need to change independently of
    # this redesign.
    decay_telegram_risk = decay_events

    def set_region_active(self, region: Region, is_active: bool) -> None:
        """Manually set a region's active state (used by the map's info click)."""
        state = self._region_states.get(region)
        if state is None:
            return
        if state.is_active == is_active:
            return
        state.is_active = is_active
        state.risk_level = RiskLevel.HIGH if is_active else RiskLevel.NONE
        state.last_changed = datetime.now()
        self._last_update = datetime.now()
        self._notify()

    async def refresh_from_api(self, api_token: str) -> None:
        """Fetch the official per-oblast alert status, if a token is set.

        Logs one summary line per refresh (success or failure) with the
        exact reason on failure -- enough to diagnose a problem from the
        Лог tab without flooding it on every poll cycle. On any failure,
        ``api_status`` is set to :attr:`ApiStatus.ERROR` with that reason;
        the UI must never show a false "Чисто" just because this call
        failed.
        """
        if not api_token.strip():
            self._api_status = ApiStatus.NOT_CONFIGURED
            self._api_error_message = ""
            return

        try:
            async with httpx.AsyncClient(timeout=DEFAULTS.ALERTS_API_TIMEOUT_SECONDS) as client:
                response = await client.get(
                    ALERTS_STATUS_URL,
                    headers={"Authorization": f"Bearer {api_token}"},
                )
        except httpx.TimeoutException as exc:
            self._set_api_error(
                f"Час очікування відповіді від alerts.in.ua вичерпано "
                f"({DEFAULTS.ALERTS_API_TIMEOUT_SECONDS}с). {type(exc).__name__}: {exc}"
            )
            return
        except httpx.ConnectError as exc:
            self._set_api_error(
                f"Немає з'єднання з api.alerts.in.ua (перевірте інтернет). {type(exc).__name__}: {exc}"
            )
            return
        except httpx.HTTPError as exc:
            self._set_api_error(f"Помилка HTTP-запиту до alerts.in.ua: {type(exc).__name__}: {exc}")
            return
        except Exception as exc:  # noqa: BLE001 - must be visible, never hidden
            self._logger.error(
                f"alerts.in.ua: НЕОЧІКУВАНА помилка запиту: {type(exc).__name__}: {exc}\n"
                f"{traceback.format_exc()}"
            )
            self._set_api_error(f"Неочікувана помилка запиту: {type(exc).__name__}: {exc}")
            return

        if response.status_code == 401:
            self._set_api_error(
                "alerts.in.ua: 401 Не авторизовано -- токен API відсутній, неправильний, "
                "відкликаний або прострочений."
            )
            return
        if response.status_code == 403:
            self._set_api_error(
                "alerts.in.ua: 403 Заборонено -- IP заблоковано або API недоступне у вашій країні."
            )
            return
        if response.status_code == 429:
            self._set_api_error(
                "alerts.in.ua: 429 Забагато запитів -- перевищено ліміт (8-12 запитів/хв)."
            )
            return
        if response.status_code != 200:
            self._set_api_error(
                f"alerts.in.ua: неочікуваний HTTP статус {response.status_code}: "
                f"{response.text[:200]!r}"
            )
            return

        try:
            payload = response.json()
        except Exception as exc:  # noqa: BLE001 - must be visible, never hidden
            self._set_api_error(f"Не вдалося розібрати JSON відповіді: {type(exc).__name__}: {exc}")
            self._logger.error(f"alerts.in.ua: {traceback.format_exc()}")
            return

        try:
            self._apply_api_payload(payload)
        except Exception as exc:  # noqa: BLE001 - must be visible, never hidden
            self._set_api_error(f"Не вдалося обробити відповідь alerts.in.ua: {type(exc).__name__}: {exc}")
            self._logger.error(f"alerts.in.ua: {traceback.format_exc()}")

    def _set_api_error(self, message: str) -> None:
        """Record an API failure. Never silently keeps a false 'all clear'."""
        self._api_status = ApiStatus.ERROR
        self._api_error_message = message
        self._logger.warning(message)
        self._last_update = datetime.now()
        self._notify()

    def _apply_api_payload(self, payload: object) -> None:
        """Update region states from the API response.

        The real response of ``/v1/iot/active_air_raid_alerts_by_oblast.json``
        is a single JSON *string* of 27 characters, one per oblast, in the
        fixed order documented at https://devs.alerts.in.ua/ and stored in
        ``ALERTS_OBLAST_ORDER`` -- NOT a list of objects. A previous version
        of this project assumed the list-of-objects shape, which meant this
        method always logged a format warning and returned without ever
        updating a single region, silently leaving the map on its default
        "all clear" state even while real alerts were active. That is
        exactly why the map sometimes showed "Чисто" when alerts.in.ua had
        active alerts: this parsing never succeeded even once.
        """
        if not isinstance(payload, str):
            self._set_api_error(
                f"Неочікуваний формат відповіді alerts.in.ua: очікувався рядок статусів, "
                f"отримано {type(payload).__name__}."
            )
            return

        if len(payload) != len(ALERTS_OBLAST_ORDER):
            self._set_api_error(
                f"Неочікувана довжина рядка статусів alerts.in.ua: "
                f"отримано {len(payload)} символів, очікувалось {len(ALERTS_OBLAST_ORDER)}. "
                f"Можливо, API змінився -- перевірте ALERTS_OBLAST_ORDER у app/config.py."
            )
            return

        active_titles: set[str] = set()
        for oblast_name, status_char in zip(ALERTS_OBLAST_ORDER, payload):
            if status_char in _ACTIVE_STATUS_CHARS:
                active_titles.add(oblast_name)

        newly_alerted: list[Region] = []
        for region, state in self._region_states.items():
            should_be_active = region.value in active_titles
            if state.is_active != should_be_active:
                if not state.is_active and should_be_active and region in self._watched_regions:
                    # A watched region's real transition No Alert -> Air
                    # Raid Alert, straight from the official source --
                    # never fired for the reverse transition (leaving
                    # alert), and this whole block only runs once per
                    # refresh_from_api() call, so a region already active
                    # on the previous refresh can't re-trigger here no
                    # matter how many refreshes it stays active for.
                    newly_alerted.append(region)
                state.is_active = should_be_active
                state.risk_level = RiskLevel.HIGH if should_be_active else RiskLevel.NONE
                state.last_changed = datetime.now()

        self._api_status = ApiStatus.OK
        self._api_error_message = ""
        self._last_update = datetime.now()
        self._logger.info(
            f"alerts.in.ua: оновлено. Активних областей: {len(active_titles)} з "
            f"{len(ALERTS_OBLAST_ORDER)}."
            + (f" ({', '.join(sorted(active_titles))})" if active_titles else "")
        )
        # Always notify, even if nothing changed, so the UI's "last checked"
        # timestamp and api_status move forward and a stale "Чисто" state
        # is confirmed as fresh, not just assumed.
        self._notify()

        if newly_alerted and self._on_region_alert is not None:
            # One call for the whole refresh, even if several watched
            # regions flipped to active together -- the caller (main.py)
            # plays exactly one sound per call, regardless of how many
            # region names are in the list, satisfying "play only ONE
            # notification sound" for simultaneous multi-region alerts.
            self._on_region_alert(newly_alerted)

    async def run_self_diagnostic(self, api_token: str) -> list[tuple[bool, str]]:
        """Run a startup self-check of the alerts.in.ua integration.

        Returns an ordered list of ``(passed, message)`` steps, meant to be
        logged as a readable ✓/✗ checklist. Never raises.
        """
        steps: list[tuple[bool, str]] = []

        if not api_token.strip():
            steps.append((True, "Токен alerts.in.ua не налаштовано -- застосунок працює в режимі "
                                 "'лише Telegram' (це очікувана поведінка, не помилка)."))
            return steps

        steps.append((True, f"Токен alerts.in.ua налаштовано (довжина={len(api_token)})."))

        try:
            async with httpx.AsyncClient(timeout=DEFAULTS.ALERTS_API_TIMEOUT_SECONDS) as client:
                response = await client.get(
                    ALERTS_STATUS_URL,
                    headers={"Authorization": f"Bearer {api_token}"},
                )
        except Exception as exc:  # noqa: BLE001 - diagnostic must never raise
            steps.append((False, f"API недоступне: {type(exc).__name__}: {exc}"))
            return steps

        steps.append((response.status_code == 200, f"HTTP статус: {response.status_code}"))
        if response.status_code != 200:
            steps.append((False, f"Тіло відповіді: {response.text[:300]!r}"))
            return steps

        try:
            payload = response.json()
        except Exception as exc:  # noqa: BLE001
            steps.append((False, f"JSON не розібрано: {type(exc).__name__}: {exc}"))
            return steps
        steps.append((True, f"JSON прочитано (тип={type(payload).__name__})."))

        if not isinstance(payload, str) or len(payload) != len(ALERTS_OBLAST_ORDER):
            steps.append((False, "Формат відповіді не відповідає очікуваному рядку статусів."))
            return steps

        active = [name for name, ch in zip(ALERTS_OBLAST_ORDER, payload) if ch in _ACTIVE_STATUS_CHARS]
        steps.append((True, f"Активні області знайдені: {len(active)} з {len(ALERTS_OBLAST_ORDER)}."))
        return steps

    def _notify(self) -> None:
        """Invoke the registered listener with the fresh snapshot, if any."""
        if self._listener is None:
            return
        try:
            self._listener(self.snapshot(), self.region_states)
        except Exception as exc:  # noqa: BLE001 - a UI bug must not crash the service
            self._logger.error(f"Помилка під час оновлення інтерфейсу: {type(exc).__name__}: {exc}")
