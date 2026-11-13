"""Data update coordinator for the Webtop (SmartSchool) integration.

One poll logs in with pywebtop if needed, walks every student linked to the
account, and collects homework, behaviour events, this week's timetable, the
message inbox and the unread-notification count into a single snapshot.
"""

from __future__ import annotations

import datetime as dt
import logging
import random
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

from .auth import async_create_client, login_failure_reason
from .const import (
    CONF_DATA,
    CONF_LESSON_TIMES,
    CONF_NIGHT_PAUSE,
    CONF_PASSWORD,
    CONF_READ_UNREAD_MESSAGES,
    CONF_SCAN_INTERVAL,
    CONF_STUDENT_LESSON_TIMES,
    CONF_USERNAME,
    DEFAULT_NIGHT_PAUSE,
    DEFAULT_READ_UNREAD_MESSAGES,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    EVENT_NEW_BEHAVIOUR,
    EVENT_NEW_GRADE,
    EVENT_NEW_HOMEWORK,
    EVENT_NEW_MESSAGE,
    EVENT_NEW_NOTIFICATION,
    EVENT_NEW_TEST,
    MESSAGE_BODIES_PER_POLL,
    MESSAGE_BODY_CACHE_SIZE,
    MESSAGE_BODY_EVENT_CHARS,
    MODULE_BEHAVIOUR,
    MODULE_GRADES,
    MODULE_TIMETABLE,
    MODULE_WEEK,
    NIGHT_PAUSE_END_HOUR,
    NIGHT_PAUSE_START_HOUR,
    POLL_JITTER,
    PREVIOUS_WEEK_REFRESH_HOURS,
    TESTS_BOARD_MONTHS_AHEAD,
)
from .models import (
    BellSchedule,
    DisciplineEvent,
    Grade,
    HomeworkItem,
    Lesson,
    Message,
    Notification,
    ScheduleChange,
    SchoolEvent,
    Student,
    WebtopApiError,
    board_available,
    dashboard_allows,
    fill_rooms,
    homework_from_lessons,
    parse_bell_schedule,
    parse_changes,
    parse_discipline,
    parse_grades,
    parse_homework,
    parse_manual_bell,
    parse_menu_counters,
    parse_message_body,
    parse_messages,
    parse_notifications,
    parse_pending_evaluations,
    parse_schedule,
    parse_students,
    parse_tests_board,
    parse_unread_count,
    student_from_login,
    student_groups,
    unwrap,
    view_allowed,
)

API = "/server/api/"

_LOGGER = logging.getLogger(__name__)
_T = TypeVar("_T")

# pywebtop folds the HTTP status into the exception text: "POST /x failed (401): ...".
_HTTP_STATUS_RE = re.compile(r"\((\d{3})\)")


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
    # Exams, school events and vacations that apply to each student, soonest first.
    calendar: dict[str, list[SchoolEvent]] = field(default_factory=dict)
    calendar_ok: dict[str, bool] = field(default_factory=dict)
    # Lesson number -> (start, end) for each student's school.
    bell: dict[str, BellSchedule] = field(default_factory=dict)
    grades: dict[str, list[Grade]] = field(default_factory=dict)
    grades_ok: dict[str, bool] = field(default_factory=dict)
    changes: dict[str, list[ScheduleChange]] = field(default_factory=dict)
    changes_ok: dict[str, bool] = field(default_factory=dict)
    # Study groups each student attends; exams are addressed to study groups.
    groups: dict[str, frozenset[int]] = field(default_factory=dict)
    # Evaluations set but not yet marked, from the pupil card (only when it could be read).
    pending: dict[str, list[SchoolEvent]] = field(default_factory=dict)
    # The standing weekday timetable, and last week's dated lessons; both feed the timetable calendar.
    standing: dict[str, list[Lesson]] = field(default_factory=dict)
    previous_schedule: dict[str, list[Lesson]] = field(default_factory=dict)
    messages: list[Message] = field(default_factory=list)
    # From the portal's menu badges; None when the counters could not be read.
    unread_messages: int | None = None
    unread_notifications: int | None = None
    notifications: list[Notification] = field(default_factory=list)
    # Last raw payloads per endpoint, for the diagnostics download only.
    raw_samples: dict[str, Any] = field(default_factory=dict)

    def student(self, key: str) -> Student | None:
        return next((s for s in self.students if s.key == key), None)


