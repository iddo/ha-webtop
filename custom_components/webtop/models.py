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

import hashlib
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

    def as_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "hour": self.hour,
            "subject": self.subject,
            "teacher": self.teacher,
            "room": self.room,
            "homework": self.homework,
            "changed": self.changed,
        }


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

    def walk(node: Any, date: str, hour: int | None) -> None:
        if isinstance(node, dict):
            own_date = _pick_text(node, *_DATE_KEYS) or date
            own_hour = _as_int(_pick(node, *_HOUR_KEYS))
            if own_hour is None:
                own_hour = hour
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
                    )
                )
            for value in node.values():
                if isinstance(value, (dict, list)):
                    walk(value, own_date, own_hour)
        elif isinstance(node, list):
            for item in node:
                walk(item, date, hour)

    walk(data, "", None)
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
