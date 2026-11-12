"""End-to-end run of the capture tool against a fake portal and a fake local model."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from webtop import WebtopLoginError, WebtopRequestError

from tools import webtop_capture as tool
from tools.anonymizer import same_shape

from .conftest import DASHBOARD, ENCRYPTED_ID_A, ENCRYPTED_ID_B, HOMEWORK_A, INBOX, FakeWebtopClient
from .test_anonymizer import FakeLLM

PERSONAL = ("parent", "secret-pw", "tok-1", "u-42", ENCRYPTED_ID_A, ENCRYPTED_ID_B, "נועם", "מאיה", "לוי", "דנה",
            "בית ספר לדוגמה", "כהן", "רחל", "טיול שנתי", "עמוד 45", "440099", "שפירא", "כל הכבוד")  # fmt: skip
# Keyed by path suffix: lesson subjects are generic, a message's subject is not.
LLM_TABLE = {
    "dataTable[].lesson": "keep",
    "dataTable[].subject": "keep",
    "exams[].subject": "keep",
    "scheduale[].subject_name": "keep",
    "dataTable[].status": "keep",
    "data[].subject": "free_text",
}
USE_FAKE_MODEL = object()


async def _run(
    tmp_path: Path, responses: dict | None = None, llm=USE_FAKE_MODEL
) -> tuple[int, Path | None, list[FakeWebtopClient]]:
    created: list[FakeWebtopClient] = []

    def factory(**kwargs):
        client = FakeWebtopClient(**kwargs)
        client.responses = responses or {}
        created.append(client)
        return client

    counter = MagicMock()
    counter.json.return_value = 0
    args = argparse.Namespace(out=str(tmp_path / "captures"), max_students=2)
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
    assert requests["homework_today__student1"]["parsed_by_integration"] == {"homework": 1}
    assert requests["pupil_schedule__student1"]["parsed_by_integration"] == {"lessons": 3}
    assert requests["pupil_grades__student1"]["http_error"] == 500
    assert requests["pupil_period_grades__student1"]["errorDescription"] == "view is blocked"
    assert requests["homework_today__student1"]["payload_template"]["id"] == "{student_id}"
    assert manifest["anonymizer"]["plan"]["token"] == "secret"

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
