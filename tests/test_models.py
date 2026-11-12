"""Offline tests for the response parsers (no Home Assistant needed)."""

from __future__ import annotations

import datetime as dt

import pytest

from custom_components.webtop.models import (
    Message,
    Student,
    WebtopApiError,
    board_available,
    dashboard_allows,
    homework_from_lessons,
    is_no_homework,
    parse_bell_schedule,
    parse_discipline,
    parse_homework,
    parse_manual_bell,
    parse_menu_counters,
    parse_messages,
    parse_notifications,
    parse_schedule,
    parse_students,
    parse_unread_count,
    student_from_login,
    unwrap,
    view_allowed,
)

from .conftest import (
    DASHBOARD,
    DISCIPLINE,
    DISCIPLINE_DASHBOARD,
    ENCRYPTED_ID_A,
    HOMEWORK_A,
    INBOX,
    KEY_A,
    KEY_B,
    LESSONS_WEEK,
    LOGIN_DATA,
    MENU_COUNTERS,
    NOTIFICATIONS,
    SCHOOL,
    TESTS_BOARD,
    TESTS_BOARD_UNUSED,
    TIMETABLE,
    WEEKLY,
    WEEKLY_WITHOUT_TIMES,
)


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


def test_permission_flags():
    assert view_allowed({"allowToViewThis": True, "dataTable": []}) and view_allowed([]) and view_allowed(None)
    assert not view_allowed({"allowToViewThis": False})
    assert dashboard_allows(DASHBOARD["data"], "allowGetGrades")
    assert dashboard_allows(DASHBOARD["data"], "aFlagTheServerNeverSent") and dashboard_allows(None, "x")
    assert not dashboard_allows({"allowGetGrades": False}, "allowGetGrades")


# --- students ---------------------------------------------------------------
def test_parse_students_from_childrens():
    students = parse_students(DASHBOARD["data"], school_name=SCHOOL)
    assert [s.name for s in students] == ["נועם לוי", "מאיה לוי"]
    first = students[0]
    assert first.student_id == ENCRYPTED_ID_A
    # No numeric id on the wire: the key is derived from name and school, never the encrypted id.
    assert (first.key, students[1].key) == (KEY_A, KEY_B)
    assert KEY_A != KEY_B and ENCRYPTED_ID_A not in KEY_A
    assert (first.class_code, first.class_number, first.study_year) == (3, 2, None)
    assert first.class_label == "ג'2" and students[1].class_label == "ו'1"
    assert first.school_name == SCHOOL and first.has_class_params


@pytest.mark.parametrize(
    ("code", "number", "label"),
    [(1, 3, "א'3"), (10, 2, "י'2"), (11, 4, 'י"א 4'), (12, 1, 'י"ב 1'), (14, 1, "14-1"), (None, 1, ""), (3, None, "")],
)
def test_class_label(code, number, label):
    assert Student(student_id="x", key="k", class_code=code, class_number=number).class_label == label
    named = Student(student_id="x", key="k", class_code=3, class_number=1, class_name="ג מצוינות")
    assert named.class_label == "ג מצוינות"


def test_parse_students_bare_list_and_unknown_wrapper():
    bare = parse_students([{"id": "x", "firstName": "A"}])
    assert bare[0].key and bare[0].name == "A"
    wrapped = parse_students({"foo": 1, "kids": [{"id": "y", "classCode": 1}]})
    assert wrapped[0].student_id == "y"
    assert parse_students({"foo": [None, 1]}) == []
    assert parse_students(None) == []


def test_student_key_survives_a_new_encrypted_id_and_prefers_a_numeric_id():
    a = Student.from_dashboard({"id": "enc1", "firstName": "A", "lastName": "B"}, school_name="S")
    b = Student.from_dashboard({"id": "enc2", "firstName": " A", "lastName": "b "}, school_name="S")
    assert a.key == b.key  # the encrypted id changes every school year; the child does not
    assert a.has_class_params is False
    assert Student.from_dashboard({"id": "enc", "studentId": 1001, "firstName": "A"}).key == "1001"


