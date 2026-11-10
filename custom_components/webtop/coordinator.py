"""Data update coordinator for the Webtop (SmartSchool) integration.

One poll logs in with pywebtop if needed, walks every student linked to the
account, and collects homework, behaviour events, this week's timetable, the
message inbox and the unread-notification count into a single snapshot.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, TypeVar

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from webtop import WebtopClient, WebtopError, WebtopLoginError, WebtopRequestError

from .const import (
    CONF_DATA,
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL,
    CONF_USERNAME,
    DEFAULT_DATA,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    EVENT_NEW_HOMEWORK,
    EVENT_NEW_MESSAGE,
    REQUEST_TIMEOUT,
)
from .models import (
    DisciplineEvent,
    HomeworkItem,
    Lesson,
    Message,
    Student,
    WebtopApiError,
    parse_discipline,
    parse_homework,
    parse_messages,
    parse_schedule,
    parse_students,
    parse_unread_count,
    student_from_login,
    unwrap,
)

_LOGGER = logging.getLogger(__name__)
_T = TypeVar("_T")

# pywebtop folds the HTTP status into the exception text: "POST /x failed (401): ...".
_HTTP_STATUS_RE = re.compile(r"\((\d{3})\)")
_TRANSIENT_LOGIN_MARKERS = ("timed out", "request failed", "not json", "login failed (5")


def is_auth_error(err: Exception) -> bool:
    """True when the server rejected our session rather than the request."""
    if isinstance(err, WebtopLoginError):
        return True
    if isinstance(err, WebtopApiError):
        return err.looks_like_expired_session
    if isinstance(err, WebtopRequestError):
        match = _HTTP_STATUS_RE.search(str(err))
        return bool(match) and match.group(1) in ("401", "403")
    return False


def is_transient_login_error(err: WebtopLoginError) -> bool:
    """pywebtop raises WebtopLoginError for timeouts too; those are not bad credentials."""
    text = str(err).casefold()
    return any(marker in text for marker in _TRANSIENT_LOGIN_MARKERS)


def school_year_for(now) -> int:
    """Webtop names a school year by the calendar year it ends in."""
    return now.year + 1 if now.month >= 8 else now.year


@dataclass
class Account:
    """The logged-in (parent or student) user."""

    user_id: str = ""
    first_name: str = ""
    last_name: str = ""
    school_name: str = ""

    @property
    def name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()


@dataclass
class WebtopData:
    """Snapshot produced by one poll."""

    account: Account = field(default_factory=Account)
    students: list[Student] = field(default_factory=list)
    homework: dict[str, list[HomeworkItem]] = field(default_factory=dict)
    discipline: dict[str, list[DisciplineEvent]] = field(default_factory=dict)
    schedule: dict[str, list[Lesson]] = field(default_factory=dict)
    # Per student key: False when an optional endpoint failed this poll, so the
    # matching sensor can report unknown instead of a misleading zero.
    homework_ok: dict[str, bool] = field(default_factory=dict)
    discipline_ok: dict[str, bool] = field(default_factory=dict)
    schedule_ok: dict[str, bool] = field(default_factory=dict)
    messages: list[Message] = field(default_factory=list)
    unread_notifications: int | None = None
    # Last raw payloads per endpoint, for the diagnostics download only.
    raw_samples: dict[str, Any] = field(default_factory=dict)

    def student(self, key: str) -> Student | None:
        return next((s for s in self.students if s.key == key), None)


type WebtopConfigEntry = ConfigEntry[WebtopCoordinator]


class WebtopCoordinator(DataUpdateCoordinator[WebtopData]):
    """Polls Webtop through pywebtop."""

    config_entry: WebtopConfigEntry

    def __init__(self, hass: HomeAssistant, entry: WebtopConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(
                minutes=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
            ),
        )
        self.client = WebtopClient(
            username=entry.data[CONF_USERNAME],
            password=entry.data[CONF_PASSWORD],
            data=entry.data.get(CONF_DATA, DEFAULT_DATA),
            timeout=REQUEST_TIMEOUT,
            auto_login=False,
        )
        self._seen_homework: dict[str, set[str]] = {}
        self._seen_messages: set[str] = set()
        self._baseline_done = False
        # Device-registry id of the account device; set once the first poll succeeds.
        self.account_device_id: str | None = None

    async def async_close(self) -> None:
        """Close the underlying HTTP client."""
        try:
            await self.client.close()
        except Exception:
            _LOGGER.debug("Closing the Webtop client failed", exc_info=True)

    # ------------------------------------------------------------------
    # update
    # ------------------------------------------------------------------
    async def _async_update_data(self) -> WebtopData:
        try:
            data = await self._fetch_with_relogin()
        except WebtopLoginError as err:
            if is_transient_login_error(err):
                raise UpdateFailed(f"Webtop login did not complete: {err}") from err
            raise ConfigEntryAuthFailed("Webtop rejected the credentials") from err
        except (WebtopError, WebtopApiError) as err:
            raise UpdateFailed(f"Webtop request failed: {err}") from err

        self._publish_events(data)
        return data

    async def _fetch_with_relogin(self) -> WebtopData:
        """Fetch everything; on a rejected session log in once more and retry."""
        if not self.client.is_logged_in:
            await self.client.login()
        try:
            return await self._collect()
        except (WebtopRequestError, WebtopApiError) as err:
            if not is_auth_error(err):
                raise
            _LOGGER.debug("Webtop session was rejected (%s); logging in again", err)
            await self.client.login()
            return await self._collect()

    async def _optional(
        self, what: str, call: Callable[[], Awaitable[_T]], *, envelope: str | None = None
    ) -> Any:
        """Run a secondary fetch; a failure there must not discard the rest.

        With ``envelope`` set, the response is unwrapped (``status``/``data``)
        and a ``status: false`` counts as a failure too. Session errors still
        propagate so the caller can log in again.
        """
        try:
            result = await call()
            return unwrap(result, envelope) if envelope else result
        except (WebtopRequestError, WebtopApiError) as err:
            if is_auth_error(err):
                raise
            _LOGGER.warning("Webtop %s failed; keeping the other data: %s", what, err)
            return None

    async def _collect(self) -> WebtopData:
        client = self.client
        data = WebtopData()
        now = dt_util.now()
        today = now.strftime("%Y-%m-%d")

        session = client.session
        data.account = Account(
            user_id=str(session.user_id or ""),
            first_name=session.first_name or "",
            last_name=session.last_name or "",
            school_name=session.school_name or "",
        )

        # Siblings in different schools are separate logins under one parent
        # account; the session has to be switched to each in turn.
        linked = await self._optional("linked students", client.get_linked_students) or []
        contexts: list[dict[str, Any] | None] = (
            [ctx for ctx in linked if isinstance(ctx, dict)] if len(linked) > 1 else [None]
        )
        data.raw_samples["linked_students"] = linked

        messages: dict[str, Message] = {}
        for context in contexts:
            if context is not None:
                student_id = context.get("studentId") or context.get("StudentId")
                try:
                    await client.switch_student(student_id=str(student_id))
                except WebtopLoginError as err:
                    _LOGGER.warning(
                        "Could not switch to linked student %s; skipping it this poll: %s",
                        context.get("studentLogin") or student_id,
                        err,
                    )
                    continue
            session = client.session
            school_name = session.school_name or (context or {}).get("school_name") or ""

            dashboard = unwrap(await client.get_students(), "InitDashboard")
            data.raw_samples["dashboard"] = dashboard
            students = parse_students(dashboard, school_name=school_name)
            if not students:
                fallback = student_from_login(session.raw_login_data or {}, school_name=school_name)
                if fallback is not None:
                    students = [fallback]
            if not students:
                _LOGGER.warning("Webtop dashboard listed no students for this session")

            for student in students:
                if any(s.key == student.key for s in data.students):
                    continue  # same child reachable through two contexts
                data.students.append(student)
                await self._collect_student(data, student, today=today, year=school_year_for(now))

            inbox = unwrap(await client.get_messages_inbox(page_id=1), "GetMessagesInbox")
            data.raw_samples["inbox"] = inbox
            context_name = students[0].name if len(students) == 1 else ""
            for message in parse_messages(inbox):
                message.student_name = context_name
                messages.setdefault(message.identity(), message)

            payload = await self._optional(
                "notification preview",
                client.get_preview_unread_notifications,
                envelope="GetPreviewUnreadNotifications",
            )
            if payload is not None:
                data.raw_samples["notifications"] = payload
                count = parse_unread_count(payload)
                if count is not None:
                    data.unread_notifications = (data.unread_notifications or 0) + count

        data.messages = sorted(messages.values(), key=lambda m: m.sent_at, reverse=True)
        return data

    async def _collect_student(self, data: WebtopData, student: Student, *, today: str, year: int) -> None:
        client = self.client
        key = student.key
        data.homework[key] = []
        data.discipline[key] = []
        data.schedule[key] = []
        data.homework_ok[key] = data.discipline_ok[key] = data.schedule_ok[key] = False

        if not student.has_class_params:
            _LOGGER.warning(
                "Webtop did not report a class for %s; homework, behaviour and timetable are unavailable",
                student.name or key,
            )
            return

        payload = await self._optional(
            f"homework for {student.name}",
            lambda: client.get_homework(
                encrypted_student_id=student.student_id,
                class_code=student.class_code,
                class_number=student.class_number if student.class_number is not None else 0,
            ),
            envelope="GetHomeWork",
        )
        if payload is not None:
            data.raw_samples["homework"] = payload
            data.homework[key] = parse_homework(payload, date=today)
            data.homework_ok[key] = True

        payload = await self._optional(
            f"behaviour events for {student.name}",
            lambda: client.get_discipline_events(
                encrypted_student_id=student.student_id, class_code=student.class_code
            ),
            envelope="GetPupilDiciplineEvents",
        )
        if payload is not None:
            data.raw_samples["discipline"] = payload
            data.discipline[key] = parse_discipline(payload)
            data.discipline_ok[key] = True

        payload = await self._optional(
            f"timetable for {student.name}",
            lambda: client.get_pupil_schedule(
                week_index=0,
                study_year=student.study_year or year,
                encrypted_student_id=student.student_id,
                class_code=student.class_code,
            ),
            envelope="GetPupilScheduale",
        )
        if payload is not None:
            data.raw_samples["schedule"] = payload
            data.schedule[key] = parse_schedule(payload)
            data.schedule_ok[key] = True

    # ------------------------------------------------------------------
    # events
    # ------------------------------------------------------------------
    def _publish_events(self, data: WebtopData) -> None:
        """Fire bus events for homework and messages not seen in an earlier poll.

        The first successful poll only records a baseline, so a restart does
        not replay every existing item as new.
        """
        for student in data.students:
            seen = self._seen_homework.setdefault(student.key, set())
            if not data.homework_ok.get(student.key):
                continue
            for item in data.homework[student.key]:
                ident = item.identity()
                if ident in seen:
                    continue
                seen.add(ident)
                if self._baseline_done:
                    self.hass.bus.async_fire(
                        EVENT_NEW_HOMEWORK,
                        {
                            "entry_id": self.config_entry.entry_id,
                            "student": student.name,
                            "student_key": student.key,
                            **item.as_dict(),
                        },
                    )
        for message in data.messages:
            ident = message.identity()
            if ident in self._seen_messages:
                continue
            self._seen_messages.add(ident)
            if self._baseline_done:
                self.hass.bus.async_fire(
                    EVENT_NEW_MESSAGE,
                    {"entry_id": self.config_entry.entry_id, **message.as_dict()},
                )
        self._baseline_done = True
