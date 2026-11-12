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


def normalize_name(name: str | None) -> str:
    return " ".join((name or "").split()).casefold()


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
        if self.class_code is not None and self.class_number is not None:
            return f"{self.class_code}-{self.class_number}"
        return ""

    @property
    def has_class_params(self) -> bool:
        return bool(self.student_id) and self.class_code is not None


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

    def identity(self) -> str:
        return _digest(self.subject, self.homework)

    def as_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "hour": self.hour,
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

    def identity(self) -> str:
        return _digest(self.date[:19], self.event, self.subject, self.teacher)

    def as_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "event": self.event,
            "subject": self.subject,
            "teacher": self.teacher,
            "remark": self.remark,
            "justified": self.justified,
        }


_EVENT_KEYS = ("eventName", "event", "disciplineEvent", "eventType", "type", "name", "title")
_DATE_KEYS = ("eventDate", "date", "lessonDate", "Date", "dateTime")


def _row_list(data: Any) -> list[dict[str, Any]]:
    """The first list of dicts in a payload (the payload itself, or one level down)."""
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        for key in ("dataTable", "events", "items", "list", "rows"):
            if isinstance(data.get(key), list):
                return [r for r in data[key] if isinstance(r, dict)]
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
        justified_raw = _pick(row, "justified", "isJustified", "approved")
        events.append(
            DisciplineEvent(
                date=date,
                event=event or "Unknown",
                subject=_pick_text(row, "subject", "subjectName", "lesson", "lessonSubject", "subject_name"),
                teacher=_pick_text(row, "teacher", "teacherName", "Teacher"),
                remark=_pick_text(row, "remark", "remarks", "comment", "comments", "description", "notes"),
                justified=None if justified_raw is None else _as_bool(justified_raw),
            )
        )
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
        }


_WEEKDAY_KEYS = ("day", "dayIndex", "dayOfWeek", "dayNum", "weekDay")
_START_TIME_KEYS = ("fromTime", "startTime", "from_time", "start_time", "timeFrom")
_END_TIME_KEYS = ("toTime", "endTime", "to_time", "end_time", "timeTo")
_SUBJECT_KEYS = ("subject_name", "subjectName", "subject", "lesson", "lessonSubject", "Subject")
_HOUR_KEYS = ("hourNum", "hour", "hourNumber", "lessonNum", "HourNum")


