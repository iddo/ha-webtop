"""Shared fixtures: a fake pywebtop client wired into the integration."""

from __future__ import annotations

import threading
import time
from collections.abc import Generator
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from pytest_homeassistant_custom_component.common import MockConfigEntry
from webtop import WebtopLoginError, WebtopRequestError, WebtopSession

from custom_components.webtop.const import CONF_DATA, CONF_PASSWORD, CONF_USERNAME, DEFAULT_DATA, DOMAIN
from custom_components.webtop.models import Student

ENCRYPTED_ID_A = "enc-student-a=="
ENCRYPTED_ID_B = "enc-student-b=="
SCHOOL = "בית ספר לדוגמה"

# Every fixture below mirrors the structure of a real (anonymized) capture; the values are invented.
LOGIN_DATA = {
    "token": "tok-1",
    "userId": "u-42",
    "id": "enc-parent-login==",
    "studentId": 0,
    "schoolId": 7,
    "schoolName": SCHOOL,
    "firstName": "דנה",
    "lastName": "לוי",
    "fullName": "דנה לוי",
    "userType": 2,
    "isTeacher": 2,
    "classCode": "0",
    "classNumber": 0,
    "institutionCode": 440099,
    "cellphone": "0520000000",
}

DASHBOARD = {
    "status": True,
    "data": {
        "childrens": [
            {"id": ENCRYPTED_ID_A, "firstName": "נועם", "lastName": "לוי", "classCode": 3, "classNum": 2,
             "cellphone": "0521111111"},
            {"id": ENCRYPTED_ID_B, "firstName": "מאיה", "lastName": "לוי", "classCode": 6, "classNum": 1,
             "cellphone": "0522222222"},
        ],
        "allowShotefSchedualeDataForToday": True,
        "allowChangesAndMessagesDataForToday": True,
        "allowGetHomeWork": True,
        "allowGetPupilDiciplineEvents": True,
        "allowGetGrades": True,
        "isOrt": False,
        "shortcuts": [],
    },
    "message": None,
    "errorId": None,
    "errorDescription": None,
    "errorHTML": None,
}  # fmt: skip
# The portal gives children no numeric id, so the integration keys them by name and school.
KEY_A = Student.from_dashboard(DASHBOARD["data"]["childrens"][0], school_name=SCHOOL).key
KEY_B = Student.from_dashboard(DASHBOARD["data"]["childrens"][1], school_name=SCHOOL).key


# Study group ids, per subject, for student A. Exams are addressed to study groups.
GROUPS = {"תורה": 3003, "מתמטיקה": 3001, "אנגלית": 3002, "מדעים": 3004}


def _week_lesson(
    day: int, hour: int, subject: str, teacher: str, homework: str = "", topic: str = ""
) -> dict[str, Any]:
    return {
        "files": [],
        "subject_name": subject,
        "subject_level": "",
        "teacher": teacher,
        "day_no": day,
        "hour_no": hour,
        "hour_name": None,
        "study_group": GROUPS[subject],
        "descClass": topic,
        "homeWork": homework,
        "statClass": 1 if topic else 0,
        "file_name": None,
    }