def test_student_from_login_fallback():
    s = student_from_login({**LOGIN_DATA, "classCode": 4, "classNum": 1}, school_name="S")
    assert s is not None and s.student_id == LOGIN_DATA["id"] and s.name == "דנה לוי"
    assert student_from_login({}, school_name="S") is None


# --- timetable --------------------------------------------------------------
def test_parse_dated_week_of_lessons():
    lessons = parse_schedule(LESSONS_WEEK["data"])
    assert [(x.date[:10], x.weekday, x.hour, x.subject, x.teacher) for x in lessons] == [
        ("2026-11-09", 2, 1, "תורה", "שרה"),
        ("2026-11-10", 3, 1, "מתמטיקה", "רחל"),
        ("2026-11-10", 3, 2, "אנגלית", "דוד"),
        ("2026-11-11", 4, 1, "מדעים", "יעל"),
    ]
    assert lessons[1].topic == "שברים" and lessons[1].homework == "עמוד 45 תרגילים 1-5"
    assert lessons[3].topic == "" and lessons[3].homework == ""
    assert parse_schedule(None) == [] and parse_schedule({"data": "x"}) == []


def test_parse_standing_timetable_by_weekday():
    lessons = parse_schedule(TIMETABLE["data"])
    assert [(x.date, x.weekday, x.hour, x.subject) for x in lessons] == [
        ("", 3, 1, "מתמטיקה"),
        ("", 3, 2, "אנגלית"),
        ("", 4, 1, "מתמטיקה"),
    ]
    assert lessons[0].teacher == "כהן רחל" and lessons[0].room == ""


def test_parse_schedule_reads_a_lessons_own_times():
    row = {"day": 3, "hourNum": 2, "subject_name": "ספורט", "fromTime": "08:45", "toTime": "09:30"}
    lesson = parse_schedule([row])[0]
    assert (lesson.weekday, lesson.hour, lesson.start, lesson.end) == (3, 2, "08:45", "09:30")


# --- homework ---------------------------------------------------------------
@pytest.mark.parametrize("text", ["", "אין", "אין שיעורי בית", "לא הוזן", "-", "None", "  . "])
def test_no_homework_values(text):
    assert is_no_homework(text)


def test_real_homework_is_kept():
    assert not is_no_homework("עמוד 45")


def test_homework_from_the_week_is_dated_and_filtered():
    items = homework_from_lessons(parse_schedule(LESSONS_WEEK["data"]))
    assert [(h.date[:10], h.subject, h.homework) for h in items] == [
        ("2026-11-09", "תורה", "לקרוא פרק ג"),
        ("2026-11-10", "מתמטיקה", "עמוד 45 תרגילים 1-5"),
    ]  # "אין" and empty homework are not assignments
    assert items[1].as_dict() == {
        "date": "2026-11-10", "hour": 1, "subject": "מתמטיקה", "teacher": "רחל",
        "homework": "עמוד 45 תרגילים 1-5", "status": "",
    }  # fmt: skip
    assert items[0].identity() != items[1].identity()
    # An undated timetable cannot say when homework was given.
    assert homework_from_lessons(parse_schedule(TIMETABLE["data"])) == []


def test_dashboard_homework_fallback_filters_and_stamps_date():
    items = parse_homework(HOMEWORK_A["data"], date="2026-11-10")
    assert len(items) == 1
    item = items[0]
    assert item.subject == "מתמטיקה" and item.teacher == "רחל" and item.hour == 1
    assert item.date == "2026-11-10" and item.status == "Open"


def test_parse_homework_tolerates_garbage():
    assert parse_homework(None, date="d") == []
    assert parse_homework({"dataTable": [None, 3]}, date="d") == []
    assert parse_homework([{"homework": "p.1", "subject": "X"}], date="d")[0].subject == "X"


# --- messages, counters, notifications --------------------------------------
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


def test_menu_counters():
    counters = parse_menu_counters(MENU_COUNTERS["data"])
    assert (counters.unread_messages, counters.unread_notifications) == (1, 3)
    empty = parse_menu_counters(None)
    assert (empty.unread_messages, empty.unread_notifications) == (None, None)


