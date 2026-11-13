"""Plain dataclasses parsed out of pywebtop's raw JSON responses.

pywebtop returns the server's JSON envelope untouched: ``{"status": bool,
"data": ..., "errorDescription": ..., "errorId": ...}``. Everything in this
module is a pure function over already-parsed JSON so it can be tested offline.

The field names below were taken from the SmartSchool web client and from
payloads other Webtop projects have published. Where a shape has not been
confirmed, the parser is deliberately tolerant and picks from several
candidate keys rather than failing.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any


class WebtopApiError(Exception):
    """The server answered with ``status: false``."""

    def __init__(self, what: str, description: str | None, error_id: Any = None) -> None:
        self.what = what
        self.description = description or ""
        self.error_id = error_id
        detail = self.description or "request rejected"
        super().__init__(f"{what}: {detail}")

    @property
    def is_blocked_view(self) -> bool:
        """The school switched this view off for parents and pupils."""
        text = self.description.casefold()
        return "blocked" in text or "nopermission" in text.replace(" ", "") or "permissiondenied" in text

    @property
    def looks_like_expired_session(self) -> bool:
        """A ``status: false`` with no error text is how a dead token presents."""
        return not self.description and self.error_id in (None, 0, "")


def unwrap(body: Any, what: str) -> Any:
    """Return the ``data`` of a Webtop envelope, raising on ``status: false``."""
    if not isinstance(body, dict):
        raise WebtopApiError(what, f"unexpected response type {type(body).__name__}", "type")
    if body.get("status") is False:
        raise WebtopApiError(what, body.get("errorDescription"), body.get("errorId"))
    return body.get("data")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return " ".join(value.split())
    return str(value)


def _pick(data: dict[str, Any], *names: str) -> Any:
    for name in names:
        value = data.get(name)
        if value not in (None, ""):
            return value
    return None


def _pick_text(data: dict[str, Any], *names: str) -> str:
    return _text(_pick(data, *names))


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes")
    return False


def _digest(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]


# Hebrew vowel points and cantillation, and invisible direction / zero-width marks.
_NAME_NOISE_RE = re.compile("[\u0591-\u05c7\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]")


def normalize_name(name: str | None) -> str:
    """A form of a name that survives spacing, case, vowel points and stray direction marks."""
    cleaned = _NAME_NOISE_RE.sub("", unicodedata.normalize("NFKC", name or ""))
    return " ".join(cleaned.split()).casefold()


_TIME_IN_TEXT_RE = re.compile(r"(?:^|[T ])(\d{1,2})[:.](\d{2})(?::\d{2})?")


def parse_time(value: Any) -> dt.time | None:
    """Parse "8:00", "08:00:00" or an ISO timestamp into a time of day."""
    if isinstance(value, dt.datetime):
        return value.time().replace(second=0, microsecond=0)
    if isinstance(value, dt.time):
        return value
    if not isinstance(value, str):
        return None
    match = _TIME_IN_TEXT_RE.search(value.strip())
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    return dt.time(hour, minute)


def format_time(value: Any) -> str:
    parsed = parse_time(value)
    return parsed.strftime("%H:%M") if parsed else ""


def parse_date(value: Any) -> dt.date | None:
    """Parse the date part of "2026-11-18T00:00:00" or "18/11/2026"."""
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    iso = re.match(r"(\d{4})-(\d{2})-(\d{2})", text)
    try:
        if iso:
            return dt.date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
        local = re.match(r"(\d{1,2})[/.](\d{1,2})[/.](\d{4})", text)
        if local:
            return dt.date(int(local.group(3)), int(local.group(2)), int(local.group(1)))
    except ValueError:
        return None
    return None


# ---------------------------------------------------------------------------
# students
# ---------------------------------------------------------------------------
@dataclass
class Student:
    """A child on the account, as listed by ``dashboard/InitDashboard``.

    ``student_id`` is the server's *encrypted* id; every per-student endpoint
    wants it back as ``id``. It is not stable across school years, so ``key``
    (a numeric id when the server provides one, otherwise a digest of the name
    and school) is what Home Assistant uses for devices and entities.
    """

    student_id: str
    key: str
    name: str = ""
    class_code: int | None = None
    class_number: int | None = None
    class_name: str = ""
    study_year: int | None = None
    school_name: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dashboard(cls, entry: dict[str, Any], *, school_name: str = "") -> Student:
        first = _pick_text(entry, "firstName", "FirstName", "first_name")
        last = _pick_text(entry, "lastName", "LastName", "last_name")
        name = _pick_text(entry, "studentName", "fullName", "name", "Name") or f"{first} {last}".strip()
        school = _pick_text(entry, "schoolName", "SchoolName", "school_name") or school_name
        encrypted = _pick_text(entry, "id", "Id", "encryptedId", "studentId", "StudentId")
        numeric = _as_int(_pick(entry, "studentId", "StudentId", "pupilId", "userId", "UserId"))
        key = str(numeric) if numeric is not None else _digest(normalize_name(name), normalize_name(school))
        return cls(
            student_id=encrypted,
            key=key,
            name=name,
            class_code=_as_int(_pick(entry, "classCode", "ClassCode")),
            class_number=_as_int(_pick(entry, "classNum", "classNumber", "ClassNumber", "ClassNum")),
            class_name=_pick_text(entry, "className", "ClassName", "class_name"),
            study_year=_as_int(_pick(entry, "studyYear", "StudyYear")),
            school_name=school,
            raw=entry,
        )

    @property
    def class_label(self) -> str:
        if self.class_name:
            return self.class_name
        if self.class_code is None or self.class_number is None:
            return ""
        letter = GRADE_LETTERS.get(self.class_code)
        if letter is None:
            return f"{self.class_code}-{self.class_number}"
        # Single letters take a geresh (ג'1); the two-letter grades already carry gershayim.
        return f"{letter}'{self.class_number}" if len(letter) == 1 else f"{letter} {self.class_number}"

    @property
    def has_class_params(self) -> bool:
        return bool(self.student_id) and self.class_code is not None


# Webtop's class code is the grade level: 1 = א ... 12 = י"ב.
GRADE_LETTERS = {
    1: "א", 2: "ב", 3: "ג", 4: "ד", 5: "ה", 6: "ו", 7: "ז", 8: "ח", 9: "ט", 10: "י", 11: 'י"א', 12: 'י"ב',
}  # fmt: skip


def view_allowed(data: Any) -> bool:
    """Dashboard widgets answer ``allowToViewThis: false`` when the school switched them off."""
    return not (isinstance(data, dict) and data.get("allowToViewThis") is False)


def dashboard_allows(dashboard: Any, flag: str) -> bool:
    """An ``InitDashboard`` permission flag such as ``allowGetGrades``; absent means allowed."""
    return not (isinstance(dashboard, dict) and dashboard.get(flag) is False)


_STUDENT_LIST_KEYS = ("childrens", "children", "students", "Students", "pupils", "Pupils")
_STUDENT_MARKER_KEYS = ("id", "Id", "classCode", "ClassCode", "studentId", "StudentId")


def parse_students(data: Any, *, school_name: str = "") -> list[Student]:
    """Find the student list in an ``InitDashboard`` payload.

    Live servers wrap it as ``data.childrens`` (sic); older ones returned a bare
    list. Anything else is searched for the first list of student-looking dicts.
    """
    entries: list[dict[str, Any]] = []
    if isinstance(data, list):
        entries = [e for e in data if isinstance(e, dict)]
    elif isinstance(data, dict):
        for key in _STUDENT_LIST_KEYS:
            if isinstance(data.get(key), list):
                entries = [e for e in data[key] if isinstance(e, dict)]
                break
        else:
            for value in data.values():
                if (
                    isinstance(value, list)
                    and value
                    and isinstance(value[0], dict)
                    and any(k in value[0] for k in _STUDENT_MARKER_KEYS)
                ):
                    entries = [e for e in value if isinstance(e, dict)]
                    break
    students = [Student.from_dashboard(e, school_name=school_name) for e in entries]
    return [s for s in students if s.student_id]


def student_from_login(login_data: dict[str, Any], *, school_name: str = "") -> Student | None:
    """Fallback: treat the logged-in session itself as the (single) student."""
    if not isinstance(login_data, dict) or not login_data.get("id"):
        return None
    return Student.from_dashboard(login_data, school_name=school_name)


# ---------------------------------------------------------------------------
# homework
# ---------------------------------------------------------------------------
# Teachers type "none" into the homework field instead of leaving it blank.
NO_HOMEWORK_VALUES = frozenset(
    v.casefold()
    for v in (
        "אין",
        "אין שיעורי בית",
        "אין שעורי בית",
        "אין שיעורים",
        "אין שעורים",
        "אין שב",
        "לא ניתנו שיעורי בית",
        "לא ניתנו שיעורים",
        "ללא שיעורים",
        "אין ש.ב",
        "אין ש״ב",
        "לא הוזן",
        "ללא",
        "ללא שיעורי בית",
        "none",
        "no homework",
        "n/a",
        "-",
        "--",
        ".",
    )
)


def is_no_homework(text: str) -> bool:
    normalised = _text(text).strip().strip(".!:;־-").casefold()
    return not normalised or normalised in NO_HOMEWORK_VALUES


@dataclass
class HomeworkItem:
    """One homework assignment from ``dashboard/GetHomeWork``."""

    subject: str
    homework: str
    teacher: str = ""
    hour: int | None = None
    status: str = ""
    date: str = ""
    # Every lesson number of the day the same homework was written on (a double lesson has two).
    hours: list[int] = field(default_factory=list)

    def identity(self) -> str:
        return _digest(self.subject, self.homework, self.date[:10])

    def as_dict(self) -> dict[str, Any]:
        return {
            "date": self.date[:10],
            "hour": self.hour,
            "hours": self.hours or ([self.hour] if self.hour is not None else []),
            "subject": self.subject,
            "teacher": self.teacher,
            "homework": self.homework,
            "status": self.status,
        }


def parse_homework(data: Any, *, date: str) -> list[HomeworkItem]:
    """Parse ``GetHomeWork``: ``data.dataTable[]`` rows for today, no per-row date.

    Row shape seen live: ``{"lesson", "teacher", "homeworkData", "lessonSubject",
    "hourNum", "status"}``.
    """
    rows: list[Any]
    if isinstance(data, dict):
        found = _pick(data, "dataTable", "DataTable", "homework", "items")
        rows = found if isinstance(found, list) else []
    elif isinstance(data, list):
        rows = data
    else:
        rows = []

    items: list[HomeworkItem] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        text = _pick_text(row, "homeworkData", "homeWork", "homework", "HomeWork", "Homework")
        if is_no_homework(text):
            continue
        items.append(
            HomeworkItem(
                subject=_pick_text(row, "lesson", "lessonSubject", "subject_name", "subjectName", "subject", "Subject")
                or "Unknown",
                homework=text,
                teacher=_pick_text(row, "teacher", "teacherName", "Teacher"),
                hour=_as_int(_pick(row, "hourNum", "hour", "lessonNum")),
                status=_pick_text(row, "status", "Status"),
                date=_pick_text(row, "date", "lessonDate", "dueDate") or date,
            )
        )
    return items


def homework_from_lessons(lessons: list[Any]) -> list[HomeworkItem]:
    """Homework written on dated lessons (``GetPupilLessonsAndHomework``), oldest first.

    A teacher fills in the same text on each lesson of a double period, so entries for
    the same day, subject and text are one assignment listing every lesson number.
    """
    merged: dict[tuple[str, str, str], HomeworkItem] = {}
    ordered = sorted(lessons, key=lambda x: (x.date, x.hour if x.hour is not None else 99))
    for lesson in ordered:
        if not lesson.date or is_no_homework(lesson.homework):
            continue
        key = (lesson.date[:10], normalize_name(lesson.subject), " ".join(lesson.homework.split()))
        item = merged.get(key)
        if item is None:
            item = merged[key] = HomeworkItem(
                subject=lesson.subject,
                homework=lesson.homework,
                teacher=lesson.teacher,
                hour=lesson.hour,
                date=lesson.date,
            )
        if lesson.hour is not None and lesson.hour not in item.hours:
            item.hours.append(lesson.hour)
    return list(merged.values())


# ---------------------------------------------------------------------------
# messages
# ---------------------------------------------------------------------------
@dataclass
class Message:
    """An inbox message from ``messageBox/GetMessagesInbox``.

    On the wire ``student_F_name`` / ``student_L_name`` hold the *sender's*
    name, and the boolean flags are ``0``/``1`` integers.
    """

    subject: str
    sender: str = ""
    sent_at: str = ""
    has_read: bool = False
    has_files: bool = False
    type_id: int | None = None
    student_name: str = ""

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Message:
        first = _pick_text(data, "student_F_name", "senderFirstName")
        last = _pick_text(data, "student_L_name", "senderLastName")
        sender = f"{last} {first}".strip() or _pick_text(data, "fromTitle", "userTitle", "senderName", "sender")
        return cls(
            subject=_pick_text(data, "subject", "Subject"),
            sender=sender,
            sent_at=_pick_text(data, "sendingDate", "SendingDate", "msgTime", "date"),
            has_read=_as_bool(data.get("hasRead", data.get("HasRead"))),
            has_files=_as_bool(data.get("filesWereAttached", data.get("hasFiles"))),
            type_id=_as_int(_pick(data, "typeID", "TypeID", "typeId")),
        )

    def identity(self) -> str:
        return _digest(self.subject, self.sender, self.sent_at[:19])

    def as_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "sender": self.sender,
            "sent_at": self.sent_at,
            "has_read": self.has_read,
            "has_files": self.has_files,
            "student": self.student_name,
        }


def parse_messages(data: Any) -> list[Message]:
    rows: list[Any]
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        found = _pick(data, "messages", "Messages", "dataTable", "items")
        rows = found if isinstance(found, list) else []
    else:
        rows = []
    return [Message.from_api(r) for r in rows if isinstance(r, dict)]


# ---------------------------------------------------------------------------
# discipline / behaviour events
# ---------------------------------------------------------------------------
@dataclass
class DisciplineEvent:
    """A behaviour record from ``dashboard/GetPupilDiciplineEvents``."""

    date: str
    event: str
    subject: str = ""
    teacher: str = ""
    remark: str = ""
    justified: bool | None = None
    hour: int | None = None
    event_id: str = ""

    def identity(self) -> str:
        if self.event_id:
            return _digest(self.event_id)
        return _digest(self.date[:19], self.event, self.subject, self.teacher, str(self.hour))

    def as_dict(self) -> dict[str, Any]:
        return {
            "date": self.date[:10],
            "hour": self.hour,
            "event": self.event,
            "subject": self.subject,
            "teacher": self.teacher,
            "remark": self.remark,
            "justified": self.justified,
        }


_EVENT_KEYS = ("eventType", "eventName", "event", "disciplineEvent", "type", "name", "title")
_DATE_KEYS = ("eventDate", "date", "lessonDate", "Date", "dateTime")


def _row_list(data: Any) -> list[dict[str, Any]]:
    """The first list of dicts in a payload (the payload itself, or one level down)."""
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        # "diciplineEvents" is the server's spelling. The dashboard nests it under a dataTable object.
        for key in ("diciplineEvents", "dataTable", "events", "items", "list", "rows"):
            value = data.get(key)
            if isinstance(value, list):
                return [r for r in value if isinstance(r, dict)]
            if isinstance(value, dict):
                nested = _row_list(value)
                if nested:
                    return nested
        for value in data.values():
            if isinstance(value, list) and value and isinstance(value[0], dict):
                return [r for r in value if isinstance(r, dict)]
    return []


def parse_discipline(data: Any) -> list[DisciplineEvent]:
    events: list[DisciplineEvent] = []
    for row in _row_list(data):
        event = _pick_text(row, *_EVENT_KEYS)
        date = _pick_text(row, *_DATE_KEYS)
        if not event and not date:
            continue
        justified_raw = row.get("isJustified", row.get("justified", row.get("approved")))
        events.append(
            DisciplineEvent(
                date=date,
                event=event or "Unknown",
                subject=_pick_text(row, "subject", "subjectName", "lesson", "lessonSubject", "subject_name"),
                teacher=_pick_text(row, "teacher", "teacherName", "Teacher"),
                remark=_pick_text(row, "remark", "remarks", "comment", "comments", "description", "notes"),
                justified=None if justified_raw is None else _as_bool(justified_raw),
                hour=_as_int(_pick(row, "hourNum", "hour")),
                event_id=_pick_text(row, "eventID", "eventId"),
            )
        )
    events.sort(key=lambda e: (e.date, e.hour if e.hour is not None else 0), reverse=True)
    return events


# ---------------------------------------------------------------------------
# schedule / timetable
# ---------------------------------------------------------------------------
@dataclass
class Lesson:
    """One timetable slot from ``PupilCard/GetPupilScheduale``."""

    date: str
    hour: int | None
    subject: str
    teacher: str = ""
    room: str = ""
    homework: str = ""
    changed: bool = False
    # Webtop weekday (1 = Sunday ... 7 = Saturday) for timetables keyed by day instead of date.
    weekday: int | None = None
    # "HH:MM" when the lesson row carries its own times; otherwise the bell schedule supplies them.
    start: str = ""
    end: str = ""
    # What the teacher wrote about the lesson (``descClass``), once it has been reported.
    topic: str = ""
    # The study group taught in this lesson; exams are addressed to study groups.
    study_group: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "hour": self.hour,
            "subject": self.subject,
            "teacher": self.teacher,
            "room": self.room,
            "homework": self.homework,
            "changed": self.changed,
            "start": self.start,
            "end": self.end,
            "topic": self.topic,
        }


_WEEKDAY_KEYS = ("dayIndex", "day", "day_no", "dayOfWeek", "dayNum", "weekDay")
_START_TIME_KEYS = ("fromTime", "startTime", "from_time", "start_time", "timeFrom")
_END_TIME_KEYS = ("toTime", "endTime", "to_time", "end_time", "timeTo")
_SUBJECT_KEYS = ("subject_name", "subjectName", "subject", "lesson", "lessonSubject", "Subject")
_HOUR_KEYS = ("hour", "hour_no", "hourNum", "hourNumber", "lessonNum", "HourNum")


def _teacher_of(row: dict[str, Any]) -> str:
    last = _pick_text(row, "teacherLastName", "teacher_last_name")
    first = _pick_text(row, "teacherPrivateName", "teacherFirstName", "teacher_private_name")
    return f"{last} {first}".strip() or _pick_text(row, "teacher", "teacherName", "Teacher")


def parse_schedule(data: Any) -> list[Lesson]:
    """Walk a timetable payload. Two real shapes, both a list of days:

    * ``PupilCard/GetPupilLessonsAndHomework`` - this week, dated::

        [{date, dayIndex, hoursData: [{hour, scheduale: [
            {subject_name, teacher, hour_no, descClass, homeWork, ...}]}]}]

    * ``PupilCard/GetPupilScheduale`` - the standing timetable, by weekday::

        [{dayIndex, hoursData: [{hour, scheduale: [
            {day, hour, subject, teacherLastName, teacherPrivateName, room}]}]}]

    ("scheduale" is the server's spelling.) Dates, weekdays and lesson numbers
    sit on ancestors of the lesson rows, so they are inherited while walking.
    """
    lessons: list[Lesson] = []

    def walk(node: Any, date: str, hour: int | None, weekday: int | None) -> None:
        if isinstance(node, dict):
            own_date = _pick_text(node, *_DATE_KEYS) or date
            own_hour = _as_int(_pick(node, *_HOUR_KEYS))
            if own_hour is None:
                own_hour = hour
            own_weekday = _as_int(_pick(node, *_WEEKDAY_KEYS))
            if own_weekday is None:
                own_weekday = weekday
            subject = _pick_text(node, *_SUBJECT_KEYS)
            if subject:
                lessons.append(
                    Lesson(
                        date=own_date,
                        hour=own_hour,
                        subject=subject,
                        teacher=_teacher_of(node),
                        room=_pick_text(node, "room", "roomName", "classRoom"),
                        homework=_pick_text(node, "homeWork", "homework", "homeworkData"),
                        changed=_as_bool(_pick(node, "isChanged", "changed", "hasChange", "isSubstitute")),
                        topic=_pick_text(node, "descClass", "lessonSubject", "topic"),
                        study_group=_as_int(_pick(node, "study_group", "studyGroupID", "studyGroupId")),
                        weekday=own_weekday,
                        start=format_time(_pick(node, *_START_TIME_KEYS)),
                        end=format_time(_pick(node, *_END_TIME_KEYS)),
                    )
                )
            for value in node.values():
                if isinstance(value, (dict, list)):
                    walk(value, own_date, own_hour, own_weekday)
        elif isinstance(node, list):
            for item in node:
                walk(item, date, hour, weekday)

    walk(data, "", None, None)
    return lessons


# ---------------------------------------------------------------------------
# notifications
# ---------------------------------------------------------------------------
@dataclass
class MenuCounters:
    """``Menu/GetMenuCounters``: the unread badges in the portal's menu."""

    unread_messages: int | None = None
    unread_notifications: int | None = None


def parse_menu_counters(data: Any) -> MenuCounters:
    if not isinstance(data, dict):
        return MenuCounters()
    return MenuCounters(
        unread_messages=_as_int(data.get("unreadMessages")),
        unread_notifications=_as_int(data.get("unreadNotifications")),
    )


@dataclass
class Notification:
    """One row of ``Menu/GetPreviewUnreadNotifications``."""

    date: str
    message: str
    module: str = ""
    kind: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"date": self.date, "message": self.message, "module": self.module, "kind": self.kind}


