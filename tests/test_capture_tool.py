"""End-to-end run of the capture tool against a fake portal and a fake local model."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from webtop import WebtopLoginError, WebtopRequestError

from tools import webtop_capture as tool
from tools.anonymizer import same_shape

from .conftest import (
    DASHBOARD,
    ENCRYPTED_ID_A,
    ENCRYPTED_ID_B,
    HOMEWORK_A,
    INBOX,
    LOGIN_DATA,
    SCHOOL,
    FakeWebtopClient,
)
from .test_anonymizer import FakeLLM

PERSONAL = ("parent", "secret-pw", "tok-1", "u-42", ENCRYPTED_ID_A, ENCRYPTED_ID_B, "נועם", "מאיה", "לוי", "דנה",
            "בית ספר לדוגמה", "כהן", "רחל", "טיול שנתי", "עמוד 45", "440099", "שפירא", "כל הכבוד",
            "enc-parent-login", "0520000000", "0521111111", "שברים", "דיבר בשיעור", "עודכנו שיעורי בית",
            "enc-item-1", "enc-message-1", "img-token", "School B", "enc-login-b", "BBB222", "550011")  # fmt: skip
# Keyed by path suffix, the way a sensible local model answers: subjects and labels are generic.
LLM_TABLE = {
    "dataTable[].lesson": "keep",
    "dataTable[].subject": "keep",
    "exams[].subject": "keep",
    "scheduale[].subject_name": "keep",
    "scheduale[].subject": "keep",
    "dataTable[].status": "keep",
    "diciplineEvents[].eventType": "keep",
    "diciplineEvents[].subjectName": "keep",
    "data[].subject": "keep",  # wrong for the inbox; the path rule must override it
}
USE_FAKE_MODEL = object()


async def _run(
    tmp_path: Path, responses: dict | None = None, llm=USE_FAKE_MODEL, current_only: bool = False
) -> tuple[int, Path | None, list[FakeWebtopClient]]:
    created: list[FakeWebtopClient] = []

    def factory(**kwargs):
        client = FakeWebtopClient(**kwargs)
        client.responses = responses or {}
        created.append(client)
        return client

    counter = MagicMock()
    counter.json.return_value = 0
    args = argparse.Namespace(out=str(tmp_path / "captures"), max_students=2, current_only=current_only)
    with (
        patch.object(tool, "WebtopClient", factory),
        patch.object(tool.httpx, "post", return_value=counter),
        patch.object(tool.asyncio, "sleep", AsyncMock()),
    ):
        model = FakeLLM(LLM_TABLE) if llm is USE_FAKE_MODEL else llm
        code = await tool.capture(args, "parent", "secret-pw", model)
    runs = sorted((tmp_path / "captures").glob("*")) if (tmp_path / "captures").exists() else []
    return code, (runs[0] if runs else None), created


async def test_capture_writes_only_anonymized_files(tmp_path: Path, capsys) -> None:
    code, out_dir, created = await _run(tmp_path, {"GetPupilGrades": WebtopRequestError("POST /x failed (500): body")})
    assert code == 0 and out_dir is not None
    client = created[0]
    assert client.auto_login is False and client.closed is True and client.login_count == 1

    files = {p.name: p.read_text(encoding="utf-8") for p in out_dir.iterdir()}
    everything = "\n".join(files.values()) + capsys.readouterr().out
    for value in PERSONAL:
        assert value not in everything, value

    manifest = json.loads(files["manifest.json"])
    requests = {r["name"]: r for r in manifest["requests"]}
    # Global endpoints once, per-student endpoints once per child.
    assert "messages_inbox" in requests and "homework_today__student1" in requests
    assert "homework_today__student2" in requests and "homework_today__student3" not in requests
    assert requests["dashboard_init"]["parsed_by_integration"] == {"students": 2}
    assert requests["homework_today__student1"]["parsed_by_integration"] == {"allowed": True, "homework": 1}
    assert requests["pupil_schedule__student1"]["parsed_by_integration"] == {
        "lessons": 3, "dated": 0, "with_weekday": 3, "with_teacher": 3, "with_room": 2, "with_topic": 0,
        "homework_items": 0, "homework_most_repeated": 0, "bell_hours": 0,
    }  # fmt: skip
    # Answered on the raw data, because anonymization scrambles the ids that link these responses.
    check = requests["cross_check__student1"]
    assert (check["exams_on_board"], check["exams_with_groups"]) == (5, 4)
    assert check["study_groups_on_lessons"] == 4
    assert check["exams_for_a_study_group_of_the_student"] == 2
    assert check["exams_listing_the_students_class"] == 4  # includes an elective the child does not take
    assert check["exams_the_integration_would_show"] == 3
    assert check["week_lessons_given_a_room_from_the_timetable"] == 2
    week = requests["pupil_lessons_homework__student1"]["parsed_by_integration"]
    assert (week["lessons"], week["dated"], week["with_topic"], week["homework_items"]) == (4, 4, 3, 2)
    assert requests["pupil_discipline__student1"]["parsed_by_integration"] == {"allowed": True, "events": 4}
    assert requests["events_tests_board"]["parsed_by_integration"] == {
        "available": True, "exams": 5, "events": 1, "vacations": 1, "bell_hours": 5,
    }  # fmt: skip
    assert requests["weekly_schedule_today__student1"]["parsed_by_integration"] == {"bell_hours": 4}
    assert requests["menu_counters"]["parsed_by_integration"] == {
        "has_unread_messages": True, "has_unread_notifications": True,
    }  # fmt: skip
    assert (manifest["linked_logins"], manifest["schools_captured"], manifest["students_captured"]) == (0, 1, 2)
    assert requests["pupil_grades__student1"]["http_error"] == 500
    assert requests["pupil_period_grades__student1"]["errorDescription"] == "view is blocked"
    assert requests["homework_today__student1"]["payload_template"]["id"] == "{student_id}"
    # Each pupil-card view is asked for under its own module id, plus two comparison requests.
    modules = {
        name.removesuffix("__student1"): request["payload_template"]["moduleID"]
        for name, request in requests.items()
        if name.startswith("pupil_") and name.endswith("__student1")
    }
    assert modules == {
        "pupil_schedule": 10, "pupil_lessons_homework": 11, "pupil_lessons_homework_last_week": 11,
        "pupil_grades": 6, "pupil_period_grades": 7, "pupil_discipline": 4, "pupil_previous_homework": 11,
        "pupil_grades_with_timetable_module": 10, "pupil_discipline_with_timetable_module": 10,
    }  # fmt: skip
    assert requests["pupil_lessons_homework_last_week__student1"]["payload_template"]["weekIndex"] == -1
    plan = manifest["anonymizer"]["plan"]
    assert plan["login.token"] == "secret"
    # A message's subject is written by a person: redacted by rule, whatever the model would say.
    assert plan["messages_inbox.data[].subject"] == "free_text"
    assert plan["unread_notifications.data.personalNotifications[].message"] == "free_text"

    by_name = {name.split("_", 1)[1].removesuffix(".json"): json.loads(text) for name, text in files.items()
               if name != "manifest.json"}  # fmt: skip
    assert same_shape(by_name["dashboard_init"], DASHBOARD)
    assert same_shape(by_name["messages_inbox"], INBOX)
    homework = by_name["homework_today__student1"]
    assert same_shape(homework, HOMEWORK_A)
    rows = homework["data"]["dataTable"]
    assert rows[0]["lesson"] == "מתמטיקה" and rows[1]["homeworkData"] == "אין"  # generic values survive
    assert by_name["login"]["token"] == "<secret>"
    # Every request the tool made was sent through the authenticated read-only helper.
    assert {c[0] for c in client.calls} >= {"InitDashboard", "EventsTestsBoard", "GetGrades"}
    assert not any(c[0] in {"switch_student", "setInput"} for c in client.calls)


async def test_capture_walks_every_linked_school(tmp_path: Path, capsys) -> None:
    linked = [
        {"institutionCode": 440099, "school_name": SCHOOL, "studentLogin": "AAA111", "studentId": LOGIN_DATA["id"]},
        {"institutionCode": 550011, "school_name": "School B", "studentLogin": "BBB222", "studentId": "enc-login-b=="},
    ]
    session_b = {**LOGIN_DATA, "id": "enc-login-b==", "schoolName": "School B", "institutionCode": 550011}
    responses = {"GetMultipleUsersForUser": {"status": True, "data": linked}, "switch_student": session_b}
    code, out_dir, created = await _run(tmp_path, responses)
    assert code == 0
    client = created[0]
    # The session starts on the first login, so only the second school is switched to.
    assert [c[1]["student_id"] for c in client.calls if c[0] == "switch_student"] == ["enc-login-b=="]
    board_codes = [c[1]["institutionCode"] for c in client.calls if c[0] == "EventsTestsBoard"]
    assert board_codes == [440099, 550011]

    files = {p.name: p.read_text(encoding="utf-8") for p in out_dir.iterdir()}
    everything = "\n".join(files.values()) + capsys.readouterr().out
    for value in PERSONAL:
        assert value not in everything, value
    manifest = json.loads(files["manifest.json"])
    names = [r["name"] for r in manifest["requests"]]
    assert "school1__dashboard_init" in names and "school2__dashboard_init" in names
    assert "school2__pupil_lessons_homework__student1" in names
    switches = {r["name"]: r["result"] for r in manifest["requests"] if r["path"] == "user/ChangeUser"}
    assert switches == {"school1__switch": "already on this login; no switch needed", "school2__switch": "ok"}
    assert (manifest["linked_logins"], manifest["schools_captured"], manifest["students_captured"]) == (2, 2, 4)
    assert any(name.endswith("school2__session.json") for name in files)


async def test_capture_current_only_never_switches(tmp_path: Path) -> None:
    linked = [{"studentId": LOGIN_DATA["id"]}, {"studentId": "enc-login-b=="}]
    code, out_dir, created = await _run(
        tmp_path, {"GetMultipleUsersForUser": {"status": True, "data": linked}}, current_only=True
    )
    assert code == 0
    assert not [c for c in created[0].calls if c[0] == "switch_student"]
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert (manifest["linked_logins"], manifest["schools_captured"]) == (2, 1)


async def test_capture_skips_a_school_it_cannot_switch_to(tmp_path: Path) -> None:
    linked = [{"studentId": LOGIN_DATA["id"]}, {"studentId": "enc-login-b=="}]
    responses = {
        "GetMultipleUsersForUser": {"status": True, "data": linked},
        "switch_student": WebtopLoginError("Switch student failed: {'status': False}"),
    }
    code, out_dir, _ = await _run(tmp_path, responses)
    assert code == 0
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    results = {r["name"]: r.get("result") for r in manifest["requests"] if r["path"] == "user/ChangeUser"}
    assert results["school2__switch"] == "failed (WebtopLoginError)"
    assert not any(r["name"].startswith("school2__dashboard") for r in manifest["requests"])
    assert manifest["students_captured"] == 2


async def test_capture_identifies_itself_and_reports_a_firewall_block(tmp_path: Path, capsys) -> None:
    block = WebtopLoginError("Login failed (403): <html>Request blocked. Generated by cloudfront (CloudFront)</html>")
    code, out_dir, created = await _run(tmp_path, {"login": block})
    assert code == 3 and out_dir is None
    assert created[0]._http.headers["User-Agent"].startswith("ha-webtop/")
    printed = capsys.readouterr().out
    assert "firewall refused the connection" in printed and "not a password problem" in printed
    assert "secret-pw" not in printed


async def test_capture_reports_login_failure_and_writes_nothing(tmp_path: Path, capsys) -> None:
    error = WebtopLoginError("Login returned status=false. errorDescription='CaptchaRequired', errorId=None")
    code, out_dir, _ = await _run(tmp_path, {"login": error})
    assert code == 2 and out_dir is None
    printed = capsys.readouterr().out
    assert "CaptchaRequired" in printed and "secret-pw" not in printed


async def test_capture_in_strict_mode_redacts_all_free_text(tmp_path: Path) -> None:
    code, out_dir, _ = await _run(tmp_path, llm=None)
    assert code == 0
    text = "\n".join(p.read_text(encoding="utf-8") for p in out_dir.iterdir())
    for value in (*PERSONAL, "מתמטיקה"):
        assert value not in text, value