def test_notifications_newest_first():
    found = parse_notifications(NOTIFICATIONS["data"])
    assert [(n.date[:10], n.module, n.kind) for n in found] == [
        ("2026-11-10", "lessonSubjectHomework", "personal"),
        ("2026-11-09", "disciplineEvents", "personal"),
    ]
    assert found[0].message == "עודכנו שיעורי בית במתמטיקה"
    assert parse_notifications(None) == [] and parse_notifications({"systemMessages": [None]}) == []


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (NOTIFICATIONS["data"], 2),
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


# --- behaviour --------------------------------------------------------------
def test_parse_discipline_from_the_pupil_card():
    events = parse_discipline(DISCIPLINE["data"])
    assert [(e.date[:10], e.hour, e.event) for e in events] == [
        ("2026-11-10", 5, "הפרעה"),
        ("2026-11-10", 2, "ציון לשבח"),
        ("2026-11-09", 1, "איחור"),
        ("2026-09-01", 1, "ציון לשבח"),
    ]  # newest first
    first = events[0]
    assert (first.subject, first.teacher, first.remark, first.justified) == ("מדעים", "רחל כהן", "דיבר בשיעור", False)
    assert len({e.identity() for e in events}) == 4
    assert first.as_dict()["date"] == "2026-11-10"


def test_parse_discipline_from_the_dashboard_nesting():
    events = parse_discipline(DISCIPLINE_DASHBOARD["data"])
    assert [e.event for e in events] == ["הפרעה", "ציון לשבח"]


def test_parse_discipline_tolerates_other_shapes():
    rows = [{"date": "2026-01-01", "type": "T", "justified": 1}]
    assert parse_discipline({"dataTable": rows})[0].justified is True
    assert parse_discipline({"dataTable": [{"foo": 1}]}) == []
    assert parse_discipline(None) == []


# --- lesson times -----------------------------------------------------------
def test_bell_schedule_sources():
    expected_third = (dt.time(9, 50), dt.time(10, 35))
    board = parse_bell_schedule(TESTS_BOARD["data"])
    weekly = parse_bell_schedule(WEEKLY["data"])  # nested at data.data.events.hours
    assert sorted(board) == sorted(weekly) == [1, 2, 3, 4]
    assert board[3] == weekly[3] == expected_third
    # A school that never entered its times answers ":" for every lesson.
    assert parse_bell_schedule(WEEKLY_WITHOUT_TIMES["data"]) == {}
    assert parse_bell_schedule(TESTS_BOARD_UNUSED["data"]) == {}
    assert parse_bell_schedule({"hours": [{"hour": 1, "fromTime": "09:00", "toTime": "08:00"}]}) == {}
    assert parse_bell_schedule(None) == {} and parse_bell_schedule(LESSONS_WEEK["data"]) == {}


def test_board_available():
    assert board_available(TESTS_BOARD["data"])
    assert not board_available(TESTS_BOARD_UNUSED["data"]) and not board_available(None)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", {}),
        (None, {}),
        ("08:00-08:45, 08:45-09:30", {1: ("08:00", "08:45"), 2: ("08:45", "09:30")}),
        ("8:00 - 8:45\n8:45 – 9:30", {1: ("08:00", "08:45"), 2: ("08:45", "09:30")}),
        ("0=07:15-08:00, 08:00-08:45", {0: ("07:15", "08:00"), 1: ("08:00", "08:45")}),
        ("3: 09.50-10.35; 10:35-11:20", {3: ("09:50", "10:35"), 4: ("10:35", "11:20")}),
    ],
)
def test_parse_manual_bell(text, expected):
    parsed = parse_manual_bell(text)
    assert {k: (a.strftime("%H:%M"), b.strftime("%H:%M")) for k, (a, b) in parsed.items()} == expected


@pytest.mark.parametrize("text", ["morning", "08:00", "09:00-08:00", "08:00-08:45, lunch", "25:00-26:00"])
def test_parse_manual_bell_rejects_nonsense(text):
    with pytest.raises(ValueError, match="lesson"):
        parse_manual_bell(text)