_NOTIFICATION_LISTS = (
    ("personalNotifications", "personal"),
    ("pedegogicalNotification", "pedagogical"),
    ("systemMessages", "system"),
)


def parse_notifications(data: Any) -> list[Notification]:
    """Newest first."""
    if not isinstance(data, dict):
        return []
    found = [
        Notification(
            date=_pick_text(row, "date", "sendingDate"),
            message=_pick_text(row, "message", "text", "title"),
            module=_pick_text(row, "moduleName"),
            kind=kind,
        )
        for key, kind in _NOTIFICATION_LISTS
        for row in (data.get(key) or [])
        if isinstance(row, dict)
    ]
    found.sort(key=lambda n: n.date, reverse=True)
    return found


def parse_unread_count(data: Any) -> int | None:
    """Count the unread notifications in a ``GetPreviewUnreadNotifications`` payload."""
    if isinstance(data, list):
        return len(data)
    if isinstance(data, dict):
        count = _as_int(_pick(data, "unreadCount", "count", "total", "totalUnread", "numOfUnread"))
        if count is not None:
            return count
        for key in ("notifications", "items", "list", "dataTable"):
            if isinstance(data.get(key), list):
                return len(data[key])
        if any(key in data for key, _ in _NOTIFICATION_LISTS):
            return len(parse_notifications(data))
        return None
    if isinstance(data, (int, float)) and not isinstance(data, bool):
        return int(data)
    return None


