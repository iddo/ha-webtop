"""Shared fixtures: a fake pywebtop client wired into the integration."""

from __future__ import annotations

import threading
from collections.abc import Generator
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from pytest_homeassistant_custom_component.common import MockConfigEntry
from webtop import WebtopLoginError, WebtopRequestError, WebtopSession

from custom_components.webtop.const import CONF_DATA, CONF_PASSWORD, CONF_USERNAME, DEFAULT_DATA, DOMAIN

ENCRYPTED_ID_A = "enc-student-a=="
ENCRYPTED_ID_B = "enc-student-b=="

LOGIN_DATA = {
    "token": "tok-1",
    "userId": "u-42",
    "studentId": 1001,
    "id": ENCRYPTED_ID_A,
    "schoolId": 7,
    "schoolName": "בית ספר לדוגמה",
    "firstName": "דנה",
    "lastName": "לוי",
}

DASHBOARD = {
    "status": True,
    "data": {
        "childrens": [
            {
                "id": ENCRYPTED_ID_A,
                "studentId": 1001,
                "firstName": "נועם",
                "lastName": "לוי",
                "classCode": 3,
                "classNum": 2,
                "className": "ג'2",
                "studyYear": 2026,
            },
            {
                "id": ENCRYPTED_ID_B,
                "studentId": 1002,
                "firstName": "מאיה",
                "lastName": "לוי",
                "classCode": 6,
                "classNum": 1,
                "studyYear": 2026,
            },
        ]
    },
}

HOMEWORK_A = {
    "status": True,
    "data": {
        "dataTable": [
            {
                "lesson": "מתמטיקה",
                "teacher": "רחל",
                "homeworkData": "עמוד 45 תרגילים 1-5",
                "hourNum": "1",
                "status": "Open",
            },
            {"lesson": "אנגלית", "teacher": "דוד", "homeworkData": "אין", "hourNum": "2"},
            {"lesson": "מדעים", "teacher": "יעל", "homeworkData": "", "hourNum": "3"},
        ]
    },
}
HOMEWORK_B = {"status": True, "data": {"dataTable": []}}

DISCIPLINE = {
    "status": True,
    "data": [
        {
            "eventDate": "2026-11-09T09:00:00",
            "eventName": "איחור",
            "subjectName": "חשבון",
            "teacherName": "רחל",
            "remark": "",
        },
    ],
}

SCHEDULE = {
    "status": True,
    "data": [
        {
            "date": "2026-11-10T00:00:00",
            "hoursData": [
                {"hourNum": 1, "scheduale": [{"subject_name": "מתמטיקה", "teacher": "רחל"}]},
                {"hourNum": 2, "scheduale": [{"subject_name": "אנגלית", "teacher": "דוד", "isChanged": 1}]},
            ],
        },
        {
            "date": "2026-11-11T00:00:00",
            "hoursData": [{"hourNum": 1, "scheduale": [{"subject_name": "מדעים", "teacher": "יעל"}]}],
        },
    ],
}

INBOX = {
    "status": True,
    "data": [
        {
            "subject": "טיול שנתי",
            "sendingDate": "2026-11-09T13:13:00",
            "student_F_name": "דוד",
            "student_L_name": "כהן",
            "hasRead": 0,
            "filesWereAttached": 1,
            "rowNumber": 1,
        },
        {
            "subject": "הודעה ישנה",
            "sendingDate": "2026-11-01T08:30:00",
            "student_F_name": "שרה",
            "student_L_name": "מזרחי",
            "hasRead": 1,
            "filesWereAttached": 0,
            "rowNumber": 2,
        },
    ],
}

NOTIFICATIONS = {"status": True, "data": [{"id": 1}, {"id": 2}, {"id": 3}]}


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
        return self._answer("get_discipline_events", DISCIPLINE, encrypted_student_id=encrypted_student_id)

    async def get_pupil_schedule(self, **kwargs: Any) -> Any:
        return self._answer("get_pupil_schedule", SCHEDULE, **kwargs)

    async def get_messages_inbox(self, **kwargs: Any) -> Any:
        return self._answer("get_messages_inbox", INBOX, **kwargs)

    async def get_preview_unread_notifications(self) -> Any:
        return self._answer("get_preview_unread_notifications", NOTIFICATIONS)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Make custom_components/ loadable in every test."""


@pytest.fixture(autouse=True)
def frozen_clock(freezer: FrozenDateTimeFactory) -> FrozenDateTimeFactory:
    """Pin "today" to the date the fixtures use, so tests pass on any day.

    Midday UTC is 10 November in every timezone the test harness might use
    (it defaults to US/Pacific, not Israel).
    """
    freezer.move_to("2026-11-10 12:00:00+00:00")
    return freezer


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