def parse_schedule(data: Any) -> list[Lesson]:
    """Tolerant walk of a timetable payload.

    The known shape (shared with ``GetPupilLessonsAndHomework``) is
    ``[{date, hoursData: [{hourNum, scheduale: [{subject_name, teacher, ...}]}]}]``
    with the server's own spelling of "schedule". Dates and hour numbers sit on
    ancestors of the lesson dicts, so they are inherited while walking.
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
                        teacher=_pick_text(node, "teacher", "teacherName", "Teacher"),
                        room=_pick_text(node, "room", "roomName", "className", "classRoom"),
                        homework=_pick_text(node, "homeWork", "homework", "homeworkData"),
                        changed=_as_bool(_pick(node, "isChanged", "changed", "hasChange", "isSubstitute")),
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
        return None
    if isinstance(data, (int, float)) and not isinstance(data, bool):
        return int(data)
    return None


# ---------------------------------------------------------------------------
# bell schedule, tests board, grades, daily changes
# ---------------------------------------------------------------------------
# Lesson number -> (start, end). Comes with the tests board as ``data.hours``.
BellSchedule = dict[int, tuple[dt.time, dt.time]]


def parse_bell_schedule(data: Any) -> BellSchedule:
    """Find ``[{hour, fromTime, toTime}]`` anywhere near the top of a payload."""
    candidates: list[Any] = []
    if isinstance(data, dict):
        candidates = [data.get(k) for k in ("hours", "hoursList", "hourSettings", "allHours")]
        candidates += [v for v in data.values() if isinstance(v, list)]
    elif isinstance(data, list):
        candidates = [data]
    for rows in candidates:
        if not isinstance(rows, list):
            continue
        bell: BellSchedule = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            hour = _as_int(_pick(row, *_HOUR_KEYS))
            start = parse_time(_pick(row, *_START_TIME_KEYS))
            end = parse_time(_pick(row, *_END_TIME_KEYS))
            if hour is not None and start and end and start < end:
                bell[hour] = (start, end)
        if bell:
            return bell
    return {}


@dataclass
class SchoolEvent:
    """An exam, school event or vacation from ``shotef/EventsTestsBoard``."""

    kind: str  # "exam" | "event" | "vacation"
    title: str
    start_date: dt.date
    end_date: dt.date
    subject: str = ""
    from_hour: int | None = None
    to_hour: int | None = None
    note: str = ""
    # (class code, class number) pairs the item is for; empty means the whole school.
    classes: tuple[tuple[int | None, int | None], ...] = ()

    def identity(self) -> str:
        return _digest(self.kind, self.title, self.subject, self.start_date.isoformat(), str(self.from_hour))

    def applies_to(self, student: Student) -> bool:
        if not self.classes or student.class_code is None:
            return True
        for code, number in self.classes:
            if code == student.class_code and (number is None or number == student.class_number):
                return True
        return False

    def times(self, bell: BellSchedule) -> tuple[dt.time, dt.time] | None:
        """Start and end time of day, when lesson numbers and a bell schedule are known."""
        if self.from_hour is None or self.from_hour not in bell:
            return None
        start = bell[self.from_hour][0]
        # The client treats ``to_hour`` as exclusive (it looks up ``to_hour - 1``).
        last = self.to_hour - 1 if self.to_hour is not None and self.to_hour > self.from_hour else self.from_hour
        end = bell.get(last, bell[self.from_hour])[1]
        return (start, end) if start < end else None

    def as_dict(self, bell: BellSchedule | None = None) -> dict[str, Any]:
        times = self.times(bell or {})
        return {
            "kind": self.kind,
            "title": self.title,
            "subject": self.subject,
            "date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
            "from_hour": self.from_hour,
            "to_hour": self.to_hour,
            "start": times[0].strftime("%H:%M") if times else "",
            "end": times[1].strftime("%H:%M") if times else "",
            "note": self.note,
        }


_EVENT_START_KEYS = ("date", "examDate", "eventDate", "from_date", "fromDate", "startDate", "start_date")
_EVENT_END_KEYS = ("to_date", "toDate", "endDate", "end_date")
_FROM_HOUR_KEYS = ("fromHour", "from_hour", "startHour", "hour", "hourNum")
_TO_HOUR_KEYS = ("toHour", "to_hour", "endHour")
_BOARD_LISTS = (("exams", "exam"), ("events", "event"), ("vactications", "vacation"), ("vacations", "vacation"))


def _classes_of(row: dict[str, Any]) -> tuple[tuple[int | None, int | None], ...]:
    pairs: list[tuple[int | None, int | None]] = []
    own_code = _as_int(_pick(row, "classCode", "class_Code", "ClassCode"))
    if own_code is not None:
        pairs.append((own_code, _as_int(_pick(row, "classNumber", "classNum", "class_Num", "ClassNumber"))))
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
    return tuple(dict.fromkeys(pairs))


def parse_tests_board(data: Any) -> list[SchoolEvent]:
    """Exams, events and vacations from an ``EventsTestsBoard`` payload."""
    if not isinstance(data, dict):
        return []
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
            title = _pick_text(row, "title", "name", "eventName", "content", "description", "type") or subject
            found.append(
                SchoolEvent(
                    kind=kind,
                    title=title or kind,
                    start_date=start,
                    end_date=max(start, end),
                    subject=subject,
                    from_hour=_as_int(_pick(row, *_FROM_HOUR_KEYS)),
                    to_hour=_as_int(_pick(row, *_TO_HOUR_KEYS)),
                    note=_pick_text(row, "note", "remark", "remarks", "comment", "details"),
                    classes=_classes_of(row),
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

    def identity(self) -> str:
        return _digest(self.subject, self.title, self.date[:10], self.grade)

    def as_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "grade": self.grade,
            "title": self.title,
            "type": self.type,
            "date": self.date,
            "weight": self.weight,
            "teacher": self.teacher,
            "remark": self.remark,
        }


def parse_grades(data: Any) -> list[Grade]:
    """Newest first."""
    grades: list[Grade] = []
    for row in _row_list(data):
        subject = _pick_text(row, "subject", "subjectName", "subject_name", "lesson")
        value = _pick_text(row, "grade", "numeric_grade", "numGrade", "mark", "assessment", "textGrade")
        if not subject or not value:
            continue
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
            )
        )
    grades.sort(key=lambda g: g.date, reverse=True)
    return grades


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