# ---------------------------------------------------------------------------
# bell schedule, tests board, grades, daily changes
# ---------------------------------------------------------------------------
# Lesson number -> (start, end). Comes with the tests board as ``data.hours``.
BellSchedule = dict[int, tuple[dt.time, dt.time]]


_TIMES_IN_NAME_RE = re.compile(r"(\d{1,2}[:.]\d{2})\s*[-\u2013]\s*(\d{1,2}[:.]\d{2})")


def _hour_row_times(row: dict[str, Any]) -> tuple[dt.time, dt.time] | None:
    """Times of one lesson slot: explicit ``fromTime``/``toTime``, else read from its label.

    The label looks like ``"3. 10:35-9:50"`` (end first, as it renders right-to-left),
    so the two times are simply ordered.
    """
    start = parse_time(_pick(row, *_START_TIME_KEYS))
    end = parse_time(_pick(row, *_END_TIME_KEYS))
    if start and end and start < end:
        return start, end
    label = _pick(row, "name", "hourName", "hour_name")
    match = _TIMES_IN_NAME_RE.search(label) if isinstance(label, str) else None
    if match:
        first, second = parse_time(match.group(1)), parse_time(match.group(2))
        if first and second and first != second:
            return min(first, second), max(first, second)
    return None


def parse_bell_schedule(data: Any, _depth: int = 0) -> BellSchedule:
    """Find the school's lesson times within a payload.

    They appear as ``hours: [{hour, name, fromTime, toTime}]`` on the tests board
    (``data.hours``) and the weekly schedule (``data.data.events.hours``), and as
    ``hourName`` labels on a timetable's ``hoursData`` rows. A school that never
    entered its times answers ``":"`` with no label, which yields an empty schedule.
    """
    if isinstance(data, list):
        bell: BellSchedule = {}
        for row in data:
            if not isinstance(row, dict):
                continue
            if isinstance(row.get("hoursData"), list):  # a timetable day
                for hour, times in parse_bell_schedule(row["hoursData"], _depth + 1).items():
                    bell.setdefault(hour, times)
                continue
            hour = _as_int(_pick(row, *_HOUR_KEYS))
            times = _hour_row_times(row)
            if hour is not None and times:
                bell[hour] = times
        return bell
    if not isinstance(data, dict) or _depth > 3:
        return {}
    for key in ("hours", "hoursList", "allHours", "scheduale"):
        found = parse_bell_schedule(data[key], _depth + 1) if isinstance(data.get(key), list) else {}
        if found:
            return found
    for value in data.values():
        if isinstance(value, dict):
            found = parse_bell_schedule(value, _depth + 1)
            if found:
                return found
    return {}


