"""Anonymize Webtop JSON responses before anyone (or any cloud model) reads them.

How it works:

1. Every leaf value is addressed by a *path* such as ``data.childrens[].firstName``.
2. Each path gets a category. Hard rules on the key name come first and cannot be
   overridden (tokens, ids, names of people, contact details, grades). Remaining
   string paths are classified by a **local** LLM that sees a few sample values;
   the request never leaves this machine (loopback only).
3. Values are replaced deterministically per category, so the JSON keeps its
   exact structure, key names, value types and cross-references (the same
   original always maps to the same placeholder within a run).
4. Anything unclassified is redacted (default deny), and a final leak check
   removes any value that still contains a known personal literal.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import re
import secrets
import subprocess
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

KEEP = "keep"
PERSON = "person_name"
ORGANIZATION = "organization"
IDENTIFIER = "identifier"
CONTACT = "contact"
SECRET = "secret"
FREE_TEXT = "free_text"
GRADE = "grade"
CATEGORIES = (KEEP, PERSON, ORGANIZATION, IDENTIFIER, CONTACT, SECRET, FREE_TEXT, GRADE)
# What the LLM may answer. Secrets and grades are decided by key name only.
LLM_CATEGORIES = (KEEP, PERSON, ORGANIZATION, IDENTIFIER, CONTACT, FREE_TEXT)

_SECRET_RE = re.compile(r"token|passw|secret|cookie|biolog|biometric|otp|apikey|api_key|signature", re.I)
_CONTACT_RE = re.compile(r"phone|mobile|cell|e?mail|address|street|zip|fax", re.I)
_PERSON_RE = re.compile(
    r"(first|last|private|family|full|student|pupil|teacher|sender|parent|user|child|educator|display)[_ ]?"
    r"(f_|l_)?name|^student_[fl]_name$|^teacher$|^sender$|^from(title)?$|usertitle|nickname",
    re.I,
)
_ORGANIZATION_RE = re.compile(r"(school|institution|institute|mosad)[_ ]?name|^school$|city|settlement", re.I)
_IDENTIFIER_RE = re.compile(
    r"(^|_)id$|[a-z]Id$|ID$|^id|identity|tz$|teudat|login|guid|uuid|url|link|image|photo|avatar|file|path|"
    r"serial|birth|institutioncode|schoolcode|semel",
    re.I,
)
_GRADE_RE = re.compile(r"grade(?!s?list)|(?<!re)mark$|score|assessment|evaluation", re.I)
# Grade-looking keys that are really class/grade-level codes, not marks.
_NOT_A_MARK_RE = re.compile(r"class|level|layer|code|type|weight|id$", re.I)

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?)?$")
_SLASH_DATE_RE = re.compile(r"^\d{1,2}[/.]\d{1,2}[/.]\d{2,4}$")
_TIME_RE = re.compile(r"^\d{1,2}:\d{2}(:\d{2})?$")
_NUMERIC_RE = re.compile(r"^-?\d{1,4}([.,]\d+)?$")

# Stock phrases teachers type for "no homework"; generic, and parsers depend on them.
GENERIC_VALUES = frozenset({"אין", "אין שיעורי בית", "אין שעורי בית", "לא הוזן", "ללא", "ללא שיעורי בית", "-", "--"})

MAX_SAMPLES = 4
MAX_SAMPLE_CHARS = 60
LLM_BATCH = 30


def leaf_key(path: str) -> str:
    """The last key of a path, without list markers."""
    return path.replace("[]", "").rsplit(".", 1)[-1]


def walk(node: Any, path: str = "$"):
    """Yield ``(path, value)`` for every leaf."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield from walk(value, f"{path}.{key}")
    elif isinstance(node, list):
        for item in node:
            yield from walk(item, f"{path}[]")
    else:
        yield path, node


def is_datetime_string(value: str) -> bool:
    v = value.strip()
    return bool(_DATE_RE.match(v) or _SLASH_DATE_RE.match(v) or _TIME_RE.match(v))


def is_structural_string(value: str) -> bool:
    """Dates, times, short numbers and stock phrases carry no identity on their own."""
    v = value.strip()
    stock = " ".join(v.split()).strip(".!:;־- ")
    if not v or v in GENERIC_VALUES or stock in GENERIC_VALUES:
        return True
    return is_datetime_string(v) or bool(_NUMERIC_RE.match(v))


