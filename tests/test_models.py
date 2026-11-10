"""Offline tests for the response parsers (no Home Assistant needed)."""

from __future__ import annotations

import pytest

from custom_components.webtop.models import (
    Message,
    Student,
    WebtopApiError,
    is_no_homework,
    parse_discipline,
    parse_homework,
    parse_messages,
    parse_schedule,
    parse_students,
    parse_unread_count,
    student_from_login,
    unwrap,
)

from .conftest import DASHBOARD, DISCIPLINE, ENCRYPTED_ID_A, HOMEWORK_A, INBOX, LOGIN_DATA, SCHEDULE


# --- envelope ---------------------------------------------------------------
def test_unwrap_returns_data():
    assert unwrap({"status": True, "data": [1]}, "x") == [1]


def test_unwrap_raises_on_status_false_with_description():
    with pytest.raises(WebtopApiError) as info:
        unwrap({"status": False, "errorDescription": "view is blocked", "errorId": 12}, "GetHomeWork")
    assert "view is blocked" in str(info.value)
    assert info.value.looks_like_expired_session is False


def test_unwrap_status_false_without_text_looks_like_dead_session():
    with pytest.raises(WebtopApiError) as info:
        unwrap({"status": False, "errorDescription": None, "errorId": None}, "x")
    assert info.value.looks_like_expired_session is True


def test_unwrap_rejects_non_dict():
    with pytest.raises(WebtopApiError):
        unwrap("<html>", "x")


# --- students ---------------------------------------------------------------
def test_parse_students_from_childrens():
    students = parse_students(DASHBOARD["data"], school_name="S")
    assert [s.name for s in students] == ["נועם לוי", "מאיה לוי"]
    first = students[0]
    assert first.student_id == ENCRYPTED_ID_A
    assert first.key == "1001"  # numeric id wins over the encrypted blob
    assert (first.class_code, first.class_number, first.study_year) == (3, 2, 2026)
    assert first.class_label == "ג'2"
    assert students[1].class_label == "6-1"
    assert first.school_name == "S"


def test_parse_students_bare_list_and_unknown_wrapper():
    bare = parse_students([{"id": "x", "firstName": "A"}])
    assert bare[0].key and bare[0].name == "A"
    wrapped = parse_students({"foo": 1, "kids": [{"id": "y", "classCode": 1}]})
    assert wrapped[0].student_id == "y"
    assert parse_students({"foo": [None, 1]}) == []
    assert parse_students(None) == []


def test_student_key_without_numeric_id_is_stable_digest():
    a = Student.from_dashboard({"id": "enc1", "firstName": "A", "lastName": "B"}, school_name="S")
    b = Student.from_dashboard({"id": "enc2", "firstName": " A", "lastName": "b "}, school_name="S")
    assert a.key == b.key  # encrypted id rotated, same child
    assert a.has_class_params is False


def test_student_from_login_fallback():
    s = student_from_login(LOGIN_DATA, school_name="S")
    assert s is not None and s.student_id == ENCRYPTED_ID_A and s.key == "1001"
    assert student_from_login({}, school_name="S") is None


# --- homework ---------------------------------------------------------------
@pytest.mark.parametrize("text", ["", "אין", "אין שיעורי בית", "לא הוזן", "-", "None", "  . "])
def test_no_homework_values(text):
    assert is_no_homework(text)


def test_real_homework_is_kept():
    assert not is_no_homework("עמוד 45")


def test_parse_homework_filters_and_stamps_date():
    items = parse_homework(HOMEWORK_A["data"], date="2026-11-10")
    assert len(items) == 1
    item = items[0]
    assert item.subject == "מתמטיקה" and item.teacher == "רחל" and item.hour == 1
    assert item.date == "2026-11-10" and item.status == "Open"
    assert item.identity() == parse_homework(HOMEWORK_A["data"], date="2026-11-11")[0].identity()


def test_parse_homework_tolerates_garbage():
    assert parse_homework(None, date="d") == []
    assert parse_homework({"dataTable": [None, 3]}, date="d") == []
    assert parse_homework([{"homework": "p.1", "subject": "X"}], date="d")[0].subject == "X"


# --- messages ---------------------------------------------------------------
def test_message_sender_and_flags():
    msgs = parse_messages(INBOX["data"])
    assert msgs[0].sender == "כהן דוד"
    assert msgs[0].has_read is False and msgs[0].has_files is True
    assert msgs[1].has_read is True and msgs[1].has_files is False
    assert msgs[0].identity() != msgs[1].identity()


def test_message_identity_ignores_read_state():
    raw = dict(INBOX["data"][0])
    read = Message.from_api({**raw, "hasRead": 1})
    assert read.identity() == Message.from_api(raw).identity()


def test_message_fallback_sender():
    assert Message.from_api({"subject": "x", "fromTitle": "מנהלת"}).sender == "מנהלת"
    assert parse_messages({"messages": [{"subject": "a"}]})[0].subject == "a"
    assert parse_messages(42) == []


# --- discipline -------------------------------------------------------------
def test_parse_discipline():
    events = parse_discipline(DISCIPLINE["data"])
    assert len(events) == 1
    assert events[0].event == "איחור" and events[0].subject == "חשבון" and events[0].teacher == "רחל"
    assert events[0].justified is None


def test_parse_discipline_wrapped_and_empty():
    assert parse_discipline({"dataTable": [{"date": "2026-01-01", "type": "T", "justified": 1}]})[0].justified is True
    assert parse_discipline({"dataTable": [{"foo": 1}]}) == []
    assert parse_discipline(None) == []


# --- schedule ---------------------------------------------------------------
def test_parse_schedule_inherits_date_and_hour():
    lessons = parse_schedule(SCHEDULE["data"])
    assert [(lesson.date[:10], lesson.hour, lesson.subject) for lesson in lessons] == [
        ("2026-11-10", 1, "מתמטיקה"),
        ("2026-11-10", 2, "אנגלית"),
        ("2026-11-11", 1, "מדעים"),
    ]
    assert lessons[1].changed is True and lessons[0].changed is False


def test_parse_schedule_flat_rows():
    lessons = parse_schedule({"rows": [{"lessonDate": "2026-11-10", "hour": "3", "subject": "ספורט"}]})
    assert lessons[0].hour == 3 and lessons[0].subject == "ספורט"
    assert parse_schedule(None) == []


# --- notifications ----------------------------------------------------------
@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ([{"id": 1}, {"id": 2}], 2),
        ({"unreadCount": 5}, 5),
        ({"notifications": [1, 2, 3]}, 3),
        ({"foo": "bar"}, None),
        (7, 7),
        (None, None),
    ],
)
def test_parse_unread_count(payload, expected):
    assert parse_unread_count(payload) == expected