_MANUAL_SLOT_RE = re.compile(r"(?:(\d{1,2})\s*[=:]\s*)?(\d{1,2}[:.]\d{2})\s*[-\u2013]\s*(\d{1,2}[:.]\d{2})")


def parse_manual_bell(text: str | None) -> BellSchedule:
    """Lesson times typed by the user: ``08:00-08:45, 08:45-09:30`` or ``1=08:00-08:45`` per lesson.

    Unnumbered slots count up from the previous lesson number (starting at 1).
    Raises ValueError when the text has content but no usable slot, or a slot ends before it starts.
    """
    bell: BellSchedule = {}
    cleaned = (text or "").strip()
    if not cleaned:
        return bell
    number = 0
    for part in re.split(r"[,;\n]+", cleaned):
        if not part.strip():
            continue
        match = _MANUAL_SLOT_RE.fullmatch(part.strip())
        if not match:
            raise ValueError(f"cannot read lesson time {part.strip()!r}")
        number = int(match.group(1)) if match.group(1) else number + 1
        start, end = parse_time(match.group(2)), parse_time(match.group(3))
        if start is None or end is None or start >= end:
            raise ValueError(f"lesson {number} must end after it starts")
        bell[number] = (start, end)
    return bell


def board_available(data: Any) -> bool:
    """False when the tests board answered with every list null (the school does not use it)."""
    if not isinstance(data, dict):
        return False
    return any(data.get(key) is not None for key in ("exams", "events", "vactications", "vacations", "hours"))


