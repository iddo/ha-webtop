# Webtop (SmartSchool) for Home Assistant

A [HACS](https://hacs.xyz) custom integration that brings your children's
**Webtop / SmartSchool** student portal (webtop.smartschool.co.il, used by
Israeli schools) into Home Assistant, built on the
[pywebtop](https://github.com/t0mer/pywebtop) library.

You sign in with the same username and password you use on the portal. A
parent account sees every linked child, including siblings in different
schools.

## What you get

One **account device** and one **device per student**.

| Entity | Device | State | Attributes |
| --- | --- | --- | --- |
| Current lesson | student | the subject being taught right now, `Break` between lessons, `No lesson` outside school hours | teacher, room, hour, start, end, changed, next_subject, next_start, lessons_today, times_known |
| Next test | student | date of the next exam for the student's class | title, subject, start, end, from_hour, to_hour, note, days_until |
| Upcoming tests | student | number of exams from today onward | `tests` list |
| Latest grade | student | the most recent mark | subject, title, type, date, weight, teacher, remark, `recent` list |
| Schedule changes today | student | substitutions, cancellations, exams and notices on today's board | `changes` (kind, text, hour, teacher, room, note) |
| Homework today | student | number of assignments due today | `items` (subject, teacher, hour, homework text, status), `text` |
| Behaviour events | student | number of discipline records | `events` (date, event, subject, teacher, remark, justified) |
| Lessons today | student | number of timetable slots today | `today`, `week` (date, hour, subject, teacher, room, changed, start, end) |
| Class | student (diagnostic) | class name, e.g. `ג'2` | school, class code/number, study year |
| School calendar | student (calendar) | exams, school events and vacations for the student's class | standard calendar events; timed when lesson numbers are known |
| Unread messages | account | unread count in the inbox | `total`, `messages` (subject, sender, sent_at, read/attachment flags, student) |
| Last message | account | subject of the newest message | sender, sent_at, flags |
| Unread notifications | account | unread portal notifications | |

### Current lesson and history

The **Current lesson** sensor changes state at the exact minute a lesson
starts or ends, using the timetable and the school's bell schedule. It runs
on its own timer, so it does not wait for the next poll. Open the entity's
history (or add a History graph card) to see the day as a strip of subjects.
If the portal gives lessons but no lesson times, the state is `unknown` and
the `times_known` attribute is `false`.

### Events

Events are fired on the Home Assistant bus once a running instance sees
something it had not seen before. The first poll after a restart only records
a baseline, so nothing is replayed.

| Event | Data |
| --- | --- |
| `webtop_new_homework` | `student`, `student_key`, `subject`, `teacher`, `homework`, `hour`, `date`, `status`, `entry_id` |
| `webtop_new_grade` | `student`, `student_key`, `subject`, `grade`, `title`, `type`, `date`, `weight`, `teacher`, `remark`, `entry_id` |
| `webtop_new_test` | `student`, `student_key`, `title`, `subject`, `date`, `start`, `end`, `from_hour`, `to_hour`, `note`, `entry_id` |
| `webtop_new_message` | `subject`, `sender`, `sent_at`, `has_read`, `has_files`, `student`, `entry_id` |

Example automation:

```yaml
automation:
  - alias: Homework notification
    triggers:
      - trigger: event
        event_type: webtop_new_homework
    actions:
      - action: notify.mobile_app_phone
        data:
          title: "New homework for {{ trigger.event.data.student }}"
          message: "{{ trigger.event.data.subject }}: {{ trigger.event.data.homework }}"
```

## Installation

### HACS (recommended)

1. HACS → Integrations → ⋮ → **Custom repositories**.
2. Add `https://github.com/iddo/ha-webtop`, category **Integration**.
3. Install **Webtop (SmartSchool)** and restart Home Assistant.

### Manual

Copy `custom_components/webtop` into your `config/custom_components/`
directory and restart Home Assistant.

## Configuration

Settings → Devices & services → **Add integration** → *Webtop (SmartSchool)*.

| Field | Meaning |
| --- | --- |
| Username / Password | Your Webtop portal credentials. |
| Login data override (advanced) | Leave empty. The login request carries a `Data` value derived from your username; the integration computes it the same way the Webtop web client does. Fill this in only to force a value captured from the browser. |

Options (⋮ → *Configure*): the polling interval in minutes (default 30,
minimum 10). Each poll is a handful of requests per student; keep it polite.

If Webtop rejects the credentials later (for example after a password
change), the integration asks you to **re-authenticate** from the
integrations page.

## Known limitations

- **Password login can be refused by the portal's bot protection.** The
  Webtop login page runs a Cloudflare Turnstile check and, after failed
  attempts from a network, demands a visible captcha. This integration cannot
  and will not solve or sidestep that. When the server answers
  `CaptchaRequired` the setup form says so; sign in once in a browser from
  the same network, wait, and try again. A plain "rejected the login" means
  the server gave no reason, which is also what a wrong password looks like.
  [Shanksg/smartschool-webtop](https://github.com/Shanksg/smartschool-webtop)
  reports password login blocked outright on their account and uses a
  browser-captured token instead.
- **pywebtop's built-in `Data` default is not used.** It is the value for one
  particular username. The integration computes `Data` per login from the
  username and the server's login counter (see `auth.py`).
- **The newer features are built from the web client's code, not from real
  data yet.** Tests, grades, schedule changes, the bell schedule and lesson
  times use field names read out of the Webtop web client. They are covered
  by tests on synthetic data but unconfirmed against a live account. Run the
  capture tool below to confirm or correct them.
- **Response shapes are only partly documented.** Homework, messages and
  student discovery follow payloads other projects have published. The
  timetable, behaviour-event and notification parsers are tolerant and
  best-effort. If a sensor shows `unknown` or the wrong value, download the
  integration's **diagnostics** (personal data is redacted, raw payload
  samples are included) and open an issue.
- Homework comes from the dashboard endpoint, which only returns **today's**
  lessons. The week view is available through the timetable attributes.
- The tests board is read for the current month and the next one.
- Lesson times assume Home Assistant runs in the school's timezone.
- Event detection lives in memory; a restart re-baselines and will not fire
  events for items that arrived while Home Assistant was down.
- Only the first page of the inbox is read (the most recent messages).
- Student devices are keyed by the numeric student id the portal reports,
  falling back to a digest of name and school. Webtop's *encrypted* student
  id changes every school year, so it is never used as a key.
- pywebtop logs the username at `INFO` level when the client is created.
  Set `logger: logs: webtop: warning` if that bothers you.

## Capturing real responses, anonymized

Refining the parsers needs real responses, and those are full of children's
personal data. `tools/webtop_capture.py` fetches them and writes only
anonymized copies, using a model that runs on your own machine.

```bash
uv venv --python 3.14 .venv && uv pip install --python .venv/bin/python -r requirements_test.txt
```

```bash
.venv/bin/python -m tools.webtop_capture
```

What it does:

- Asks for your username and password in the terminal. The password is not
  echoed, stored or logged.
- Logs in and calls a fixed list of read-only endpoints (see `ENDPOINTS` in
  the script) for up to two children.
- Classifies every text field with a **local** model. It looks for a running
  `llama-server` with a Qwen or Gemma model and refuses any address that is
  not loopback. Tokens, ids, names, contact details and grades are replaced by
  key-name rules that the model cannot override, and any field the model does
  not classify is redacted.
- Replaces values while keeping the exact JSON structure, key names and value
  types, then runs a final check for your account's known names and ids.
- Writes `captures/<timestamp>/*.json` and a `manifest.json` with the request
  templates, the anonymization plan and how many items the integration's
  parsers found. Raw responses only ever exist in memory.

`captures/` is git-ignored. Skim the files before sharing them. Use
`--no-llm` for a strict mode that redacts all free text, or `--llm-url` to
point at another local OpenAI-compatible server.

## Development

```bash
python3.14 -m venv .venv && . .venv/bin/activate
pip install -r requirements_test.txt
ruff check .
pytest
```

Tests fake the pywebtop client, so they run offline against the shapes in
`tests/conftest.py`.

## Prior art

| Project | Approach | Uses pywebtop? |
| --- | --- | --- |
| [Shanksg/smartschool-webtop](https://github.com/Shanksg/smartschool-webtop) | HACS integration with a vendored synchronous `requests` client. Authenticates with a browser-captured `bioLogin` credential (or pasted `webToken`) rather than a password, because of the captcha above. Homework + messages, events, per-student to-do lists. MIT. | No |
| [t0mer/pywebtop](https://github.com/t0mer/pywebtop) | The async API wrapper this integration is built on (`pip install pywebtop`, import name `webtop`). | — |
| [t0mer/pymashov](https://github.com/t0mer/pymashov), [NirBY/ha-mashov](https://github.com/NirBY/ha-mashov) | The same idea for Mashov, the other Israeli school portal. | — |

This integration is not affiliated with SmartSchool or Webtop.
