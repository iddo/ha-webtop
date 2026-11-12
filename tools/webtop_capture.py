#!/usr/bin/env python3
"""Capture real Webtop responses and write only anonymized copies to disk.

Run it yourself, in a terminal:

    .venv/bin/python -m tools.webtop_capture

It asks for your Webtop username and password (the password is never echoed,
stored, logged or written anywhere), logs in with pywebtop, calls a fixed list
of read-only endpoints, and anonymizes every response with a model running on
this machine before anything is saved. Raw responses exist only in memory.

Output goes to ``captures/<timestamp>/``: one JSON file per endpoint plus a
``manifest.json`` describing the requests, the anonymization plan and how many
items the integration's own parsers found. Read the files before sharing them.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from webtop import WebtopClient, WebtopError, WebtopLoginError

from custom_components.webtop import models
from custom_components.webtop.auth import BASE_URL, compute_login_data
from tools.anonymizer import LocalLLM, anonymize_documents, discover_local_llm

API = "/server/api/"
_SAFE_ERROR_RE = re.compile(r"^[\w \-'.]{1,60}$", re.ASCII)


@dataclass
class Endpoint:
    """A read-only request. ``payload`` values may reference context keys as ``{name}``."""

    name: str
    path: str
    payload: dict[str, Any]
    per_student: bool = False


SELECTED_CLASS = "{class_code}|{class_number}"
SELECTED_STUDENT = "{class_code}|{class_number}|{student_id}"
PUPIL_CARD = {
    "weekIndex": 0,
    "viewType": 0,
    "studyYear": "{study_year}",
    "studentID": "{student_id}",
    "classCode": "{class_code}",
    "moduleID": 10,
}

ENDPOINTS: tuple[Endpoint, ...] = (
    Endpoint("linked_students", "user/GetMultipleUsersForUser", {}),
    Endpoint("dashboard_init", "dashboard/InitDashboard", {}),
    Endpoint("menu_counters", "Menu/GetMenuCounters", {}),
    Endpoint("unread_notifications", "Menu/GetPreviewUnreadNotifications", {}),
    Endpoint("unread_messages_preview", "Menu/GetPreviewUnreadMessages", {}),
    Endpoint(
        "messages_inbox",
        "messageBox/GetMessagesInbox",
        {"PageId": 1, "LabelId": 0, "HasRead": None, "SearchQuery": ""},
    ),
    Endpoint(
        "events_tests_board",
        "shotef/EventsTestsBoard",
        {
            "institutionCode": "{institution_code}",
            "selectedDate": "{now_iso}",
            "selectedView": 3,
            "loadFilters": True,
            "studyYear": "{study_year}",
        },
    ),
    Endpoint(
        "homework_today",
        "dashboard/GetHomeWork",
        {"id": "{student_id}", "ClassCode": "{class_code}", "ClassNumber": "{class_number}"},
        per_student=True,
    ),
    Endpoint(
        "grades_latest",
        "dashboard/GetGrades",
        {"id": "{student_id}", "ClassCode": "{class_code}"},
        per_student=True,
    ),
    Endpoint(
        "discipline_dashboard",
        "dashboard/GetPupilDiciplineEvents",
        {"id": "{student_id}", "ClassCode": "{class_code}"},
        per_student=True,
    ),
    Endpoint(
        "schedule_today",
        "dashboard/ShotefSchedualeDataForToday",
        {"id": "{student_id}", "ClassCode": "{class_code}", "ClassNumber": "{class_number}"},
        per_student=True,
    ),
    Endpoint(
        "changes_today",
        "dashboard/ChangesAndMessagesDataForToday",
        {
            "institutionCode": "{institution_code}",
            "selectedValue": SELECTED_STUDENT,
            "typeView": 1,
            "todayOnly": True,
            "studentId": "{student_id}",
            "ClassCode": "{class_code}",
        },
        per_student=True,
    ),
    Endpoint(
        "weekly_schedule_today",
        "dashboard/WeeklySchedualeForToday",
        {
            "InstitutionCode": "{institution_code}",
            "SelectedDate": "{now_iso}",
            "View": "daily",
            "spaceId": 0,
            "language": "he",
            "item": "",
            "ClassNum": "{class_number}",
            "ClassCode": "{class_code}",
            "ChildId": "{student_id}",
            "year": "{study_year}",
        },
        per_student=True,
    ),
    Endpoint(
        "class_schedule",
        "shotef/ShotefSchedualeData",
        {"institutionCode": "{institution_code}", "selectedValue": SELECTED_CLASS, "typeView": 1},
        per_student=True,
    ),
    Endpoint(
        "changes_week",
        "shotef/ChangesAndMessagesData",
        {
            "institutionCode": "{institution_code}",
            "selectedValue": SELECTED_CLASS,
            "typeView": 1,
            "todayOnly": False,
            "startWeek": True,
        },
        per_student=True,
    ),
    Endpoint("pupil_schedule", "PupilCard/GetPupilScheduale", PUPIL_CARD, per_student=True),
    Endpoint("pupil_lessons_homework", "PupilCard/GetPupilLessonsAndHomework", PUPIL_CARD, per_student=True),
    Endpoint("pupil_grades", "PupilCard/GetPupilGrades", PUPIL_CARD, per_student=True),
    Endpoint("pupil_period_grades", "PupilCard/GetPupilPeriodGrades", PUPIL_CARD, per_student=True),
    Endpoint("pupil_discipline", "PupilCard/GetPupilDiciplineEvents", PUPIL_CARD, per_student=True),
    Endpoint("pupil_previous_homework", "PupilCard/GetPreviousHomeworks", PUPIL_CARD, per_student=True),
)


def fill(template: Any, context: dict[str, Any]) -> Any:
    """Substitute ``{name}`` placeholders; a value that is exactly one placeholder keeps its type."""
    if isinstance(template, dict):
        return {k: fill(v, context) for k, v in template.items()}
    if isinstance(template, str):
        whole = re.fullmatch(r"\{(\w+)\}", template)
        if whole:
            return context.get(whole.group(1))
        return re.sub(r"\{(\w+)\}", lambda m: str(context.get(m.group(1), "")), template)
    return template


def pick(data: dict[str, Any], *names: str) -> Any:
    for name in names:
        if data.get(name) not in (None, ""):
            return data[name]
    return None


def parser_summary(name: str, data: Any, today: str) -> dict[str, int] | None:
    """How many items the integration's parsers extract from the RAW data (counts only)."""
    try:
        if name == "dashboard_init":
            return {"students": len(models.parse_students(data))}
        if name == "messages_inbox":
            return {"messages": len(models.parse_messages(data))}
        if name == "unread_notifications":
            count = models.parse_unread_count(data)
            return {"unread": -1 if count is None else count}
        if name.startswith("homework_today"):
            return {"homework": len(models.parse_homework(data, date=today))}
        if name.startswith(("discipline_dashboard", "pupil_discipline")):
            return {"events": len(models.parse_discipline(data))}
        if name.startswith(("pupil_schedule", "pupil_lessons_homework", "schedule_today", "class_schedule")):
            return {"lessons": len(models.parse_schedule(data))}
    except Exception as err:
        return {f"parser_crashed_{type(err).__name__}": 1}
    return None