# Subjects and titles of messages are written by people, even when a sample looks generic.
_MESSAGE_DOC_RE = re.compile(r"inbox|messages|notification", re.I)
_MESSAGE_TEXT_KEYS = frozenset({"subject", "title", "message", "body", "content", "text"})


def hard_rule(path: str) -> str | None:
    """Category forced by the key name, or None to let the LLM decide."""
    key = leaf_key(path)
    document = path.split(".", 1)[0]
    if key in _MESSAGE_TEXT_KEYS and _MESSAGE_DOC_RE.search(document):
        return FREE_TEXT
    if _SECRET_RE.search(key):
        return SECRET
    if _CONTACT_RE.search(key):
        return CONTACT
    if _ORGANIZATION_RE.search(key):
        return ORGANIZATION
    if _PERSON_RE.search(key):
        return PERSON
    if _GRADE_RE.search(key) and not _NOT_A_MARK_RE.search(key):
        return GRADE
    if _IDENTIFIER_RE.search(key):
        return IDENTIFIER
    return None


@dataclass
class PathInfo:
    samples: list[str] = field(default_factory=list)
    count: int = 0
    has_text: bool = False  # at least one non-structural string value


def collect_paths(documents: dict[str, Any]) -> dict[str, PathInfo]:
    """Sample string values per path across all captured documents."""
    paths: dict[str, PathInfo] = {}
    for name, doc in documents.items():
        for path, value in walk(doc, name):
            if not isinstance(value, str):
                continue
            info = paths.setdefault(path, PathInfo())
            info.count += 1
            if is_structural_string(value):
                continue
            info.has_text = True
            sample = value.strip()[:MAX_SAMPLE_CHARS]
            if sample not in info.samples and len(info.samples) < MAX_SAMPLES:
                info.samples.append(sample)
    return paths


# ---------------------------------------------------------------------------
# local LLM
# ---------------------------------------------------------------------------
@dataclass
class LocalLLM:
    url: str  # .../v1/chat/completions
    model: str = ""
    api_key: str = ""
    timeout: float = 180.0

    def __post_init__(self) -> None:
        host = urlparse(self.url).hostname or ""
        try:
            loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = False
        if not loopback:
            raise ValueError(
                f"Refusing to send raw school data to {host!r}: the anonymizer only talks to a loopback address."
            )

    def chat(self, system: str, user: str) -> str:
        import httpx

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        body = {
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0,
            "max_tokens": 2000,
            "response_format": {"type": "json_object"},
            "chat_template_kwargs": {"enable_thinking": False},
        }
        if self.model:
            body["model"] = self.model
        response = httpx.post(self.url, json=body, headers=headers, timeout=self.timeout)
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"] or ""


def discover_local_llm(preferences: tuple[str, ...] = ("qwen", "gemma")) -> LocalLLM | None:
    """Find a running llama-server on this machine, preferring the given model families."""
    try:
        listing = subprocess.run(
            ["ps", "-axo", "command"], capture_output=True, text=True, timeout=10, check=False
        ).stdout
    except OSError:
        return None
    servers: list[tuple[str, str, str]] = []
    for line in listing.splitlines():
        if "llama-server" not in line:
            continue
        model = re.search(r"(?:^|\s)(?:-m|--model)\s+(\S+)", line)
        port = re.search(r"--port\s+(\d+)", line)
        key = re.search(r"--api-key\s+(\S+)", line)
        if model:
            servers.append((model.group(1), port.group(1) if port else "8080", key.group(1) if key else ""))
    for wanted in preferences:
        for model, port, key in servers:
            name = model.rsplit("/", 1)[-1]
            if wanted in name.casefold():
                return LocalLLM(url=f"http://127.0.0.1:{port}/v1/chat/completions", model=name, api_key=key)
    return None