@dataclass
class SchoolEvent:
    """An exam, school event or vacation from ``shotef/EventsTestsBoard``."""

    kind: str  # "exam" | "event" | "vacation"
    title: str
    start_date: dt.date
    end_date: dt.date
    subject: str = ""
    # The board's own label for an exam: מבחן, בוחן, עבודה להגשה ...
    type: str = ""
    from_hour: int | None = None
    to_hour: int | None = None
    note: str = ""
    room: str = ""
    # (class code, class number) pairs the item lists; empty means the whole school.
    classes: tuple[tuple[int | None, int | None], ...] = ()
    # Study groups the item is addressed to. An exam for an elective lists every class
    # its pupils come from, so the groups, not the classes, say who actually sits it.
    groups: frozenset[int] = frozenset()
    event_id: str = ""

    def identity(self) -> str:
        if self.event_id:
            return _digest(self.kind, self.event_id)
        return _digest(self.kind, self.title, self.subject, self.start_date.isoformat(), str(self.from_hour))

    def applies_to(self, student: Student, student_groups: frozenset[int] = frozenset()) -> bool:
        if self.groups and student_groups:
            return bool(self.groups & student_groups)
        if not self.classes or student.class_code is None:
            return True
        for code, number in self.classes:
            if code == student.class_code and (number is None or number == student.class_number):
                return True
        return False

    def times(self, bell: BellSchedule) -> tuple[dt.time, dt.time] | None:
        """Start and end time of day, when lesson numbers and a bell schedule are known.

        ``to_hour`` is the last lesson of the item, inclusive: an exam "from 1 to 3"
        is labelled by the portal with lesson 3's own name as its end.
        """
        if self.from_hour is None or self.from_hour not in bell:
            return None
        start = bell[self.from_hour][0]
        last = self.to_hour if self.to_hour is not None and self.to_hour >= self.from_hour else self.from_hour
        end = bell.get(last, bell[self.from_hour])[1]
        return (start, end) if start < end else None

    @property
    def summary(self) -> str:
        """One line for a calendar or a notification: "מבחן: היסטוריה - פרקים 4-6"."""
        details = [part for part in (self.subject, self.title) if part and part != self.type]
        details = list(dict.fromkeys(details))
        if self.type:
            return f"{self.type}: {' - '.join(details)}" if details else self.type
        return " - ".join(details) or self.kind

    def as_dict(self, bell: BellSchedule | None = None) -> dict[str, Any]:
        times = self.times(bell or {})
        return {
            "kind": self.kind,
            "type": self.type,
            "title": self.title,
            "subject": self.subject,
            "summary": self.summary,
            "date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
            "from_hour": self.from_hour,
            "to_hour": self.to_hour,
            "start": times[0].strftime("%H:%M") if times else "",
            "end": times[1].strftime("%H:%M") if times else "",
            "room": self.room,
            "note": self.note,
        }


