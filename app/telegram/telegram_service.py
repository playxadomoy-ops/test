"""
Telegram integration built on Telethon.

Responsibilities:
  * connect to Telegram using a persisted session string when available,
  * otherwise drive an interactive login (phone -> code -> optional 2FA
    password) through callbacks supplied by the UI layer,
  * monitor an arbitrary set of channels/chats for new messages,
  * automatically reconnect after network loss,
  * never let a Telethon-internal warning (e.g. missing ``cryptg``, SSL
    library notices) propagate as a crash — those are purely informational.

No business logic (risk scoring, region state) lives here: this module
only reports raw text via a callback and lets :class:`AlertService` /
:class:`RiskAnalyzer` decide what it means.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from telethon import TelegramClient, errors, events
from telethon.errors import SessionPasswordNeededError
from telethon.sessions import StringSession

from app.services.logger_service import LoggerService

# Telethon's own logger can be noisy with informational notices (missing
# optional accelerators, SSL library version notes). These are harmless,
# so we quiet them down to avoid confusing the user; real problems are
# still surfaced through LoggerService below.
logging.getLogger("telethon").setLevel(logging.ERROR)

OnMessageReceived = Callable[[str, str], None]  # (channel_username, text)
OnChannelStatusChanged = Callable[[str, bool], None]  # (channel_username, connected)

_RECONNECT_BASE_DELAY_SECONDS = 5
_RECONNECT_MAX_DELAY_SECONDS = 60


@dataclass(slots=True)
class TelegramAuthCallbacks:
    """UI-supplied callbacks used only during interactive login."""

    request_phone: Callable[[], Awaitable[str]]
    request_code: Callable[[], Awaitable[str]]
    request_password: Callable[[], Awaitable[str]]


class TelegramService:
    """Owns the Telethon client and the set of monitored channels."""

    def __init__(self, logger: LoggerService) -> None:
        """Create the service; the actual client is built in :meth:`start`."""
        self._logger = logger
        self._client: Optional[TelegramClient] = None
        self._running = False
        self._monitored_channels: set[str] = set()
        self._on_message: Optional[OnMessageReceived] = None
        self._on_channel_status: Optional[OnChannelStatusChanged] = None
        self._session_string: str = ""
        #: True for the entire duration of an in-progress start() call
        #: (from entry until its connection loop actually ends) -- guards
        #: against a second concurrent start() (e.g. a double-tap on
        #: "Увійти в Telegram", or an auto-start racing a credentials-
        #: changed restart) creating a second TelegramClient while the
        #: first is still connected. Without this, both calls would
        #: overwrite self._client/self._session_string, silently orphaning
        #: whichever connection loses the race -- its socket and
        #: reconnect-loop task would keep running forever with nothing
        #: left able to call stop() on it.
        self._start_in_progress: bool = False

    def set_message_listener(self, listener: Optional[OnMessageReceived]) -> None:
        """Register the callback invoked for every new monitored message."""
        self._on_message = listener

    def set_channel_status_listener(self, listener: Optional[OnChannelStatusChanged]) -> None:
        """Register the callback invoked when a channel's connection state changes."""
        self._on_channel_status = listener

    @property
    def session_string(self) -> str:
        """Return the current session string (empty if not authorized)."""
        return self._session_string

    @property
    def is_running(self) -> bool:
        """Whether the background connection loop is currently active."""
        return self._running

    async def start(
        self,
        api_id: str,
        api_hash: str,
        session_string: str,
        auth_callbacks: Optional[TelegramAuthCallbacks],
        on_auth_error: Optional[Callable[[str], None]] = None,
        on_session_ready: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> None:
        """Connect to Telegram and keep the connection alive until :meth:`stop`.

        Intended to be launched as a background task (``page.run_task``).
        Reconnects automatically with backoff on network loss. Any error
        is logged (with full exception type + message, never hidden); if
        it happens before the first successful authorization, it is also
        forwarded to ``on_auth_error`` so the UI can show it on screen.

        ``on_session_ready``, if given, is awaited with the session
        string immediately after a successful login/reconnect -- NOT
        only after this coroutine eventually returns. This method stays
        running for as long as the connection is alive (it only returns
        when :meth:`stop` is called or the connection is abandoned), so
        persisting the session solely in a caller's ``finally`` block
        around ``await service.start(...)`` would only ever save it once
        the connection later drops -- if the app is simply closed while
        still connected, that save would never happen, forcing the user
        to log in again on every restart.
        """
        if not api_id.strip() or not api_hash.strip():
            message = "Telegram не запущено: не вказано API ID / API HASH у налаштуваннях."
            self._logger.warning(message)
            if on_auth_error:
                on_auth_error(message)
            return

        if self._start_in_progress:
            message = (
                "Telegram: start() вже виконується -- повторний виклик проігноровано "
                "(запобігання паралельному з'єднанню, напр. подвійне натискання кнопки входу)."
            )
            self._logger.warning(message)
            return

        try:
            parsed_api_id = int(api_id)
        except ValueError:
            message = f"API ID має бути числом, отримано: {api_id!r}."
            self._logger.error(message)
            if on_auth_error:
                on_auth_error(message)
            return

        self._start_in_progress = True
        try:
            await self._run_connection_loop(parsed_api_id, api_hash, session_string, auth_callbacks, on_auth_error, on_session_ready)
        finally:
            self._start_in_progress = False

    async def _run_connection_loop(
        self,
        parsed_api_id: int,
        api_hash: str,
        session_string: str,
        auth_callbacks: Optional[TelegramAuthCallbacks],
        on_auth_error: Optional[Callable[[str], None]],
        on_session_ready: Optional[Callable[[str], Awaitable[None]]],
    ) -> None:
        """The actual connect/reconnect loop, run under start()'s reentrancy guard."""
        self._running = True
        delay = _RECONNECT_BASE_DELAY_SECONDS
        authorized_once = False

        while self._running:
            try:
                self._client = TelegramClient(
                    StringSession(session_string or self._session_string),
                    parsed_api_id,
                    api_hash,
                )
                await self._client.connect()
                already_authorized = await self._client.is_user_authorized()

                if not already_authorized:
                    if auth_callbacks is None:
                        message = "Потрібна авторизація Telegram, але UI недоступний для введення коду."
                        self._logger.warning(message)
                        if on_auth_error:
                            on_auth_error(message)
                        self._running = False
                        return
                    await self._interactive_login(
                        auth_callbacks, on_auth_error if not authorized_once else None
                    )

                self._session_string = self._client.session.save()  # type: ignore[union-attr]
                authorized_once = True
                if on_session_ready is not None:
                    await on_session_ready(self._session_string)
                self._logger.info("Telegram підключено.")
                await self._register_handlers()
                delay = _RECONNECT_BASE_DELAY_SECONDS

                # Stay connected and process events until stop() is called
                # or the connection drops.
                while self._running and self._client.is_connected():
                    await asyncio.sleep(1)

                if self._running:
                    self._logger.warning(f"Telegram: з'єднання втрачено, повторна спроба через {delay}с.")

            except SessionPasswordNeededError as exc:
                message = (
                    "Потрібен пароль двофакторної автентифікації "
                    f"({type(exc).__name__}: {exc})."
                )
                self._logger.error(message)
                if on_auth_error and not authorized_once:
                    on_auth_error(message)
            except errors.ApiIdInvalidError as exc:
                message = (
                    "Невірні API ID / API HASH -- перевірте значення в Налаштуваннях "
                    f"({type(exc).__name__})."
                )
                self._logger.error(message)
                if on_auth_error and not authorized_once:
                    on_auth_error(message)
                # An invalid ID/HASH pair will never succeed on retry --
                # stop the reconnect loop instead of hammering Telegram's
                # servers every few seconds with a doomed request.
                self._running = False
            except (ConnectionError, OSError, asyncio.TimeoutError) as exc:
                message = f"Немає з'єднання з мережею: {type(exc).__name__}: {exc}."
                self._logger.warning(message)
                if on_auth_error and not authorized_once:
                    on_auth_error(message)
            except Exception as exc:  # noqa: BLE001 - must be visible, never hidden
                message = f"Помилка з'єднання з Telegram: {type(exc).__name__}: {exc}"
                self._logger.error(message)
                if on_auth_error and not authorized_once:
                    on_auth_error(message)
            finally:
                await self._safe_disconnect()

            if self._running:
                await asyncio.sleep(delay)
                delay = min(delay * 2, _RECONNECT_MAX_DELAY_SECONDS)

    async def stop(self) -> None:
        """Stop the connection loop and disconnect cleanly."""
        self._running = False
        await self._safe_disconnect()

    async def update_monitored_channels(self, usernames: list[str]) -> None:
        """Replace the set of monitored channels and re-register handlers."""
        self._monitored_channels = {u.strip() for u in usernames if u.strip()}
        if self._client is not None and self._client.is_connected():
            await self._register_handlers()

    async def iter_channel_history(
        self, username: str, min_id: int = 0, limit: int = 2000
    ):
        """Yield ``(message_id, text)`` for a channel's history newer than ``min_id``.

        Used only by :mod:`app.services.vocabulary_builder` for its
        incremental phrase-mining pass -- not part of the live message
        stream. Requires an active connection; yields nothing (not an
        error) if not currently connected, so a caller can safely skip a
        channel that isn't reachable right now rather than crash the
        whole vocabulary run over one channel.

        Telethon's ``iter_messages`` already paces itself with network
        I/O between batches, so this naturally yields control back to
        the event loop throughout -- no manual chunking is needed to
        avoid blocking the UI thread.
        """
        if self._client is None or not self._client.is_connected():
            self._logger.warning(
                f"Vocabulary Builder: немає з'єднання, пропускаю історію '{username}'."
            )
            return
        try:
            entity = await self._client.get_entity(username)
        except Exception as exc:  # noqa: BLE001 - one bad channel must not stop the run
            self._logger.warning(
                f"Vocabulary Builder: не вдалося отримати канал '{username}': "
                f"{type(exc).__name__}: {exc}"
            )
            return

        try:
            async for message in self._client.iter_messages(
                entity, min_id=min_id, limit=limit, reverse=True
            ):
                if message.text:
                    yield message.id, message.text
        except Exception as exc:  # noqa: BLE001 - must be visible, never hidden
            self._logger.warning(
                f"Vocabulary Builder: помилка читання історії '{username}': "
                f"{type(exc).__name__}: {exc}"
            )

    async def _interactive_login(
        self,
        callbacks: TelegramAuthCallbacks,
        on_auth_error: Optional[Callable[[str], None]] = None,
    ) -> None:
        """Run the phone -> code -> (optional) password login flow.

        Logs one confirmation when the code is sent, and every distinct
        failure reason (never hidden or merged into a generic message) --
        if ``on_auth_error`` is provided, failures are also surfaced to
        the UI.
        """
        assert self._client is not None

        phone = await callbacks.request_phone()

        if not phone.strip():
            message = "Номер телефону порожній -- send_code_request() не буде викликано."
            self._logger.error(message)
            if on_auth_error:
                on_auth_error(message)
            raise ValueError(message)

        try:
            sent_code = await self._client.send_code_request(phone)
        except errors.PhoneNumberInvalidError as exc:
            message = (
                "Невірний формат номера телефону (використовуйте формат +380XXXXXXXXX). "
                f"{type(exc).__name__}: {exc}"
            )
            self._logger.error(message)
            if on_auth_error:
                on_auth_error(message)
            raise
        except errors.PhoneNumberBannedError as exc:
            message = f"Цей номер телефону заблоковано Telegram. {type(exc).__name__}: {exc}"
            self._logger.error(message)
            if on_auth_error:
                on_auth_error(message)
            raise
        except errors.PhoneNumberFloodError as exc:
            message = f"Забагато спроб входу з цим номером. {type(exc).__name__}: {exc}"
            self._logger.error(message)
            if on_auth_error:
                on_auth_error(message)
            raise
        except errors.FloodWaitError as exc:
            message = (
                f"Telegram тимчасово обмежив запити. Спробуйте через {exc.seconds} с. "
                f"{type(exc).__name__}: {exc}"
            )
            self._logger.error(message)
            if on_auth_error:
                on_auth_error(message)
            raise
        except errors.ApiIdInvalidError as exc:
            message = f"Невірні API ID / API HASH у налаштуваннях. {type(exc).__name__}: {exc}"
            self._logger.error(message)
            if on_auth_error:
                on_auth_error(message)
            raise
        except Exception as exc:  # noqa: BLE001 - must be visible, never hidden
            message = f"Не вдалося надіслати код: {type(exc).__name__}: {exc}"
            self._logger.error(message)
            if on_auth_error:
                on_auth_error(message)
            raise

        self._logger.info("Код надіслано в Telegram.")

        code = await callbacks.request_code()

        if not code.strip():
            message = "Код підтвердження порожній -- sign_in() не буде викликано."
            self._logger.error(message)
            if on_auth_error:
                on_auth_error(message)
            raise ValueError(message)

        try:
            await self._client.sign_in(
                phone=phone, code=code, phone_code_hash=sent_code.phone_code_hash
            )
        except SessionPasswordNeededError:
            password = await callbacks.request_password()
            try:
                await self._client.sign_in(password=password)
            except Exception as exc:  # noqa: BLE001 - must be visible, never hidden
                message = f"Не вдалося увійти з паролем: {type(exc).__name__}: {exc}"
                self._logger.error(message)
                if on_auth_error:
                    on_auth_error(message)
                raise
        except errors.PhoneCodeInvalidError as exc:
            message = f"Введений код авторизації невірний. {type(exc).__name__}: {exc}"
            self._logger.error(message)
            if on_auth_error:
                on_auth_error(message)
            raise
        except errors.PhoneCodeExpiredError as exc:
            message = f"Код авторизації прострочений, спробуйте увійти знову. {type(exc).__name__}: {exc}"
            self._logger.error(message)
            if on_auth_error:
                on_auth_error(message)
            raise
        except Exception as exc:  # noqa: BLE001 - must be visible, never hidden
            message = f"Не вдалося виконати вхід: {type(exc).__name__}: {exc}"
            self._logger.error(message)
            if on_auth_error:
                on_auth_error(message)
            raise

    async def _register_handlers(self) -> None:
        """(Re)register the new-message event handler for monitored channels."""
        if self._client is None:
            return

        self._client.remove_event_handler(self._handle_new_message)

        if not self._monitored_channels:
            self._logger.debug("Немає активних каналів для моніторингу.")
            return

        resolved_entities = []
        for username in self._monitored_channels:
            try:
                entity = await self._client.get_entity(username)
                resolved_entities.append(entity)
                if self._on_channel_status:
                    self._on_channel_status(username, True)
            except Exception as exc:  # noqa: BLE001
                self._logger.warning(f"Не вдалося підключитися до каналу {username}: {exc}")
                if self._on_channel_status:
                    self._on_channel_status(username, False)

        if resolved_entities:
            self._client.add_event_handler(
                self._handle_new_message, events.NewMessage(chats=resolved_entities)
            )

    async def _handle_new_message(self, event: events.NewMessage.Event) -> None:
        """Forward a raw incoming message to the registered listener."""
        try:
            text = event.raw_text or ""
            chat = await event.get_chat()
            username = getattr(chat, "username", None) or str(getattr(chat, "id", "unknown"))
            if self._on_message:
                self._on_message(username, text)
        except Exception as exc:  # noqa: BLE001 - a bad message must not kill the client
            self._logger.error(f"Помилка обробки повідомлення Telegram: {exc}")

    async def _safe_disconnect(self) -> None:
        """Disconnect the client, logging (never hiding) any error."""
        if self._client is None:
            return
        try:
            await self._client.disconnect()  # type: ignore[func-returns-value]
        except Exception as exc:  # noqa: BLE001 - must be visible, never hidden
            self._logger.debug(f"Telegram: disconnect() завершився помилкою (неважливо): {type(exc).__name__}: {exc}")