def _week(lessons_by_date: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """A ``GetPupilLessonsAndHomework`` week: Sunday 8 to Friday 13 November 2026, ten lesson slots a day."""
    days = []
    for index, day in enumerate(range(8, 14), 1):
        date = f"2026-11-{day:02d}"
        rows = lessons_by_date.get(date, [])
        days.append(
            {
                "date": f"{date}T00:00:00+02:00",
                "dayIndex": index,
                "hoursData": [
                    {"hour": hour, "hourName": None, "scheduale": [r for r in rows if r["hour_no"] == hour]}
                    for hour in range(1, 11)
                ],
            }
        )
    return {"status": True, "data": days, "message": None, "errorId": None, "errorDescription": None}


# PupilCard/GetPupilLessonsAndHomework for student A: the frozen "today" is Tuesday 10 November.
LESSONS_WEEK = _week(
    {
        "2026-11-09": [_week_lesson(2, 1, "תורה", "שרה", "לקרוא פרק ג", "פרשת השבוע")],
        "2026-11-10": [
            _week_lesson(3, 1, "מתמטיקה", "רחל", "עמוד 45 תרגילים 1-5", "שברים"),
            _week_lesson(3, 2, "אנגלית", "דוד", "אין", "Unit 3"),
        ],
        "2026-11-11": [_week_lesson(4, 1, "מדעים", "יעל")],
    }
)
LESSONS_WEEK_EMPTY = _week({})

# PupilCard/GetPupilScheduale: the standing timetable, keyed by weekday, no dates. Some schools
# label each lesson slot with its times ("1. 8:45-8:00", end first); others leave hourName null.
_BELL = [("08:00", "08:45"), ("08:45", "09:30"), ("09:50", "10:35"), ("10:35", "11:20")]
_TIMETABLE_SLOTS = {(3, 1): ("מתמטיקה", "חדר 12"), (3, 2): ("אנגלית", "מעבדה 3"), (4, 1): ("מתמטיקה", None)}


def _timetable(labels: bool) -> dict[str, Any]:
    days = []
    for day in range(1, 7):
        hours = []
        for hour, (start, end) in enumerate(_BELL[:3], 1):
            slot = _TIMETABLE_SLOTS.get((day, hour))
            hours.append(
                {
                    "hour": hour,
                    "hourName": f"{hour}. {end.lstrip('0')}-{start.lstrip('0')}" if labels else None,
                    "scheduale": [
                        {"day": day, "hour": hour, "subject": slot[0], "subjectLevel": "",
                         "teacherLastName": "כהן", "teacherPrivateName": "רחל", "room": slot[1]}
                    ] if slot else [],
                }
            )  # fmt: skip
        days.append({"dayIndex": day, "hoursData": hours})
    return {"status": True, "data": days}


TIMETABLE = _timetable(labels=False)
TIMETABLE_WITH_LABELS = _timetable(labels=True)

# dashboard/GetHomeWork: today only, no dates. Used when the dated week is unavailable.
HOMEWORK_A = {
    "status": True,
    "data": {
        "allowToViewThis": True,
        "dataTable": [
            {"lesson": "מתמטיקה", "teacher": "רחל", "homeworkData": "עמוד 45 תרגילים 1-5", "hourNum": "1",
             "status": "Open"},
            {"lesson": "אנגלית", "teacher": "דוד", "homeworkData": "אין", "hourNum": "2"},
            {"lesson": "מדעים", "teacher": "יעל", "homeworkData": "", "hourNum": "3"},
        ],
    },
}  # fmt: skip
HOMEWORK_B = {"status": True, "data": {"allowToViewThis": True, "dataTable": []}}


def _behaviour(
    event_id: int, kind: str, date: str, subject: str, hour: int, remark: str | None = None
) -> dict[str, Any]:
    return {
        "eventID": event_id,
        "eventType": kind,
        "eventCode": 244493,
        "eventDate": f"{date}T00:00:00",
        "isJustified": False,
        "justifiedReason": None,
        "subjectName": subject,
        "subjectCode": 4,
        "teacherName": "רחל כהן",
        "hourNum": hour,
        "hourName": "",
        "remark": remark,
        "studyGroupID": 721677,
        "lessonNums": 0,
    }


_BEHAVIOUR_ROWS = [
    _behaviour(1, "ציון לשבח", "2026-09-01", "תורה", 1),
    _behaviour(2, "איחור", "2026-11-09", "חשבון", 1, "איחר בעשר דקות"),
    _behaviour(3, "הפרעה", "2026-11-10", "מדעים", 5, "דיבר בשיעור"),
    _behaviour(4, "ציון לשבח", "2026-11-10", "אנגלית", 2),
]
_BEHAVIOUR_COMMON = {
    "viewRemarkForParentPupil": True,
    "canRequestJustification": False,
    "behaviorGrade": None,
    "hours": None,
    "eventsTypes": None,
    "groups": None,
}
# PupilCard/GetPupilDiciplineEvents: the whole year.
DISCIPLINE = {"status": True, "data": {**_BEHAVIOUR_COMMON, "diciplineEvents": _BEHAVIOUR_ROWS}}
# dashboard/GetPupilDiciplineEvents: the latest few, nested one level deeper.
DISCIPLINE_DASHBOARD = {
    "status": True,
    "data": {"allowToViewThis": True, "dataTable": {**_BEHAVIOUR_COMMON, "diciplineEvents": _BEHAVIOUR_ROWS[-2:]}},
}

INBOX = {
    "status": True,
    "data": [
        {"typeID": 1, "subject": "טיול שנתי", "sendingDate": "2026-11-09T13:13:00", "userTitle": None,
         "student_F_name": "דוד", "student_L_name": "כהן", "userType": 1, "classCode": "0", "classNumber": 0,
         "hasRead": 0, "filesWereAttached": 1, "rowNumber": 1, "count": 2, "fromTitle": None,
         "senderId": "enc-sender-1", "messageId": "enc-message-1", "userImageToken": "img-token"},
        {"typeID": 1, "subject": "הודעה ישנה", "sendingDate": "2026-11-01T08:30:00", "userTitle": None,
         "student_F_name": "שרה", "student_L_name": "מזרחי", "userType": 1, "classCode": "0", "classNumber": 0,
         "hasRead": 1, "filesWereAttached": 0, "rowNumber": 2, "count": 2, "fromTitle": None,
         "senderId": "enc-sender-2", "messageId": "enc-message-2", "userImageToken": "img-token"},
    ],
}  # fmt: skip

# Menu/GetPreviewUnreadNotifications
NOTIFICATIONS = {
    "status": True,
    "data": {
        "personalNotifications": [
            {"moduleName": "lessonSubjectHomework", "itemId": "enc-item-1", "moduleNavigation": "PupilCard/13/",
             "notificationType": 0, "id": 0, "date": "2026-11-10T14:05:00", "message": "עודכנו שיעורי בית במתמטיקה",
             "read_date": None, "extra_data": "a=1", "studentID": ENCRYPTED_ID_A},
            {"moduleName": "disciplineEvents", "itemId": "enc-item-2", "moduleNavigation": "PupilCard/14/",
             "notificationType": 0, "id": 0, "date": "2026-11-09T09:00:00", "message": "נרשם אירוע משמעת",
             "read_date": None, "extra_data": "a=2", "studentID": ENCRYPTED_ID_A},
        ],
        "systemMessages": None,
        "pedegogicalNotification": None,
    },
}  # fmt: skip

# Menu/GetMenuCounters
MENU_COUNTERS = {
    "status": True,
    "data": {
        "unreadMessages": 1,
        "unreadNotifications": 3,
        "unreadPedegogicalNotifications": 0,
        "unreadSystemNotifications": 0,
        "unreadPersonalNotifications": 3,
    },
}

# dashboard/WeeklySchedualeForToday: carries the bell schedule one level down. A school that never
# entered its lesson times answers ":" for each, as in WEEKLY_WITHOUT_TIMES.


def _weekly(times: list[tuple[str, str]]) -> dict[str, Any]:
    return {
        "status": True,
        "data": {
            "message": None,
            "data": {
                "hourSettings": {"schdualerHourNumber": 10, "schdualerStartZero": 1},
                "events": {
                    "hours": [
                        {"hour": hour, "name": None, "fromTime": start, "toTime": end}
                        for hour, (start, end) in enumerate(times, 1)
                    ],
                    "vactionns": [],
                    "minHour": 1,
                    "maxHour": 10,
                    "events": None,
                },
                "inactiveDays": [7],
            },
            "messageObjects": None,
        },
    }


WEEKLY = _weekly(_BELL)
WEEKLY_WITHOUT_TIMES = _weekly([(":", ":")] * 4)

# shotef/EventsTestsBoard. One captured school does not use the board (TESTS_BOARD_UNUSED); another
# does. Exam rows and hours follow that capture; event and vacation rows were empty there and
# still follow what the Webtop web client reads.
TESTS_BOARD_UNUSED = {
    "status": True,
    "data": {"classes": None, "instructorClasses": None, "studyGroups": None, "exams": None, "vactications": None,
             "events": None, "subjects": None, "examTypes": None, "eventsTypes": None, "hours": None,
             "inactiveDays": None, "startDate": "0001-01-01T00:00:00"},
}  # fmt: skip
def _exam(
    exam_id: int, date: str, hours: tuple[int, int], kind: str, title: str, *, subject: str = "", subject_id: int = 0,
    classes: tuple[tuple[int, int], ...] = ((3, 2),), groups: tuple[int, ...] = (),
) -> dict[str, Any]:  # fmt: skip
    """An exam row as the tests board returns it."""
    return {
        "fromHourName": f"{hours[0]}.",
        "tomHourName": f"{hours[1]}.",
        "groupsList": [{"id": group, "title": f"קבוצה {group}"} for group in groups],
        "classes": [{"classCode": c, "classNumber": n, "title": None, "itemId": None} for c, n in classes],
        "rooms": None,
        "id": exam_id,
        "studyGroupID": groups[0] if groups else 0,
        "subjectID": subject_id,
        "classCode": classes[0][0],
        "title": title,
        "subject": subject,
        "date": f"{date}T00:00:00",
        "typeID": 5,
        "fromHour": hours[0],
        "toHour": hours[1],  # inclusive
        "eventGroupID": exam_id,
        "type": kind,
        "roomName": None,
    }


TESTS_BOARD = {
    "status": True,
    "data": {
        "classes": [{"classCode": 3, "classNumber": 2, "title": None, "itemId": None}],
        "hours": [
            *(
                {"hour": hour, "name": f"{hour}. {end}-{start}", "fromTime": start, "toTime": end}
                for hour, (start, end) in enumerate(_BELL, 1)
            ),
            # The times are sometimes missing while the label still has them.
            {"hour": 5, "name": "5. 12:25-11:40", "fromTime": ":", "toTime": ":"},
        ],
        "subjects": [{"id": 11, "title": "היסטוריה"}, {"id": 12, "title": "תיאטרון"}],
        "examTypes": [{"id": 5, "title": "מבחן", "viewOrder": 0}, {"id": 6, "title": "בוחן", "viewOrder": 1}],
        "exams": [
            # No study group: for the listed class. The subject is only given by id.
            _exam(501, "2026-11-18", (2, 3), "מבחן", "פרקים 4-6", subject_id=11),
            _exam(502, "2026-11-12", (1, 1), "בוחן", "אוצר מילים", subject="אנגלית", groups=(GROUPS["אנגלית"],)),
            _exam(503, "2026-11-05", (1, 2), "מבחן", "שברים", subject="מתמטיקה", groups=(GROUPS["מתמטיקה"],)),
            _exam(504, "2026-11-25", (3, 4), "מבחן", "תאים", subject="מדעים", classes=((6, 1),), groups=(6001,)),
            # An elective: it lists the class, but only pupils of that study group sit it.
            _exam(505, "2026-11-19", (1, 1), "עבודה להגשה", "מונולוג", subject_id=12,
                  classes=((3, 1), (3, 2), (3, 3)), groups=(9999,)),
        ],
        "events": [
            {"title": "יום ספורט", "from_date": "2026-11-20T00:00:00", "to_date": "2026-11-20T00:00:00",
             "from_hour": 1, "to_hour": 4, "typeID": 4, "classes": []},
        ],
        "vactications": [
            {"title": "חופשת חנוכה", "from_date": "2026-12-15T00:00:00", "to_date": "2026-12-22T00:00:00"},
        ],
        "inactiveDays": [{"day_number": 6}, {"day_number": 7}],
        "startDate": "2026-11-01T00:00:00",
    },
}  # fmt: skip


def _grade(evaluation_id: int, subject: str, title: str, date: str, grade: int | None, **extra: Any) -> dict[str, Any]:
    return {
        "evaluationID": evaluation_id,
        "title": title,
        "date": f"{date}T00:00:00",
        "type": "בוחן",
        "typeCode": 6,
        "teacherFirstName": "נועה",
        "teacherLastName": "שפירא",
        "grade": grade,
        "gradeTranslation": None,
        "subject": subject,
        "level": None,
        "weight": 10 if grade is not None else None,
        "remark": None,
        "assessment": None,
        "components": [],
        "isDeleted": 0,
        **extra,
    }


_GRADE_ROWS = [
    _grade(701, "אנגלית", "בוחן אוצר מילים", "2026-10-20", 78, weight=5),
    _grade(702, "מדעים", "בוחן פתע", "2026-11-03", 92, remark="כל הכבוד"),
    _grade(703, "היסטוריה", "מבחן", "2026-11-08", None),  # set, not marked yet
]
# PupilCard/GetPupilGrades: every evaluation of the year, marked or not.
GRADES_CARD = {"status": True, "data": _GRADE_ROWS}
# dashboard/GetGrades: the latest marked ones.
GRADES = {"status": True, "data": {"allowToViewThis": True, "dataTable": _GRADE_ROWS[:2]}}

# dashboard/ChangesAndMessagesDataForToday
CHANGES = {
    "status": True,
    "data": {
        "allowToViewThis": True,
        "messages": [],
        "changes": [{"hour": 2, "content": "מילוי מקום", "lastName": "כהן", "privateName": "רחל", "room": "12"}],
        "exams": [],
        "events": [{"from_hour": 5, "title": "אסיפת שכבה"}],
    },
}


# messageBox/GetMessagesInboxData: the text of one message, as HTML. The shape follows what the
# Webtop web client reads; it has not been captured, because loading it marks the message as read.
MESSAGE_HTML = {
    "enc-message-1": "<p>שלום הורים,</p><p>הטיול השנתי יתקיים ביום ג&#39;.<br>נא לאשר השתתפות &amp; תשלום.</p>",
    "enc-message-2": "<div>תזכורת: <b>אסיפת הורים</b> ביום חמישי.</div><style>p{color:red}</style>",
}


def _message_body(payload: dict[str, Any]) -> dict[str, Any]:
    content = MESSAGE_HTML.get(payload.get("MessageId"))
    if content is None:
        return {"status": True, "data": None}
    return {
        "status": True,
        "data": {
            "messageData": {"messageContent": content, "subject": "x", "senderName": "y"},
            "childrens": [],
            "roles": None,
            "signature": None,
            "survey": None,
        },
    }


def _week_for(payload: dict[str, Any]) -> dict[str, Any]:
    """Student A's current week; an empty week for anyone else and for other weeks."""
    if payload.get("studentID") == ENCRYPTED_ID_A and payload.get("weekIndex", 0) == 0:
        return LESSONS_WEEK
    return LESSONS_WEEK_EMPTY


# The week before LESSONS_WEEK (weekIndex -1): homework given on Thursday 5 November,
# written on both lessons of a double period.
LAST_WEEK = {
    "status": True,
    "data": [
        {
            "date": "2026-11-05T00:00:00+02:00",
            "dayIndex": 5,
            "hoursData": [
                {"hour": 3, "hourName": None,
                 "scheduale": [_week_lesson(5, 3, "מדעים", "יעל", "להכין מצגת על מחזור המים", "מחזור המים")]},
                {"hour": 4, "hourName": None,
                 "scheduale": [_week_lesson(5, 4, "מדעים", "יעל", "להכין מצגת על מחזור המים", "מחזור המים")]},
            ],
        }
    ],
}  # fmt: skip


# Defaults for request(), keyed by the last path segment. A callable picks by request payload.
REQUEST_DEFAULTS: dict[str, Any] = {
    "GetPupilLessonsAndHomework": _week_for,
    "GetPupilDiciplineEvents": DISCIPLINE,
    "GetMenuCounters": MENU_COUNTERS,
    "GetMessagesInboxData": _message_body,
    "EventsTestsBoard": TESTS_BOARD,
    "WeeklySchedualeForToday": WEEKLY,
    "GetPupilGrades": GRADES_CARD,
    "GetGrades": GRADES,
    "ChangesAndMessagesDataForToday": CHANGES,
    # Used by the capture tool, which calls every endpoint through request().
    "InitDashboard": DASHBOARD,
    "GetHomeWork": HOMEWORK_A,
    "GetMessagesInbox": INBOX,
    "GetPupilScheduale": TIMETABLE,
    "GetPreviewUnreadNotifications": NOTIFICATIONS,
    "GetMultipleUsersForUser": {"status": True, "data": []},
    "GetPupilPeriodGrades": {"status": False, "errorDescription": "view is blocked", "data": None},
}


class FakeResponse:
    def __init__(self, body: Any) -> None:
        self._body = body

    def json(self) -> Any:
        return self._body


class Seq(list):
    """Responses consumed one per call; the last one repeats."""


def _session(data: dict[str, Any] | None = None) -> WebtopSession:
    data = data or LOGIN_DATA
    return WebtopSession(
        token=data["token"],
        user_id=data.get("userId"),
        student_id=data.get("studentId"),
        school_id=data.get("schoolId"),
        school_name=data.get("schoolName"),
        first_name=data.get("firstName"),
        last_name=data.get("lastName"),
        raw_login_data=data,
    )


@dataclass
class FakeWebtopClient:
    """Stands in for webtop.WebtopClient; every method is a canned response.

    A response may be an exception instance (raised) or a ``Seq`` consumed one
    per call (the last value repeats).
    """

    username: str = ""
    password: str = ""
    data: str = DEFAULT_DATA
    timeout: float = 20.0
    auto_login: bool = True
    responses: dict[str, Any] = field(default_factory=dict)
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    login_count: int = 0
    closed: bool = False
    _session: WebtopSession | None = None
    # Stands in for pywebtop's httpx client, which is where request headers live.
    _http: Any = field(default_factory=lambda: SimpleNamespace(headers={}))

    # --- plumbing -------------------------------------------------------
    def _answer(self, name: str, default: Any, **kwargs: Any) -> Any:
        self.calls.append((name, kwargs))
        value = self.responses.get(name, default)
        if isinstance(value, Seq):
            value = value.pop(0) if len(value) > 1 else value[0]
        if isinstance(value, Exception):
            raise value
        return value

    @property
    def is_logged_in(self) -> bool:
        return self._session is not None

    @property
    def session(self) -> WebtopSession:
        if self._session is None:
            raise WebtopLoginError("Not logged in")
        return self._session

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def close(self) -> None:
        self.closed = True

    # --- API -------------------------------------------------------------
    async def login(self) -> WebtopSession:
        self.login_count += 1
        result = self._answer("login", LOGIN_DATA)
        self._session = _session(result)
        return self._session

    async def switch_student(self, student_id: str, saved_user: str = "") -> WebtopSession:
        result = self._answer("switch_student", LOGIN_DATA, student_id=student_id)
        self._session = _session(result)
        return self._session

    async def get_linked_students(self) -> list[dict[str, Any]]:
        return self._answer("get_linked_students", [])

    async def get_students(self) -> Any:
        return self._answer("get_students", DASHBOARD)

    async def get_homework(self, *, encrypted_student_id: str, class_code: int, class_number: int) -> Any:
        default = HOMEWORK_A if encrypted_student_id == ENCRYPTED_ID_A else HOMEWORK_B
        return self._answer(
            "get_homework",
            default,
            encrypted_student_id=encrypted_student_id,
            class_code=class_code,
            class_number=class_number,
        )

    async def get_discipline_events(self, *, encrypted_student_id: str, class_code: int) -> Any:
        return self._answer("get_discipline_events", DISCIPLINE_DASHBOARD, encrypted_student_id=encrypted_student_id)

    async def get_pupil_schedule(self, **kwargs: Any) -> Any:
        return self._answer("get_pupil_schedule", TIMETABLE, **kwargs)

    async def get_messages_inbox(self, **kwargs: Any) -> Any:
        return self._answer("get_messages_inbox", INBOX, **kwargs)

    async def get_preview_unread_notifications(self) -> Any:
        return self._answer("get_preview_unread_notifications", NOTIFICATIONS)

    async def request(self, method: str, path: str, **kwargs: Any) -> FakeResponse:
        """Endpoints pywebtop has no method for, keyed by the last path segment."""
        name = path.rsplit("/", 1)[-1]
        payload = kwargs.get("json") or {}
        override = self.responses.get(name)
        if callable(override):  # a test deciding by request payload
            self.calls.append((name, {"path": path, **payload}))
            return FakeResponse(override(payload))
        default = REQUEST_DEFAULTS.get(name, {"status": True, "data": None})
        if callable(default):
            default = default(payload)
        return FakeResponse(self._answer(name, default, path=path, **payload))


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Make custom_components/ loadable in every test."""


@pytest.fixture(autouse=True)
def frozen_clock(freezer: FrozenDateTimeFactory) -> FrozenDateTimeFactory:
    """Pin "today" to the date the fixtures use, so tests pass on any day.

    20:00 UTC is midday on 10 November in the harness's default timezone
    (US/Pacific) and 22:00 the same day in Israel: the same date in both, and
    outside the integration's night pause in both.
    """
    freezer.move_to("2026-11-10 20:00:00+00:00")
    return freezer


@pytest.fixture(autouse=True)
def half_hour_polls() -> Generator[None]:
    """Poll every 30 minutes in tests unless a test sets an interval; the real default is an hour."""
    with patch("custom_components.webtop.coordinator.DEFAULT_SCAN_INTERVAL", 30):
        yield


@pytest.fixture(autouse=True)
def no_poll_jitter() -> Generator[None]:
    """Polls are shifted by a random fraction of the interval; pin it so ticks are exact."""
    with patch("custom_components.webtop.coordinator.random.uniform", return_value=1.0):
        yield


@pytest.fixture
def fake_client() -> Generator[FakeWebtopClient]:
    """Patch pywebtop's client in both places the integration imports it."""
    created: list[FakeWebtopClient] = []
    built_on_loop_thread: list[bool] = []
    loop_thread = threading.current_thread()

    def factory(**kwargs: Any) -> FakeWebtopClient:
        # The real client loads TLS certificates from disk when constructed,
        # so it must never be built on the event-loop thread.
        built_on_loop_thread.append(threading.current_thread() is loop_thread)
        # ...and loading them takes real time. Without this pause the tests only catch
        # "asserted before the background poll finished" on a slow CI runner.
        time.sleep(0.02)
        client = FakeWebtopClient(**kwargs)
        client.responses = factory.responses  # type: ignore[attr-defined]
        created.append(client)
        return client

    factory.responses = {}  # type: ignore[attr-defined]
    with (
        # A plain function, not a Mock: the test harness runs Mock executor
        # jobs inline, which would hide a client built on the event loop.
        patch("custom_components.webtop.auth.WebtopClient", new=factory),
        patch(
            "custom_components.webtop.auth.async_fetch_login_counter", AsyncMock(return_value=0)
        ) as counter,
    ):
        proxy = FakeWebtopClient()
        proxy.responses = factory.responses  # type: ignore[attr-defined]
        proxy.created = created  # type: ignore[attr-defined]
        proxy.built_on_loop_thread = built_on_loop_thread  # type: ignore[attr-defined]
        proxy.counter = counter  # type: ignore[attr-defined]
        yield proxy


@pytest.fixture
def config_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="דנה לוי",
        unique_id="u-42",
        data={CONF_USERNAME: "parent", CONF_PASSWORD: "secret", CONF_DATA: DEFAULT_DATA},
        options={},
    )


@pytest.fixture(name="unauthorized")
def unauthorized_error() -> WebtopRequestError:
    return WebtopRequestError("POST /server/api/dashboard/InitDashboard failed (401): ")