_EVENT_START_KEYS = ("date", "examDate", "eventDate", "from_date", "fromDate", "startDate", "start_date")
_EVENT_END_KEYS = ("to_date", "toDate", "endDate", "end_date")
_FROM_HOUR_KEYS = ("fromHour", "from_hour", "startHour", "hour", "hourNum")
_TO_HOUR_KEYS = ("toHour", "to_hour", "endHour")
_BOARD_LISTS = (("exams", "exam"), ("events", "event"), ("vactications", "vacation"), ("vacations", "vacation"))


def _classes_of(row: dict[str, Any]) -> tuple[tuple[int | None, int | None], ...]:
    pairs: list[tuple[int | None, int | None]] = []
    listed = _pick(row, "classes", "classesList")
    if isinstance(listed, list):
        for item in listed:
            if isinstance(item, dict):
                code = _as_int(_pick(item, "classCode", "class_Code", "ClassCode"))
                if code is not None:
                    pairs.append((code, _as_int(_pick(item, "classNumber", "classNum", "class_Num", "ClassNumber"))))
            elif isinstance(item, str) and "|" in item:
                code, _, number = item.partition("|")
                pairs.append((_as_int(code), _as_int(number)))
    if not pairs:
        # Only a grade level on the row itself: the whole grade.
        own_code = _as_int(_pick(row, "classCode", "class_Code", "ClassCode"))
        if own_code is not None:
            pairs.append((own_code, _as_int(_pick(row, "classNumber", "classNum", "class_Num", "ClassNumber"))))
    return tuple(dict.fromkeys(pairs))