# The WebtopData dictionaries keyed by student, carried over when a school cannot be reached.
PER_STUDENT_FIELDS = (
    "homework", "homework_ok", "discipline", "discipline_ok", "schedule", "schedule_ok",
    "calendar", "calendar_ok", "bell", "grades", "grades_ok", "changes", "changes_ok", "groups", "pending",
    "standing", "previous_schedule",
)  # fmt: skip

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
        # Built on first use: creating it loads TLS certificates from disk, and
        # its login "Data" depends on a counter fetched from the server.
        self._client: WebtopClient | None = None
        # Lesson times typed by the user: a default for the account, and per student.
        self._default_bell = self._typed_bell(entry.options.get(CONF_LESSON_TIMES), "the account")
        self._student_bell: dict[str, BellSchedule] = {
            key: bell
            for key, text in (entry.options.get(CONF_STUDENT_LESSON_TIMES) or {}).items()
            if (bell := self._typed_bell(text, "a student"))
        }
        # What the portal itself publishes per student (empty when it publishes none).
        self.portal_bell: dict[str, BellSchedule] = {}
        # Lesson times read off the labels of this week's lessons, per student.
        self._week_bell: dict[str, BellSchedule] = {}
        # Per student: (day fetched, lesson times). Per institution: day it had no tests board.
        self._bell_cache: dict[str, tuple[dt.date, BellSchedule]] = {}
        self._board_unavailable: dict[str, dt.date] = {}
        # Per student: (day fetched, standing timetable, lesson times read off its labels).
        self._timetable_cache: dict[str, tuple[dt.date, list[Lesson], BellSchedule]] = {}
        # Views a school has blocked, by cache key -> the day that was learned.
        self._blocked: dict[str, dt.date] = {}
        # Linked login id -> the students found under it, to carry over if a switch fails.
        self._context_students: dict[str, list[str]] = {}
        # Per student: (when fetched, last week's lessons).
        self._previous_week_cache: dict[str, tuple[dt.datetime, list[Lesson]]] = {}
        self._base_interval = self.update_interval or timedelta(minutes=DEFAULT_SCAN_INTERVAL)
        self._night_pause: bool = entry.options.get(CONF_NIGHT_PAUSE, DEFAULT_NIGHT_PAUSE)
        self._read_unread: bool = entry.options.get(CONF_READ_UNREAD_MESSAGES, DEFAULT_READ_UNREAD_MESSAGES)
        # Message identity -> its text. A text never changes, so each is fetched once.
        self._message_bodies: dict[str, str] = {}
        self._seen_notifications: set[str] = set()
        self._force_next = False
        self._seen_homework: dict[str, set[str]] = {}
        self._seen_behaviour: dict[str, set[str]] = {}
        self._seen_grades: dict[str, set[str]] = {}
        self._seen_tests: dict[str, set[str]] = {}
        self._seen_messages: set[str] = set()
        self._baseline_done = False
        # Device-registry id of the account device; set once the first poll succeeds.
        self.account_device_id: str | None = None

    @property
    def client(self) -> WebtopClient:
        if self._client is None:
            raise WebtopLoginError("Not logged in")
        return self._client

    @staticmethod
    def _typed_bell(text: str | None, whose: str) -> BellSchedule:
        try:
            return parse_manual_bell(text)
        except ValueError as err:
            _LOGGER.warning("Ignoring the lesson times typed for %s: %s", whose, err)
            return {}

    async def async_close(self) -> None:
        """Close the underlying HTTP client."""
        client, self._client = self._client, None
        if client is None:
            return
        try:
            await client.close()
        except Exception:
            _LOGGER.debug("Closing the Webtop client failed", exc_info=True)

    async def _async_login(self) -> None:
        """Start a fresh session: new client, new login."""
        await self.async_close()
        entry = self.config_entry
        client = await async_create_client(
            self.hass,
            entry.data[CONF_USERNAME],
            entry.data[CONF_PASSWORD],
            entry.data.get(CONF_DATA),
        )
        try:
            await client.login()
        except BaseException:
            await client.close()
            raise
        self._client = client

    # ------------------------------------------------------------------
    # update
    # ------------------------------------------------------------------
    async def async_force_refresh(self) -> None:
        """Poll the portal now, even during the night pause (the "Refresh" button)."""
        self._force_next = True
        await self.async_refresh()

    def in_night_pause(self, now: dt.datetime | None = None) -> bool:
        if not self._night_pause:
            return False
        hour = (now or dt_util.now()).hour
        return hour >= NIGHT_PAUSE_START_HOUR or hour < NIGHT_PAUSE_END_HOUR

    async def _async_update_data(self) -> WebtopData:
        # Shift the next poll a little, so polls drift instead of landing on the clock.
        self.update_interval = self._base_interval * random.uniform(1 - POLL_JITTER, 1 + POLL_JITTER)
        forced, self._force_next = self._force_next, False
        if self.data is not None and not forced and self.in_night_pause():
            # Nothing changes on the portal at night. Entities still re-evaluate
            # (the date rolls over at midnight), but the portal is left alone.
            return self.data
        try:
            data = await self._fetch_with_relogin()
        except WebtopLoginError as err:
            reason = login_failure_reason(err)
            if reason == "cannot_connect":
                raise UpdateFailed(f"Webtop login did not complete: {err}") from err
            if reason == "captcha_required":
                raise ConfigEntryAuthFailed(
                    "Webtop demands a captcha for this login; sign in once in a browser, then re-authenticate"
                ) from err
            if reason == "too_many_attempts":
                raise ConfigEntryAuthFailed("Webtop locked the account after too many attempts") from err
            raise ConfigEntryAuthFailed("Webtop rejected the credentials") from err
        except (WebtopError, WebtopApiError) as err:
            raise UpdateFailed(f"Webtop request failed: {err}") from err

        self._publish_events(data)
        return data

    async def _fetch_with_relogin(self) -> WebtopData:
        """Fetch everything; on a rejected session log in once more and retry."""
        if self._client is None or not self._client.is_logged_in:
            await self._async_login()
        try:
            return await self._collect()
        except (WebtopRequestError, WebtopApiError) as err:
            if not is_auth_error(err):
                raise
            _LOGGER.debug("Webtop session was rejected (%s); logging in again", err)
            await self._async_login()
            return await self._collect()

    async def _optional(
        self,
        what: str,
        call: Callable[[], Awaitable[_T]],
        *,
        envelope: str | None = None,
        remember: str | None = None,
    ) -> Any:
        """Run a secondary fetch; a failure there must not discard the rest.

        With ``envelope`` set, the response is unwrapped (``status``/``data``)
        and a ``status: false`` counts as a failure too. Session errors still
        propagate so the caller can log in again.

        With ``remember`` set, a view the school has blocked is not requested
        again until tomorrow: it will not unblock within the day, and asking
        every poll only adds load and log noise.
        """
        today = dt_util.now().date()
        if remember is not None and self._blocked.get(remember) == today:
            return None
        try:
            result = await call()
            return unwrap(result, envelope) if envelope else result
        except (WebtopRequestError, WebtopApiError) as err:
            if is_auth_error(err):
                raise
            if isinstance(err, WebtopApiError) and err.is_blocked_view:
                if remember is not None:
                    self._blocked[remember] = today
                _LOGGER.debug("Webtop %s is switched off by the school (%s)", what, err.description)
                return None
            _LOGGER.warning("Webtop %s failed; keeping the other data: %s", what, err)
            return None

    async def _collect(self) -> WebtopData:
        client = self.client
        data = WebtopData()
        now = dt_util.now()

        session = client.session
        data.account = Account(
            user_id=str(session.user_id or ""),
            first_name=session.first_name or "",
            last_name=session.last_name or "",
            school_name=session.school_name or "",
        )

        # A parent with children in different schools has one login per school,
        # linked together; the session has to be switched to each in turn.
        linked = await self._optional("linked students", client.get_linked_students) or []
        contexts: list[dict[str, Any] | None] = (
            [ctx for ctx in linked if isinstance(ctx, dict)] if len(linked) > 1 else [None]
        )
        data.raw_samples["linked_students"] = linked

        messages: dict[str, Message] = {}
        for context in contexts:
            context_id = str((context or {}).get("studentId") or "")
            if context is not None and not await self._enter_context(context):
                # Keep showing that school's last known data rather than dropping its entities.
                self._carry_over(data, self._context_students.get(context_id, []))
                continue
            session = client.session
            login_data = session.raw_login_data or {}
            school_name = session.school_name or (context or {}).get("school_name") or ""
            institution = login_data.get("institutionCode") or login_data.get("InstitutionCode")

            dashboard = unwrap(await client.get_students(), "InitDashboard")
            data.raw_samples["dashboard"] = dashboard
            students = parse_students(dashboard, school_name=school_name)
            if not students:
                fallback = student_from_login(login_data, school_name=school_name)
                if fallback is not None:
                    students = [fallback]
            if not students:
                _LOGGER.warning("Webtop dashboard listed no students for this session")

            fresh = [s for s in students if not any(known.key == s.key for known in data.students)]
            self._context_students[context_id] = [s.key for s in fresh]
            board_events, board_bell = await self._collect_tests_board(data, institution, now)
            for student in fresh:  # a child reachable through two contexts is only read once
                data.students.append(student)
                await self._collect_student(data, student, now=now, institution=institution, dashboard=dashboard)
                groups = data.groups.get(student.key, frozenset())
                if board_events is not None:
                    data.calendar[student.key] = [e for e in board_events if e.applies_to(student, groups)]
                    data.calendar_ok[student.key] = True
                elif student.key in data.pending:
                    # No tests board at this school: the unmarked evaluations on the pupil
                    # card are the tests that have been set.
                    data.calendar[student.key] = data.pending[student.key]
                    data.calendar_ok[student.key] = True
                else:
                    data.calendar[student.key] = []
                    data.calendar_ok[student.key] = False
                data.bell[student.key] = await self._bell_for(student, board_bell, institution, now)

            inbox = unwrap(await client.get_messages_inbox(page_id=1), "GetMessagesInbox")
            data.raw_samples["inbox"] = inbox
            context_name = students[0].name if len(students) == 1 else ""
            inbox_messages = parse_messages(inbox)
            await self._collect_message_bodies(inbox_messages)  # ids only mean something in this school's session
            for message in inbox_messages:
                message.student_name = context_name
                messages.setdefault(message.identity(), message)

            await self._collect_notifications(data, students)

        data.messages = sorted(messages.values(), key=lambda m: m.sent_at, reverse=True)
        data.notifications.sort(key=lambda n: n.date, reverse=True)
        return data

    def _carry_over(self, data: WebtopData, keys: list[str]) -> None:
        """Copy the previous poll's data for these students into the new snapshot."""
        previous = self.data
        if previous is None:
            return
        for key in keys:
            student = previous.student(key)
            if student is None or data.student(key) is not None:
                continue
            data.students.append(student)
            for name in PER_STUDENT_FIELDS:
                source = getattr(previous, name)
                if key in source:
                    getattr(data, name)[key] = source[key]

    async def _enter_context(self, context: dict[str, Any]) -> bool:
        """Switch the session to a linked login; False when that school must be skipped this poll."""
        client = self.client
        target = str(context.get("studentId") or context.get("StudentId") or "")
        if not target:
            return False
        if target == str((client.session.raw_login_data or {}).get("id") or ""):
            return True  # already there; switching to yourself is a wasted (and rate-limited) call
        try:
            await client.switch_student(student_id=target)
        except WebtopLoginError as err:
            _LOGGER.warning(
                "Could not switch to the linked Webtop login for %s; skipping it this poll: %s",
                context.get("school_name") or "another school",
                err,
            )
            return False
        return True

    async def _collect_message_bodies(self, messages: list[Message]) -> None:
        """Attach the text of inbox messages, fetching the ones not seen before.

        On the portal, loading a message's text is what marks it as read: there is no
        separate "mark read" request when a message is opened. So the text of a message
        the user has not read yet is fetched only if they turned that option on. A
        message they already read is fetched freely, newest first, a few per poll.
        """
        budget = MESSAGE_BODIES_PER_POLL
        for message in sorted(messages, key=lambda m: m.sent_at, reverse=True):
            identity = message.identity()
            if identity in self._message_bodies:
                message.body = self._message_bodies[identity]
                continue
            if not message.message_id or budget <= 0:
                continue
            if not message.has_read and not self._read_unread:
                continue
            budget -= 1
            payload = await self._optional(
                "message text",
                lambda message_id=message.message_id: self._post(
                    "messageBox/GetMessagesInboxData",
                    {"MessageId": message_id, "FilterId": 0, "IsInbox": True, "hasRead": None},
                ),
                envelope="GetMessagesInboxData",
            )
            if payload is None:
                continue  # the request failed; try again on a later poll
            # An answer without readable text is remembered as empty, so it is not asked for every poll.
            body = parse_message_body(payload) or ""
            message.body = body
            self._message_bodies[identity] = body
            while len(self._message_bodies) > MESSAGE_BODY_CACHE_SIZE:
                self._message_bodies.pop(next(iter(self._message_bodies)))

    async def _collect_notifications(self, data: WebtopData, students: list[Student]) -> None:
        """Unread badges and the notification preview for the current session."""
        counters = await self._optional(
            "menu counters", lambda: self._post("Menu/GetMenuCounters", {}), envelope="GetMenuCounters"
        )
        counted: int | None = None
        if counters is not None:
            data.raw_samples["counters"] = counters
            parsed = parse_menu_counters(counters)
            counted = parsed.unread_notifications
            if parsed.unread_messages is not None:
                data.unread_messages = (data.unread_messages or 0) + parsed.unread_messages

        preview = await self._optional(
            "notification preview",
            self.client.get_preview_unread_notifications,
            envelope="GetPreviewUnreadNotifications",
        )
        if preview is not None:
            data.raw_samples["notifications"] = preview
            names = {student.student_id: student.name for student in students}
            for notification in parse_notifications(preview):
                notification.student_name = names.get(notification.student_id, "")
                data.notifications.append(notification)
            if counted is None:
                counted = parse_unread_count(preview)
        if counted is not None:
            data.unread_notifications = (data.unread_notifications or 0) + counted

    async def _post(self, path: str, payload: dict[str, Any]) -> Any:
        """Call an endpoint pywebtop has no method for."""
        response = await self.client.request("POST", API + path, json=payload)
        try:
            return response.json()
        except ValueError as err:
            raise WebtopRequestError(f"POST {path} did not return JSON") from err

    async def _collect_tests_board(
        self, data: WebtopData, institution: Any, now: Any
    ) -> tuple[list[SchoolEvent] | None, BellSchedule]:
        """Exams, events, vacations and the bell schedule for the session's school.

        Returns ``(None, {})`` when the school has no tests board or it could not be read.
        """
        if institution in (None, ""):
            _LOGGER.debug("Webtop login data has no institution code; skipping the tests board")
            return None, {}
        if self._board_unavailable.get(str(institution)) == now.date():
            return None, {}  # asked earlier today; this school does not publish a board
        events: dict[str, SchoolEvent] = {}
        bell: BellSchedule = {}
        answered = False
        month = now.replace(day=1, hour=12, minute=0, second=0, microsecond=0)
        for _ in range(TESTS_BOARD_MONTHS_AHEAD + 1):
            payload = await self._optional(
                "tests board",
                lambda when=month: self._post(
                    "shotef/EventsTestsBoard",
                    {
                        "institutionCode": institution,
                        "selectedDate": when.isoformat(),
                        "selectedView": 3,
                        "loadFilters": True,
                        "studyYear": school_year_for(when),
                    },
                ),
                envelope="EventsTestsBoard",
            )
            if payload is not None:
                data.raw_samples["tests_board"] = payload
                if not board_available(payload):
                    # Every list null: the school does not use the board. Do not ask again today.
                    self._board_unavailable[str(institution)] = now.date()
                    break
                answered = True
                bell = bell or parse_bell_schedule(payload)
                for event in parse_tests_board(payload):
                    events.setdefault(event.identity(), event)
            month = (month + timedelta(days=32)).replace(day=1)
        if not answered:
            return None, {}
        ordered = sorted(events.values(), key=lambda e: (e.start_date, e.from_hour if e.from_hour is not None else 99))
        return ordered, bell

    async def _bell_for(self, student: Student, board_bell: BellSchedule, institution: Any, now: Any) -> BellSchedule:
        """Lesson times for a student.

        Times typed for that student win. Otherwise the portal's are used, and the
        account-wide typed times fill in only when the school publishes none.
        """
        portal = await self._portal_bell(student, board_bell, institution, now)
        self.portal_bell[student.key] = portal
        return self._student_bell.get(student.key) or portal or self._default_bell

    async def _portal_bell(
        self, student: Student, board_bell: BellSchedule, institution: Any, now: Any
    ) -> BellSchedule:
        """The school's own lesson times, from wherever it publishes them.

        The weekly-schedule lookup is a request of its own, so it comes last and is made once a day.
        """
        if board_bell:
            return board_bell
        timetable = self._timetable_cache.get(student.key)
        if timetable and timetable[2]:
            return timetable[2]  # read off the timetable's own lesson labels
        if self._week_bell.get(student.key):
            return self._week_bell[student.key]  # the same labels, on this week's lessons
        cached = self._bell_cache.get(student.key)
        if cached and cached[0] == now.date():
            return cached[1]
        if institution in (None, "") or not student.has_class_params:
            return {}
        payload = await self._optional(
            f"lesson times for {student.name}",
            lambda: self._post(
                "dashboard/WeeklySchedualeForToday",
                {
                    "InstitutionCode": institution,
                    "SelectedDate": now.replace(microsecond=0).isoformat(),
                    "View": "daily",
                    "spaceId": 0,
                    "language": "he",
                    "item": "",
                    "ClassNum": student.class_number,
                    "ClassCode": student.class_code,
                    "ChildId": student.student_id,
                    "year": student.study_year or school_year_for(now),
                },
            ),
            envelope="WeeklySchedualeForToday",
        )
        if payload is None:
            return {}
        bell = parse_bell_schedule(payload)
        self._bell_cache[student.key] = (now.date(), bell)
        return bell

    async def _pupil_card(
        self, what: str, view: str, student: Student, year: int, module: int, *, week_index: int = 0
    ) -> Any:
        """Read one pupil-card view, sending the module id that view has on the portal.

        Schools grant each view separately. If the view's own id is blocked, the
        timetable's id is tried once: earlier versions sent it for every view, and
        it is known to work where the school allows the timetable.
        """
        for module_id in dict.fromkeys((module, MODULE_TIMETABLE)):
            remember = f"{student.key}:{view}:{module_id}"
            payload = await self._optional(
                what,
                lambda module_id=module_id: self._post(
                    f"PupilCard/{view}",
                    {
                        "weekIndex": week_index,
                        "viewType": 0,
                        "studyYear": year,
                        "studentID": student.student_id,
                        "classCode": student.class_code,
                        "moduleID": module_id,
                    },
                ),
                envelope=view,
                remember=remember,
            )
            if payload is not None or self._blocked.get(remember) != dt_util.now().date():
                return payload  # answered, or failed for a reason another module id will not fix
        return None

    async def _previous_week(self, student: Student, year: int, now: Any) -> list[Lesson]:
        """Last week's lessons, so that homework given late last week is still listed.

        Past weeks rarely change, so they are re-read only every few hours.
        """
        cached = self._previous_week_cache.get(student.key)
        if cached and now - cached[0] < timedelta(hours=PREVIOUS_WEEK_REFRESH_HOURS):
            return cached[1]
        payload = await self._pupil_card(
            f"last week's lessons for {student.name}",
            "GetPupilLessonsAndHomework",
            student,
            year,
            MODULE_WEEK,
            week_index=-1,
        )
        if not isinstance(payload, list):
            return cached[1] if cached else []
        lessons = parse_schedule(payload)
        self._previous_week_cache[student.key] = (now, lessons)
        return lessons

    async def _standing_timetable(self, student: Student, year: int, now: Any) -> list[Lesson] | None:
        """The weekday timetable (with rooms). It rarely changes, so it is read once a day."""
        cached = self._timetable_cache.get(student.key)
        if cached and cached[0] == now.date():
            return cached[1]
        payload = await self._optional(
            f"timetable for {student.name}",
            lambda: self.client.get_pupil_schedule(
                week_index=0,
                study_year=year,
                encrypted_student_id=student.student_id,
                class_code=student.class_code,
            ),
            envelope="GetPupilScheduale",
            remember=f"{student.key}:timetable",
        )
        if payload is None:
            return None
        lessons = parse_schedule(payload)
        self._timetable_cache[student.key] = (now.date(), lessons, parse_bell_schedule(payload))
        return lessons

    async def _collect_student(
        self, data: WebtopData, student: Student, *, now: Any, institution: Any, dashboard: Any
    ) -> None:
        client = self.client
        key = student.key
        today = now.strftime("%Y-%m-%d")
        data.homework[key] = []
        data.discipline[key] = []
        data.schedule[key] = []
        data.grades[key] = []
        data.changes[key] = []
        data.homework_ok[key] = data.discipline_ok[key] = data.schedule_ok[key] = False
        data.grades_ok[key] = data.changes_ok[key] = False

        if not student.has_class_params:
            _LOGGER.warning(
                "Webtop did not report a class for %s; homework, behaviour and timetable are unavailable",
                student.name or key,
            )
            return

        year = student.study_year or school_year_for(now)

        # This week's lessons, dated, with the homework and topic written on each one.
        week = await self._pupil_card(
            f"this week's lessons for {student.name}", "GetPupilLessonsAndHomework", student, year, MODULE_WEEK
        )
        standing = await self._standing_timetable(student, year, now)
        data.standing[key] = standing or []
        data.previous_schedule[key] = []
        if isinstance(week, list):
            data.raw_samples["lessons"] = week
            self._week_bell[key] = parse_bell_schedule(week)
            lessons = parse_schedule(week)
            fill_rooms(lessons, standing or [])
            data.schedule[key] = lessons
            data.schedule_ok[key] = True
            # Homework from last week and this one: Thursday's is still due on Sunday.
            earlier = await self._previous_week(student, year, now)
            fill_rooms(earlier, standing or [])
            data.previous_schedule[key] = earlier
            data.homework[key] = homework_from_lessons([*earlier, *lessons])
            data.homework_ok[key] = True
        else:
            # No dated week: the standing timetable, and today's homework from the dashboard.
            if standing is not None:
                data.schedule[key] = standing
                data.schedule_ok[key] = True
            if dashboard_allows(dashboard, "allowGetHomeWork"):
                payload = await self._optional(
                    f"homework for {student.name}",
                    lambda: client.get_homework(
                        encrypted_student_id=student.student_id,
                        class_code=student.class_code,
                        class_number=student.class_number if student.class_number is not None else 0,
                    ),
                    envelope="GetHomeWork",
                )
                if payload is not None and view_allowed(payload):
                    data.raw_samples["homework"] = payload
                    data.homework[key] = parse_homework(payload, date=today)
                    data.homework_ok[key] = True
        data.groups[key] = student_groups(data.schedule[key]) | student_groups(standing or [])

        if dashboard_allows(dashboard, "allowGetPupilDiciplineEvents"):
            # The pupil card lists the whole year; the dashboard widget only the latest few.
            payload = await self._pupil_card(
                f"behaviour events for {student.name}", "GetPupilDiciplineEvents", student, year, MODULE_BEHAVIOUR
            )
            if payload is None:
                payload = await self._optional(
                    f"recent behaviour events for {student.name}",
                    lambda: client.get_discipline_events(
                        encrypted_student_id=student.student_id, class_code=student.class_code
                    ),
                    envelope="GetPupilDiciplineEvents",
                )
            if payload is not None and view_allowed(payload):
                data.raw_samples["discipline"] = payload
                data.discipline[key] = parse_discipline(payload)
                data.discipline_ok[key] = True

        if dashboard_allows(dashboard, "allowGetGrades"):
            # Same split: every mark of the year on the pupil card, the latest few on the dashboard.
            payload = await self._pupil_card(
                f"grades for {student.name}", "GetPupilGrades", student, year, MODULE_GRADES
            )
            if payload is not None:
                data.pending[key] = parse_pending_evaluations(payload)
            else:
                payload = await self._optional(
                    f"recent grades for {student.name}",
                    lambda: self._post(
                        "dashboard/GetGrades", {"id": student.student_id, "ClassCode": student.class_code}
                    ),
                    envelope="GetGrades",
                )
            if payload is not None and view_allowed(payload):
                data.raw_samples["grades"] = payload
                data.grades[key] = parse_grades(payload)
                data.grades_ok[key] = True

        if institution not in (None, "") and dashboard_allows(dashboard, "allowChangesAndMessagesDataForToday"):
            payload = await self._optional(
                f"today's changes for {student.name}",
                lambda: self._post(
                    "dashboard/ChangesAndMessagesDataForToday",
                    {
                        "institutionCode": institution,
                        "selectedValue": f"{student.class_code}|{student.class_number}|{student.student_id}",
                        "typeView": 1,
                        "todayOnly": True,
                        "studentId": student.student_id,
                        "ClassCode": student.class_code,
                    },
                ),
                envelope="ChangesAndMessagesDataForToday",
            )
            if payload is not None and view_allowed(payload):
                data.raw_samples["changes"] = payload
                data.changes[key] = parse_changes(payload)
                data.changes_ok[key] = True

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
            if data.homework_ok.get(student.key):
                for item in data.homework[student.key]:
                    if self._first_sighting(seen, item.identity()):
                        self._fire(EVENT_NEW_HOMEWORK, student, item.as_dict())
            if data.discipline_ok.get(student.key):
                seen = self._seen_behaviour.setdefault(student.key, set())
                for record in data.discipline[student.key]:
                    if self._first_sighting(seen, record.identity()):
                        self._fire(EVENT_NEW_BEHAVIOUR, student, record.as_dict())
            if data.grades_ok.get(student.key):
                seen = self._seen_grades.setdefault(student.key, set())
                for grade in data.grades[student.key]:
                    if self._first_sighting(seen, grade.identity()):
                        self._fire(EVENT_NEW_GRADE, student, grade.as_dict())
            if data.calendar_ok.get(student.key):
                seen = self._seen_tests.setdefault(student.key, set())
                bell = data.bell.get(student.key, {})
                for event in data.calendar[student.key]:
                    if event.kind == "exam" and self._first_sighting(seen, event.identity()):
                        self._fire(EVENT_NEW_TEST, student, event.as_dict(bell))
        for message in data.messages:
            ident = message.identity()
            if ident in self._seen_messages:
                continue
            self._seen_messages.add(ident)
            if self._baseline_done:
                self.hass.bus.async_fire(
                    EVENT_NEW_MESSAGE,
                    {
                        "entry_id": self.config_entry.entry_id,
                        **message.as_dict(),
                        # Empty for a message not read yet, unless reading unread messages is switched on.
                        "body": message.body[:MESSAGE_BODY_EVENT_CHARS],
                    },
                )
        for notification in data.notifications:
            if self._first_sighting(self._seen_notifications, notification.identity()):
                self.hass.bus.async_fire(
                    EVENT_NEW_NOTIFICATION,
                    {"entry_id": self.config_entry.entry_id, **notification.as_dict()},
                )
        self._baseline_done = True

    def _first_sighting(self, seen: set[str], identity: str) -> bool:
        """Record an item; True when it is new and the baseline poll is behind us."""
        if identity in seen:
            return False
        seen.add(identity)
        return self._baseline_done

    def _fire(self, event_type: str, student: Student, payload: dict[str, Any]) -> None:
        self.hass.bus.async_fire(
            event_type,
            {
                "entry_id": self.config_entry.entry_id,
                "student": student.name,
                "student_key": student.key,
                **payload,
            },
        )
