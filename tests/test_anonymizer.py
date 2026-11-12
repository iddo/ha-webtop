"""Tests for the capture tool's anonymizer (no network, fake local model)."""

from __future__ import annotations

import json

import pytest

from tools import anonymizer as an
from tools.webtop_capture import ENDPOINTS, fill


class FakeLLM:
    """Answers from a path-suffix table; everything else is left unanswered."""

    model = "fake"

    def __init__(self, table: dict[str, str], *, garbage: bool = False) -> None:
        self.table = table
        self.garbage = garbage
        self.prompts: list[str] = []

    def chat(self, system: str, user: str) -> str:
        self.prompts.append(user)
        if self.garbage:
            return "sorry, I cannot do that"
        answers = {}
        for line in user.splitlines():
            if ". path: " in line:
                number, path = line.split(". path: ")
                for suffix, category in self.table.items():
                    if path.strip().endswith(suffix):
                        answers[number.strip()] = category
        return "Here you go: " + json.dumps(answers)


DOCS = {
    "login": {
        "token": "tok-abc",
        "userId": "u-918273",
        "studentId": 3344556,
        "firstName": "דנה",
        "lastName": "ישראלי",
        "schoolName": "תיכון הדוגמה",
        "cellphone": "052-5550123",
        "birthDate": "2012-03-04T00:00:00",
        "lastLogin": "2026-11-10T07:12:00",
        "classCode": 8,
        "typeID": 3,
        "isParent": True,
        "nothing": None,
    },
    "homework": {
        "status": True,
        "data": {
            "dataTable": [
                {"lesson": "מתמטיקה", "teacher": "כהן רחל", "homeworkData": "עמוד 45 של דנה", "hourNum": "1"},
                {"lesson": "אנגלית", "teacher": "כהן רחל", "homeworkData": "אין", "hourNum": "2"},
            ]
        },
    },
    "grades": {"data": [{"subject": "מדעים", "grade": 87, "remark": "יפה מאוד", "weight": 10, "gradeLevel": 8}]},
}
LLM_TABLE = {"lesson": "keep", "homeworkData": "free_text", "subject": "keep", "remark": "free_text"}


@pytest.fixture
def result() -> an.AnonymizationResult:
    return an.anonymize_documents(DOCS, llm=FakeLLM(LLM_TABLE), known_literals={"דנה", "ישראלי"})


def test_structure_keys_and_types_are_preserved(result) -> None:
    for name, doc in DOCS.items():
        assert an.same_shape(doc, result.documents[name])


def test_no_original_personal_value_survives(result) -> None:
    dumped = json.dumps(result.documents, ensure_ascii=False)
    originals = ("tok-abc", "u-918273", "3344556", "דנה", "ישראלי", "תיכון", "052-5550123", "2012-03-04", "כהן")
    for secret in originals:
        assert secret not in dumped, secret


def test_generic_values_are_kept(result) -> None:
    login, rows = result.documents["login"], result.documents["homework"]["data"]["dataTable"]
    assert [r["lesson"] for r in rows] == ["מתמטיקה", "אנגלית"]
    assert rows[1]["homeworkData"] == "אין"  # stock "no homework" phrase
    assert [r["hourNum"] for r in rows] == ["1", "2"]
    assert login["lastLogin"] == "2026-11-10T07:12:00"  # a timestamp, despite the id-like key
    assert login["birthDate"] == "2000-01-01T00:00:00"
    assert (login["classCode"], login["typeID"], login["isParent"], login["nothing"]) == (8, 3, True, None)
    grade = result.documents["grades"]["data"][0]
    assert grade["subject"] == "מדעים" and grade["weight"] == 10 and grade["gradeLevel"] == 8
    assert isinstance(grade["grade"], int) and 55 <= grade["grade"] <= 100


def test_replacements_are_consistent_and_typed(result) -> None:
    rows = result.documents["homework"]["data"]["dataTable"]
    assert rows[0]["teacher"] == rows[1]["teacher"]  # same person, same placeholder
    assert rows[0]["teacher"].startswith("שם")
    assert rows[0]["homeworkData"].startswith("טקסט") and "תווים" in rows[0]["homeworkData"]
    login = result.documents["login"]
    assert login["token"] == "<secret>"
    assert isinstance(login["studentId"], int) and login["studentId"] != 3344556
    assert len(login["cellphone"]) == len("052-5550123") and login["cellphone"][3] == "-"
    assert login["schoolName"].startswith("מוסד")