def _groups_of(row: dict[str, Any]) -> frozenset[int]:
    groups: set[int] = set()
    own = _as_int(_pick(row, "studyGroupID", "studyGroupId", "study_group"))
    if own:
        groups.add(own)
    listed = _pick(row, "groupsList", "studyGroups")
    if isinstance(listed, list):
        for item in listed:
            group = _as_int(item.get("id")) if isinstance(item, dict) else _as_int(item)
            if group:
                groups.add(group)
    return frozenset(groups)


def parse_tests_board(data: Any) -> list[SchoolEvent]:
    """Exams, events and vacations from an ``EventsTestsBoard`` payload.

    A real exam row: ``{id, title, subject, subjectID, type, typeID, date, fromHour, toHour,
    classCode, classes: [{classCode, classNumber}], studyGroupID, groupsList: [{id, title}],
    roomName}``. ``subject`` is often empty; ``subjectID`` then points into ``data.subjects``.
    """
    if not isinstance(data, dict):
        return []
    subjects = {
        _as_int(row.get("id")): _text(row.get("title"))
        for row in data.get("subjects") or []
        if isinstance(row, dict)
    }
    found: list[SchoolEvent] = []
    for key, kind in _BOARD_LISTS:
        rows = data.get(key)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            start = parse_date(_pick(row, *_EVENT_START_KEYS))
            if start is None:
                continue
            end = parse_date(_pick(row, *_EVENT_END_KEYS)) or start
            subject = _pick_text(row, "subject", "subjectName", "subject_name", "lesson", "profession")
            subject = subject or subjects.get(_as_int(_pick(row, "subjectID", "subjectId")), "")
            label = _pick_text(row, "type", "typeName")
            title = _pick_text(row, "title", "name", "eventName", "content", "description")
            found.append(
                SchoolEvent(
                    kind=kind,
                    title=title or subject or label or kind,
                    start_date=start,
                    end_date=max(start, end),
                    subject=subject,
                    type=label,
                    from_hour=_as_int(_pick(row, *_FROM_HOUR_KEYS)),
                    to_hour=_as_int(_pick(row, *_TO_HOUR_KEYS)),
                    note=_pick_text(row, "note", "remark", "remarks", "comment", "details"),
                    room=_pick_text(row, "roomName", "room"),
                    classes=_classes_of(row),
                    groups=_groups_of(row),
                    event_id=_pick_text(row, "id", "eventGroupID"),
                )
            )
    found.sort(key=lambda e: (e.start_date, e.from_hour if e.from_hour is not None else 99))
    return found


@dataclass
class Grade:
    """A mark from ``dashboard/GetGrades`` (latest) or ``PupilCard/GetPupilGrades``."""

    subject: str
    grade: str
    title: str = ""
    type: str = ""
    date: str = ""
    weight: str = ""
    teacher: str = ""
    remark: str = ""
    evaluation_id: str = ""

    def identity(self) -> str:
        # The grade is part of the identity so that a corrected mark is announced again.
        if self.evaluation_id:
            return _digest(self.evaluation_id, self.grade)
        return _digest(self.subject, self.title, self.date[:10], self.grade)

    def as_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "grade": self.grade,
            "title": self.title,
            "type": self.type,
            "date": self.date[:10],
            "weight": self.weight,
            "teacher": self.teacher,
            "remark": self.remark,
        }