_SYSTEM_PROMPT = """You help anonymize JSON captured from an Israeli school portal (Hebrew and English) \
so that it can be shared with a developer. For each numbered field you get its JSON path and a few sample values. \
Answer with exactly one category per field:

- keep: generic, non-personal values that are the same for many people. School subject names (מתמטיקה, אנגלית), \
lesson or event type labels, status words, weekday names, class level labels (ז, ח), generic titles such as \
"מבחן" or "בוחן", colours, language codes, enum-like words.
- person_name: the name of a person (student, parent, teacher, staff), in any order or with a title.
- organization: the name of a school, institution, municipality, class nickname or any place that locates the person.
- identifier: values that identify one person, account or record: ID numbers, usernames, codes, hashes, URLs, \
file names.
- contact: phone numbers, e-mail addresses, postal addresses.
- free_text: text written by a person: message subjects and bodies, homework descriptions, remarks, notes, \
event titles that mention specifics. When a field mixes generic and specific text, choose free_text.

If you are unsure, choose free_text. Reply with a JSON object mapping each field number to its category, for example \
{"1": "keep", "2": "person_name"}. No other text."""


def classify_with_llm(llm: LocalLLM, paths: dict[str, PathInfo]) -> dict[str, str]:
    """Ask the local model for a category per path. Missing or invalid answers are left out."""
    result: dict[str, str] = {}
    items = list(paths.items())
    for start in range(0, len(items), LLM_BATCH):
        batch = items[start : start + LLM_BATCH]
        lines = [
            f"{i}. path: {path.split('.', 1)[-1]}\n   samples: {json.dumps(info.samples, ensure_ascii=False)}"
            for i, (path, info) in enumerate(batch, 1)
        ]
        print(f"  classifying fields {start + 1}-{start + len(batch)} of {len(items)} ...", flush=True)
        try:
            content = llm.chat(_SYSTEM_PROMPT, "\n".join(lines))
            match = re.search(r"\{.*\}", content, re.S)
            answers = json.loads(match.group(0)) if match else {}
        except Exception as err:
            print(f"  ! local model failed on a batch ({type(err).__name__}); those fields will be redacted")
            continue
        if not isinstance(answers, dict):
            continue
        for i, (path, _info) in enumerate(batch, 1):
            category = answers.get(str(i))
            if isinstance(category, str) and category.strip().lower() in LLM_CATEGORIES:
                result[path] = category.strip().lower()
    return result


# ---------------------------------------------------------------------------
# replacement
# ---------------------------------------------------------------------------
class Anonymizer:
    """Applies a path → category plan to documents."""

    def __init__(self, plan: dict[str, str], *, salt: bytes | None = None) -> None:
        self.plan = plan
        self._salt = salt or secrets.token_bytes(16)
        self._labels: dict[tuple[str, str], str] = {}
        self._counters: dict[str, int] = {}
        self.redacted_by_default = 0

    # -- helpers --------------------------------------------------------
    def _digest(self, value: str) -> bytes:
        return hmac.new(self._salt, value.encode("utf-8"), hashlib.sha256).digest()

    def _label(self, kind: str, word: str, value: str) -> str:
        key = (kind, " ".join(value.split()).casefold())
        if key not in self._labels:
            self._counters[kind] = self._counters.get(kind, 0) + 1
            self._labels[key] = f"{word}{self._counters[kind]}"
        return self._labels[key]

    def _scramble(self, value: str) -> str:
        """Same length and character classes, different content."""
        digest = self._digest(value)
        out = []
        for i, ch in enumerate(value):
            b = digest[i % len(digest)]
            if ch.isdigit():
                out.append(str(b % 10))
            elif ch.isalpha() and ch.isascii():
                out.append(chr((ord("A") if ch.isupper() else ord("a")) + b % 26))
            elif ch.isalpha():
                out.append("א")
            else:
                out.append(ch)
        return "".join(out)

    def _fake_grade(self, value: Any) -> Any:
        number = 55 + self._digest(str(value))[0] % 46
        if isinstance(value, bool):
            return value
        if isinstance(value, int):
            return number
        if isinstance(value, float):
            return float(number)
        if isinstance(value, str):
            return str(number) if _NUMERIC_RE.match(value.strip()) else self._label(GRADE, "הערכה", value)
        return value

    # -- core -----------------------------------------------------------
    def category_for(self, path: str, value: Any) -> str:
        forced = hard_rule(path)
        if forced:
            return forced
        if isinstance(value, str):
            if is_structural_string(value):
                return KEEP
            category = self.plan.get(path)
            if category is None:
                self.redacted_by_default += 1
                return FREE_TEXT
            return category
        if isinstance(value, int) and not isinstance(value, bool) and abs(value) >= 100_000:
            return IDENTIFIER  # long numbers are ids unless a hard rule said otherwise
        return KEEP

    def transform(self, path: str, value: Any) -> Any:
        if value is None or isinstance(value, bool):
            return value
        category = self.category_for(path, value)
        if category == KEEP:
            return value
        if category == GRADE:
            return self._fake_grade(value)
        if isinstance(value, (int, float)):
            # Small numbers under id-like keys are enum codes (typeID, moduleID); keep them readable.
            if category in (IDENTIFIER, CONTACT, SECRET) and abs(value) >= 1000:
                scrambled = self._scramble(str(abs(int(value))))
                return int(scrambled.lstrip("0") or "1")
            return value
        if not value.strip():
            return value
        if category == SECRET:
            return "<secret>"
        if category == PERSON:
            return self._label(PERSON, "שם", value)
        if category == ORGANIZATION:
            return self._label(ORGANIZATION, "מוסד", value)
        if category in (IDENTIFIER, CONTACT):
            if is_datetime_string(value):
                # A timestamp under an id-like key (lastLogin) is still just a timestamp,
                # except a date of birth, which is replaced.
                if "birth" not in leaf_key(path).casefold():
                    return value
                return "2000-01-01T00:00:00" if "T" in value else "2000-01-01"
            return self._scramble(value)
        return f"{self._label(FREE_TEXT, 'טקסט', value)} ({len(value)} תווים)"

    def apply(self, node: Any, path: str) -> Any:
        if isinstance(node, dict):
            return {key: self.apply(value, f"{path}.{key}") for key, value in node.items()}
        if isinstance(node, list):
            return [self.apply(item, f"{path}[]") for item in node]
        return self.transform(path, node)