async def capture(args: argparse.Namespace, username: str, password: str, llm: LocalLLM | None) -> int:
    now = datetime.now().astimezone()
    today = now.strftime("%Y-%m-%d")

    try:
        counter = httpx.post(f"{BASE_URL}{API}user/getInput", json={}, timeout=20).json()
    except Exception:
        counter = 0
    counter = counter if isinstance(counter, int) and not isinstance(counter, bool) else 0

    raw: dict[str, Any] = {}
    manifest: list[dict[str, Any]] = []
    literals: set[str] = {username, password}

    async with WebtopClient(
        username=username,
        password=password,
        data=compute_login_data(username, counter),
        timeout=30.0,
        auto_login=False,
    ) as client:
        try:
            session = await client.login()
        except WebtopLoginError as err:
            reason = re.search(r"errorDescription=('?[\w ]*'?)", str(err))
            print(f"\nLogin failed (login counter was {counter}).")
            print(f"Server said: {reason.group(1) if reason else 'no reason given / network error'}")
            if counter >= 2:
                print("A counter of 2 or more means the portal wants a captcha from this network.")
                print("Sign in once in a browser, wait a while, and run this again.")
            return 2

        login = dict(session.raw_login_data or {})
        raw["login"] = login
        literals |= {str(v) for v in (session.first_name, session.last_name, session.school_name) if v}
        literals |= {str(login.get(k)) for k in ("userId", "id", "studentId", "userName") if login.get(k)}
        base_context = {
            "institution_code": pick(login, "institutionCode", "InstitutionCode", "schoolId"),
            "study_year": now.year + 1 if now.month >= 8 else now.year,
            "now_iso": now.replace(microsecond=0).isoformat(),
        }
        print(f"Logged in. Capturing {len(ENDPOINTS)} read-only endpoints ...")

        students: list[models.Student] = []

        async def call(endpoint: Endpoint, context: dict[str, Any], suffix: str = "") -> None:
            name = endpoint.name + suffix
            entry: dict[str, Any] = {"name": name, "path": endpoint.path, "payload_template": endpoint.payload}
            try:
                response = await client.request("POST", API + endpoint.path, json=fill(endpoint.payload, context))
                body = response.json()
            except WebtopError as err:
                status = re.search(r"\((\d{3})\)", str(err))
                entry["http_error"] = int(status.group(1)) if status else type(err).__name__
                body = None
            except ValueError:
                entry["http_error"] = "response was not JSON"
                body = None
            if body is not None:
                raw[name] = body
                if isinstance(body, dict):
                    entry["status"] = body.get("status")
                    description = body.get("errorDescription")
                    if isinstance(description, str) and description:
                        entry["errorDescription"] = (
                            description if _SAFE_ERROR_RE.match(description) else "<non-ascii text>"
                        )
                    data = body.get("data")
                    entry["data_type"] = type(data).__name__
                    summary = parser_summary(name, data, today)
                    if summary:
                        entry["parsed_by_integration"] = summary
            manifest.append(entry)
            flag = entry.get("http_error") or ("ok" if entry.get("status") is not False else "status=false")
            print(f"  {name:<34} {flag}")
            await asyncio.sleep(0.4)

        for endpoint in ENDPOINTS:
            if not endpoint.per_student:
                await call(endpoint, base_context)
                if endpoint.name == "dashboard_init":
                    dashboard = raw.get("dashboard_init")
                    data = dashboard.get("data") if isinstance(dashboard, dict) else None
                    students = models.parse_students(data)
                    if not students:
                        fallback = models.student_from_login(login)
                        students = [fallback] if fallback else []
                    for s in students:
                        literals |= {s.name, s.student_id, s.key, *s.name.split()}
                        literals |= {str(s.raw.get(k)) for k in ("firstName", "lastName") if s.raw.get(k)}
                continue
            for index, student in enumerate(students[: args.max_students], 1):
                context = {
                    **base_context,
                    "student_id": student.student_id,
                    "class_code": student.class_code,
                    "class_number": student.class_number,
                    "study_year": student.study_year or base_context["study_year"],
                }
                await call(endpoint, context, suffix=f"__student{index}")

    for linked in (raw.get("linked_students") or {}).get("data") or []:
        if isinstance(linked, dict):
            literals |= {str(v) for v in linked.values() if isinstance(v, str) and 3 <= len(v) <= 40}

    how = f"with local model {llm.model}" if llm else "in strict mode"
    print(f"\nAnonymizing {len(raw)} responses {how} ...")
    result = anonymize_documents(raw, llm=llm, known_literals=literals)

    out_dir = Path(args.out) / now.strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=False)
    for index, (name, doc) in enumerate(result.documents.items()):
        (out_dir / f"{index:02d}_{name}.json").write_text(
            json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    (out_dir / "manifest.json").write_text(
        json.dumps(
            {
                "captured_at": today,
                "students_captured": min(len(students), args.max_students),
                "students_on_account": len(students),
                "login_counter": counter,
                "anonymizer": {
                    "model": llm.model if llm else None,
                    "fields_sent_to_model": result.llm_paths,
                    "fields_answered": result.llm_answered,
                    "values_redacted_by_default": result.redacted_by_default,
                    "leaks_removed_by_final_check": result.leaks_removed,
                    "plan": {p.split(".", 1)[-1] if "." in p else p: c for p, c in result.plan.items()},
                },
                "requests": manifest,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"\nWrote {len(result.documents)} anonymized files to {out_dir}/")
    print(f"  fields classified by the local model: {result.llm_answered}/{result.llm_paths}")
    print(f"  values redacted by default:           {result.redacted_by_default}")
    print(f"  leaks removed by the final check:     {result.leaks_removed}")
    print("Nothing raw was written. Skim the files before sharing them.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default="captures", help="output directory (default: captures/)")
    parser.add_argument("--username", help="Webtop username (otherwise you are asked)")
    parser.add_argument("--max-students", type=int, default=2, help="capture at most this many children (default 2)")
    parser.add_argument("--llm-url", help="local OpenAI-compatible chat completions URL (loopback only)")
    parser.add_argument("--llm-model", default="", help="model name to send with --llm-url")
    parser.add_argument("--prefer", default="qwen,gemma", help="model families to look for, in order")
    parser.add_argument(
        "--no-llm", action="store_true", help="strict mode: redact every text value the key-name rules do not cover"
    )
    args = parser.parse_args()

    llm: LocalLLM | None = None
    if not args.no_llm:
        if args.llm_url:
            llm = LocalLLM(url=args.llm_url, model=args.llm_model, api_key=os.environ.get("WEBTOP_LLM_API_KEY", ""))
        else:
            llm = discover_local_llm(tuple(p.strip() for p in args.prefer.split(",") if p.strip()))
        if llm is None:
            print("No local llama-server with a Qwen or Gemma model was found.")
            print("Start one, pass --llm-url http://127.0.0.1:PORT/v1/chat/completions, or use --no-llm.")
            return 1
        print(f"Anonymizer model: {llm.model or '(default)'} at {llm.url}")

    if not sys.stdin.isatty():
        print("Run this in an interactive terminal so the password prompt stays private.")
        return 1
    username = args.username or input("Webtop username: ").strip()
    password = getpass.getpass("Webtop password (not echoed, not stored): ")
    if not username or not password:
        print("Username and password are required.")
        return 1
    started = time.monotonic()
    logging.disable(logging.CRITICAL)  # pywebtop logs usernames and response bodies
    try:
        code = asyncio.run(capture(args, username, password, llm))
    finally:
        del password
        logging.disable(logging.NOTSET)
    print(f"Done in {time.monotonic() - started:.0f}s.")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