def parse_grades(data: Any) -> list[Grade]:
    """Graded evaluations, newest first.

    A real row: ``{evaluationID, title, date, type, subject, level, grade, gradeTranslation,
    assessment, weight, teacherFirstName, teacherLastName, remark, components}``. The pupil
    card also lists evaluations that have no mark yet (``grade: null``); those are skipped.
    """
    grades: list[Grade] = []
    for row in _row_list(data):
        subject = _pick_text(row, "subject", "subjectName", "subject_name", "lesson")
        value = _pick_text(
            row, "grade", "gradeTranslation", "assessment", "numeric_grade", "numGrade", "mark", "textGrade"
        )
        if not subject or not value:
            continue
        level = _pick_text(row, "level", "subjectLevel")
        if level and level not in subject:
            subject = f"{subject} ({level})"
        last = _pick_text(row, "teacherLastName", "family_name")
        first = _pick_text(row, "teacherFirstName", "private_name")
        grades.append(
            Grade(
                subject=subject,
                grade=value,
                title=_pick_text(row, "title", "eventName", "name", "gradeTitle"),
                type=_pick_text(row, "type", "gradeType", "eventType"),
                date=_pick_text(row, "date", "gradeDate", "eventDate"),
                weight=_pick_text(row, "weight", "percent"),
                teacher=f"{last} {first}".strip() or _pick_text(row, "teacher", "teacherName"),
                remark=_pick_text(row, "remark", "remarks", "comment", "note"),
                evaluation_id=_pick_text(row, "evaluationID", "evaluationId"),
            )
        )
    grades.sort(key=lambda g: g.date, reverse=True)
    return grades


def parse_pending_evaluations(data: Any) -> list[SchoolEvent]:
    """Evaluations on the pupil card that have no mark yet: tests set for the pupil.

    The grades list includes them with ``grade: null``. They carry a date, a type and a
    subject but no lesson numbers, so they make all-day entries. This is what tells a
    parent about upcoming tests at a school that does not use the tests board.
    """
    found: list[SchoolEvent] = []
    for row in _row_list(data):
        if _pick(row, "grade", "gradeTranslation", "assessment") is not None:
            continue
        date = parse_date(_pick(row, "date", "gradeDate", "eventDate"))
        subject = _pick_text(row, "subject", "subjectName", "subject_name")
        if date is None or not subject:
            continue
        label = _pick_text(row, "type", "gradeType")
        found.append(
            SchoolEvent(
                kind="exam",
                title=_pick_text(row, "title", "eventName", "name") or subject,
                start_date=date,
                end_date=date,
                subject=subject,
                type=label,
                event_id=_pick_text(row, "evaluationID", "evaluationId"),
            )
        )
    found.sort(key=lambda e: e.start_date)
    return found


@dataclass
class ScheduleChange:
    """One row of today's changes board: a substitution, cancellation, exam, event or notice."""

    kind: str  # "change" | "message" | "exam" | "event"
    text: str
    hour: int | None = None
    teacher: str = ""
    room: str = ""
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "text": self.text,
            "hour": self.hour,
            "teacher": self.teacher,
            "room": self.room,
            "note": self.note,
        }


_CHANGE_LISTS = (("changes", "change"), ("messages", "message"), ("exams", "exam"), ("events", "event"))


def parse_changes(data: Any) -> list[ScheduleChange]:
    """Rows of ``ChangesAndMessagesDataForToday``, ordered by lesson number."""
    if not isinstance(data, dict):
        return []
    rows_out: list[ScheduleChange] = []
    for key, kind in _CHANGE_LISTS:
        rows = data.get(key)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            last = _pick_text(row, "lastName", "teacherLastName")
            first = _pick_text(row, "privateName", "firstName", "teacherFirstName")
            text = _pick_text(
                row, "content", "title", "message", "text", "subject", "subject_name", "name", "changeType", "type"
            )
            rows_out.append(
                ScheduleChange(
                    kind=kind,
                    text=text or kind,
                    hour=_as_int(_pick(row, "hour", "from_hour", "fromHour", "hourNum")),
                    teacher=f"{last} {first}".strip() or _pick_text(row, "teacher", "teacherName"),
                    room=_pick_text(row, "room", "roomName"),
                    note=_pick_text(row, "note", "remark", "remarks"),
                )
            )
    rows_out.sort(key=lambda c: c.hour if c.hour is not None else 99)
    return rows_out


def student_groups(lessons: list[Any]) -> frozenset[int]:
    """The study groups a student attends, as seen on their lessons."""
    return frozenset(lesson.study_group for lesson in lessons if lesson.study_group)


def fill_rooms(dated: list[Any], standing: list[Any]) -> None:
    """Copy rooms from the standing timetable onto this week's lessons (which carry none).

    Matched by weekday and lesson number; when a slot holds several parallel lessons
    the subject has to match as well.
    """
    by_slot: dict[tuple[int | None, int | None], list[Any]] = {}
    for lesson in standing:
        if lesson.room:
            by_slot.setdefault((lesson.weekday, lesson.hour), []).append(lesson)
    for lesson in dated:
        if lesson.room:
            continue
        candidates = by_slot.get((lesson.weekday, lesson.hour), [])
        if len(candidates) > 1:
            candidates = [c for c in candidates if normalize_name(c.subject) == normalize_name(lesson.subject)]
        if len(candidates) == 1:
            lesson.room = candidates[0].room