def scrub_leaks(node: Any, literals: set[str]) -> tuple[Any, int]:
    """Replace any remaining string that contains a known personal literal."""
    needles = {" ".join(lit.split()).casefold() for lit in literals if lit and len(lit.strip()) >= 3}
    hits = 0

    def visit(value: Any) -> Any:
        nonlocal hits
        if isinstance(value, dict):
            return {k: visit(v) for k, v in value.items()}
        if isinstance(value, list):
            return [visit(v) for v in value]
        if isinstance(value, str):
            folded = " ".join(value.split()).casefold()
            if any(needle in folded for needle in needles):
                hits += 1
                return "<leak-redacted>"
        if isinstance(value, int) and not isinstance(value, bool) and str(value) in needles:
            hits += 1
            return 0
        return value

    return visit(node), hits


def same_shape(a: Any, b: Any) -> bool:
    """True when two JSON values have identical structure and value types."""
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(same_shape(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(same_shape(x, y) for x, y in zip(a, b, strict=True))
    return type(a) is type(b)


@dataclass
class AnonymizationResult:
    documents: dict[str, Any]
    plan: dict[str, str]
    leaks_removed: int
    redacted_by_default: int
    llm_paths: int
    llm_answered: int


def anonymize_documents(
    documents: dict[str, Any], *, llm: LocalLLM | None, known_literals: set[str] | None = None
) -> AnonymizationResult:
    """Anonymize a set of named JSON documents in one consistent pass."""
    paths = collect_paths(documents)
    undecided = {p: info for p, info in paths.items() if info.has_text and hard_rule(p) is None}
    plan = classify_with_llm(llm, undecided) if llm and undecided else {}
    anonymizer = Anonymizer(plan)
    out: dict[str, Any] = {}
    leaks = 0
    for name, doc in documents.items():
        cleaned = anonymizer.apply(doc, name)
        cleaned, hits = scrub_leaks(cleaned, known_literals or set())
        leaks += hits
        if not same_shape(doc, cleaned):  # cannot happen; refuse to write if it somehow does
            raise RuntimeError(f"anonymized {name} lost its structure")
        out[name] = cleaned
    full_plan = {p: (hard_rule(p) or plan.get(p) or FREE_TEXT) for p, info in paths.items() if info.has_text}
    return AnonymizationResult(
        documents=out,
        plan=dict(sorted(full_plan.items())),
        leaks_removed=leaks,
        redacted_by_default=anonymizer.redacted_by_default,
        llm_paths=len(undecided),
        llm_answered=len(plan),
    )
