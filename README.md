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
| Homework today | student | number of assignments due today | `items` (subject, teacher, hour, homework text, status), `text` |
| Behaviour events | student | number of discipline records | `events` (date, event, subject, teacher, remark, justified) |
| Lessons today | student | number of timetable slots today | `today`, `week` (date, hour, subject, teacher, room, changed) |
| Class | student (diagnostic) | class name, e.g. `ג'2` | school, class code/number, study year |
| Unread messages | account | unread count in the inbox | `total`, `messages` (subject, sender, sent_at, read/attachment flags, student) |
| Last message | account | subject of the newest message | sender, sent_at, flags |
| Unread notifications | account | unread portal notifications | |

Two events are fired on the Home Assistant bus once a running instance sees
something it had not seen before (the first poll after a restart only records
a baseline, so nothing is replayed):

- `webtop_new_homework` with `student`, `student_key`, `subject`, `teacher`, `homework`, `hour`, `date`, `status`, `entry_id`
- `webtop_new_message` with `subject`, `sender`, `sent_at`, `has_read`, `has_files`, `student`, `entry_id`

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
| Login data token | The opaque `Data` value the Webtop web client sends on login. pywebtop ships a default that works for SmartSchool Israel; change it only if logins fail. |

Options (⋮ → *Configure*): the polling interval in minutes (default 30,
minimum 10). Each poll is a handful of requests per student; keep it polite.

If Webtop rejects the credentials later (for example after a password
change), the integration asks you to **re-authenticate** from the
integrations page.

## Known limitations

- **Password login may be captcha-gated for some schools.** Another
  project ([Shanksg/smartschool-webtop](https://github.com/Shanksg/smartschool-webtop))
  reports that `LoginByUserNameAndPassword` is protected by a reCAPTCHA on
  their account and only works from a browser. pywebtop's own examples log
  in with username, password and the `Data` value, so it works for at least
  some accounts. If setup fails with "rejected the username or password"
  while the portal itself shows a captcha on login, this integration will not
  be able to sign in for that school.
- **Response shapes are only partly documented.** Homework, messages and
  student discovery follow payloads other projects have published. The
  timetable, behaviour-event and notification parsers are tolerant and
  best-effort. If a sensor shows `unknown` or the wrong value, download the
  integration's **diagnostics** (personal data is redacted, raw payload
  samples are included) and open an issue.
- Homework comes from the dashboard endpoint, which only returns **today's**
  lessons. The week view is available through the timetable attributes.
- Event detection lives in memory; a restart re-baselines and will not fire
  events for items that arrived while Home Assistant was down.
- Only the first page of the inbox is read (the most recent messages).
- Student devices are keyed by the numeric student id the portal reports,
  falling back to a digest of name and school. Webtop's *encrypted* student
  id changes every school year, so it is never used as a key.
- pywebtop logs the username at `INFO` level when the client is created.
  Set `logger: logs: webtop: warning` if that bothers you.

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