def test_plan_lists_categories_but_no_values(result) -> None:
    assert result.plan["login.token"] == an.SECRET
    assert result.plan["homework.data.dataTable[].lesson"] == an.KEEP
    assert result.plan["grades.data[].remark"] == an.FREE_TEXT
    assert not any("דנה" in key or "דנה" in value for key, value in result.plan.items())


def test_hard_rules_override_the_model() -> None:
    liar = FakeLLM({"firstName": "keep", "token": "keep", "teacher": "keep", "schoolName": "keep"})
    out = an.anonymize_documents(DOCS, llm=liar).documents
    assert out["login"]["firstName"] != "דנה" and out["login"]["token"] == "<secret>"
    assert out["homework"]["data"]["dataTable"][0]["teacher"] != "כהן רחל"
    # ...and such fields are never even sent to the model.
    assert "firstName" not in "".join(liar.prompts) and "token" not in "".join(liar.prompts)


@pytest.mark.parametrize("llm", [None, FakeLLM({}, garbage=True), FakeLLM({})])
def test_default_deny_without_a_usable_model(llm) -> None:
    result = an.anonymize_documents(DOCS, llm=llm)
    rows = result.documents["homework"]["data"]["dataTable"]
    assert rows[0]["lesson"].startswith("טקסט")  # unclassified text is redacted
    assert rows[0]["hourNum"] == "1"
    assert result.redacted_by_default > 0


def test_leak_check_catches_known_names_in_kept_fields() -> None:
    docs = {"x": {"data": [{"subject": "מתמטיקה עם דנה ישראלי"}, {"subject": "אנגלית"}]}}
    result = an.anonymize_documents(docs, llm=FakeLLM({"subject": "keep"}), known_literals={"דנה ישראלי"})
    assert [r["subject"] for r in result.documents["x"]["data"]] == ["<leak-redacted>", "אנגלית"]
    assert result.leaks_removed == 1


@pytest.mark.parametrize(
    "url", ["http://127.0.0.1:8081/v1/chat/completions", "http://localhost:1/v1/chat/completions", "http://[::1]:9/x"]
)
def test_local_llm_accepts_loopback(url: str) -> None:
    assert an.LocalLLM(url=url).url == url


@pytest.mark.parametrize("url", ["https://api.example.com/v1/chat/completions", "http://192.168.1.20:8081/v1", "x"])
def test_local_llm_refuses_non_loopback(url: str) -> None:
    with pytest.raises(ValueError, match="loopback"):
        an.LocalLLM(url=url)


@pytest.mark.parametrize(
    ("key", "category"),
    [
        ("webToken", an.SECRET), ("Password", an.SECRET), ("bioLogin", an.SECRET),
        ("teacherLastName", an.PERSON), ("student_F_name", an.PERSON), ("private_name", an.PERSON),
        ("studentId", an.IDENTIFIER), ("id", an.IDENTIFIER), ("studentID", an.IDENTIFIER), ("imageUrl", an.IDENTIFIER),
        ("email", an.CONTACT), ("cellphone", an.CONTACT), ("schoolName", an.ORGANIZATION),
        ("grade", an.GRADE), ("numeric_grade", an.GRADE),
        ("remark", None), ("subject_name", None), ("classCode", None), ("gradeLevel", None), ("homeWork", None),
    ],
)  # fmt: skip
def test_hard_rules(key: str, category: str | None) -> None:
    assert an.hard_rule(f"doc.data[].{key}") == category


def test_capture_endpoints_are_read_only_and_fillable() -> None:
    context = {
        "institution_code": 1, "study_year": 2027, "now_iso": "2026-11-10T12:00:00+02:00",
        "student_id": "enc", "class_code": 8, "class_number": 3,
    }  # fmt: skip
    names = [e.name for e in ENDPOINTS]
    assert len(names) == len(set(names))
    for endpoint in ENDPOINTS:
        action = endpoint.path.rsplit("/", 1)[-1]
        assert action.startswith(("Get", "Init")) or action in {
            "ShotefSchedualeDataForToday", "ChangesAndMessagesDataForToday", "WeeklySchedualeForToday",
            "EventsTestsBoard", "ShotefSchedualeData", "ChangesAndMessagesData",
        }, endpoint.path  # fmt: skip
        payload = fill(endpoint.payload, context)
        assert not any(isinstance(v, str) and "{" in v for v in payload.values()), payload
    assert fill({"a": "{class_code}", "b": "{class_code}|{class_number}"}, context) == {"a": 8, "b": "8|3"}
