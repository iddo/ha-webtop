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
| Current lesson | student | the subject being taught right now, `Break` between lessons, `No lesson` outside school hours | teacher, topic, room, hour, start, end, changed, next_subject, next_start, lessons_today, times_known |
| School day ends | student | when today's last lesson ends (a timestamp, for pickup automations) | starts, lessons |
| Next test | student | date of the next exam, quiz or assignment set for the student | summary, type, title, subject, start, end, from_hour, to_hour, room, note, days_until |
| Upcoming tests | student | number of exams from today onward | `tests` list |
| Latest grade | student | the most recent mark | subject, title, type, date, weight, teacher, remark, `recent` list (the school year) |
| Schedule changes today | student | substitutions, cancellations, exams and notices on today's board | `changes` (kind, text, hour, teacher, room, note) |
| Homework today | student | homework written on today's lessons | `items` (date, hour, hours, subject, teacher, homework), `text`, `week`, `week_count`, `last_week` |
| Behaviour events | student | behaviour records from the last 7 days, commendations included | `today`, `total` (school year), `by_type`, `events` (date, hour, event, subject, teacher, remark, justified) |
| Lessons today | student | number of lessons today | `today`, `week` (date, hour, subject, teacher, topic, room, start, end) |
| Class | student (diagnostic) | class, e.g. `ג'2` | school, class code/number, study year |
| School calendar | student (calendar) | exams, school events and vacations for the student | standard calendar events; timed when lesson numbers are known, with the room as location |
| Timetable | student (calendar) | the lessons themselves: what was taught last week and this week, and the standing timetable for other weeks | events with the subject as title, teacher, topic and homework as description, room as location |
| Homework | student (to-do list) | number of homework items not yet ticked | items from last week and this week; ticking one is remembered in Home Assistant |
| Unread messages | account | the portal's unread-messages badge | `total`, `messages` (subject, sender, sent_at, read/attachment flags, student) |
| Last message | account | subject of the newest message | sender, sent_at, flags, student, `body` (its text, once it may be fetched) |
| Unread notifications | account | the portal's unread-notifications badge | `latest` (date, message, module, kind, student) |
| Refresh | account (button) | polls the portal now, also during the night pause | |

A double lesson with the same homework written on both hours is one homework
item listing both hours. `last_week` keeps the previous week's homework in
view, since what is given on Thursday is usually due on Sunday.

### Where lessons, homework and topics appear

Three places, depending on how you like to look at them:

- **Calendar.** Each student's **Timetable** calendar shows lessons in Home
  Assistant's calendar view. Last week and this week show what was actually
  taught, with the teacher, the lesson topic and any homework in the event
  description and the room as its location. Other weeks are drawn from the
  standing timetable, without known vacations. A lesson appears only when its
  time is known, so a school that publishes no lesson times needs them typed
  in first (see below).
- **To-do list.** Each student's **Homework** list holds last week's and this
  week's homework in the To-do view. Ticking an item is remembered in Home
  Assistant; the portal itself has no notion of "done". Items cannot be
  added, renamed or deleted, because the list mirrors the portal.
- **Sensor attributes.** The same data, for templates and automations:

| What | Sensor | Attribute |
| --- | --- | --- |
| Today's lessons, with topic and room | Lessons today | `today` |
| The whole week's lessons | Lessons today | `week` |
| Homework from today's lessons | Homework today | `items`, `text` |
| This week's homework | Homework today | `week` |
| Last week's homework | Homework today | `last_week` |
| The lesson happening now, with its topic | Current lesson | `topic`, `teacher`, `room` |

Each lesson entry carries `date`, `hour`, `subject`, `teacher`, `topic`,
`room` and `homework`. A topic appears only after the teacher has reported
the lesson, so future lessons have none. To inspect the attributes, open the
student's device under Settings → Devices & services → Webtop, or filter by
the student's name in Developer tools → States.

#### Markdown cards

A Markdown card renders these lists on a dashboard. Replace `CHILD` with the
prefix of your entity ids, which you can copy from the student's device page.

Today's lessons, with topic and homework:

```yaml
type: markdown
title: Lessons today
content: |
  {% for l in state_attr('sensor.CHILD_lessons_today', 'today') or [] %}
  **{{ l.hour }}. {{ l.subject }}** · {{ l.teacher }}{% if l.room %} · {{ l.room }}{% endif %}
  {% if l.topic %}_{{ l.topic }}_{% endif %}
  {% if l.homework %}Homework: {{ l.homework }}{% endif %}

  {% endfor %}
```

This week's homework:

```yaml
type: markdown
title: Homework this week
content: |
  {% for h in state_attr('sensor.CHILD_homework_today', 'week') or [] %}
  - **{{ h.date }} · {{ h.subject }}**: {{ h.homework }}
  {% else %}
  No homework this week.
  {% endfor %}
```

What is on now and what comes next:

```yaml
type: markdown
content: |
  {% set s = 'sensor.CHILD_current_lesson' %}
  ## {{ states(s) }}
  {% if state_attr(s, 'teacher') %}{{ state_attr(s, 'teacher') }}{% endif %}
  {% if state_attr(s, 'room') %} · {{ state_attr(s, 'room') }}{% endif %}
  {% if state_attr(s, 'topic') %}

  _{{ state_attr(s, 'topic') }}_
  {% endif %}
  {% if state_attr(s, 'next_subject') %}

  Next: **{{ state_attr(s, 'next_subject') }}** at {{ as_timestamp(state_attr(s, 'next_start')) | timestamp_custom('%H:%M') }}
  {% endif %}
```

Upcoming tests:

```yaml
type: markdown
title: Upcoming tests
content: |
  {% for t in state_attr('sensor.CHILD_upcoming_tests', 'tests') or [] %}
  - **{{ t.date }}**{% if t.start %} {{ t.start }}{% endif %} · {{ t.summary }}
  {% else %}
  No tests coming up.
  {% endfor %}
```

The built-in **Calendar** and **To-do list** cards work with the Timetable
calendar and the Homework list directly, with no template.

### Messages and notifications

These belong to the account, not to a student, so they sit on the account
device (named "Webtop" and your name). Messages from every linked school are
merged there.

- **Unread messages** and **Unread notifications** carry the portal's own
  badges, with the newest messages and notifications as attributes.
- **Last message** has the newest message's subject as its state and its
  text in the `body` attribute.
- `webtop_new_message` and `webtop_new_notification` fire when something
  appears that was not there on an earlier poll.

**Reading a message's text marks it as read.** On the portal, loading the
text is what marks a message as read; there is no separate step. So by
default the integration fetches the text only of messages you have already
read on the portal. Until then a message shows its subject, sender and date,
and its event carries an empty `body`.

Switch on **Read unread messages** in Configure to fetch unread messages too.
Their text then reaches Home Assistant, and a notification automation, at
once. The cost is the same as opening them yourself: the portal's unread
badge clears and the school may see them as read.

An inbox card (replace `ACCOUNT` with the prefix of the account sensors):

```yaml
type: markdown
title: School messages
content: |
  {% for m in state_attr('sensor.ACCOUNT_unread_messages', 'messages') or [] %}
  - {{ m.sent_at[:10] }} · {{ m.sender }}: {{ m.subject }}{% if m.has_files %} 📎{% endif %}{% if not m.has_read %} (unread){% endif %}
  {% else %}
  No messages.
  {% endfor %}
```

A phone notification for every new message:

```yaml
automation:
  - alias: School message
    triggers:
      - trigger: event
        event_type: webtop_new_message
    actions:
      - action: notify.mobile_app_phone
        data:
          title: "{{ trigger.event.data.sender }}: {{ trigger.event.data.subject }}"
          message: "{{ trigger.event.data.body or 'Open the portal to read it.' }}"
```

### Current lesson and history

The **Current lesson** sensor changes state at the exact minute a lesson
starts or ends, using the timetable and the school's bell schedule. It runs
on its own timer, so it does not wait for the next poll. Open the entity's
history (or add a History graph card) to see the day as a strip of subjects.
Its icon follows the subject: a calculator for maths, a flask for chemistry,
a cup during a break.
Lesson times come from the portal when the school publishes them: on its
tests board, on the labels of its timetable or of the week's lessons, or on
its weekly schedule. Some schools publish none. The state is then `unknown`
and the `times_known` attribute is `false`. In that case open the
integration's **Configure** dialog and type the lesson times, in lesson order:

```text
08:00-08:45, 08:45-09:30, 09:50-10:35, 10:35-11:20, 11:40-12:25, 12:25-13:10
```

Prefix a lesson number to start elsewhere (`0=07:15-08:00`). The dialog's
first page takes a default for the account. It only fills in for students
whose school publishes no times; it never replaces a school's own. With
several students, a page per student follows, since siblings in different
schools rarely share a bell schedule. Times typed for a student win over the
portal's for that student.

### Events

Events are fired on the Home Assistant bus once a running instance sees
something it had not seen before. The first poll after a restart only records
a baseline, so nothing is replayed.

| Event | Data |
| --- | --- |
| `webtop_new_homework` | `student`, `student_key`, `subject`, `teacher`, `homework`, `hour`, `date`, `status`, `entry_id` |
| `webtop_new_behaviour_event` | `student`, `student_key`, `event`, `subject`, `teacher`, `date`, `hour`, `remark`, `justified`, `entry_id` |
| `webtop_new_grade` | `student`, `student_key`, `subject`, `grade`, `title`, `type`, `date`, `weight`, `teacher`, `remark`, `entry_id` |
| `webtop_new_test` | `student`, `student_key`, `title`, `subject`, `date`, `start`, `end`, `from_hour`, `to_hour`, `note`, `entry_id` |
| `webtop_new_message` | `subject`, `sender`, `sent_at`, `has_read`, `has_files`, `student`, `body`, `entry_id` |
| `webtop_new_notification` | `date`, `message`, `module`, `kind`, `student`, `entry_id` |

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

Options (⋮ → *Configure*):

- **Polling interval** in minutes (default 30, minimum 10). Each poll is about
  ten requests per student, and each one is shifted by up to 10% so polls do
  not land on the clock.
- **Pause at night** (on by default): the portal is not contacted between
  23:00 and 06:00, Home Assistant time. The **Refresh** button still works.
- **Read unread messages** (off by default): see *Messages and
  notifications* for what it changes on the portal.
- **Lesson times**, optional, for the account and per student (see *Current
  lesson and history*).

A parent account that links several logins, one per school, is read school
by school: the integration switches the session the way the portal's own
school picker does.

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
- **What has been checked against a real account, and what has not.**
  Captures from one parent account with children in three schools confirmed
  login, switching between linked schools, student discovery, the dated week
  of lessons with homework and topics, the standing timetable with rooms,
  lesson times, behaviour events, grades, exams on the tests board, the inbox,
  notifications and menu badges. Those captures contained **no schedule
  changes, school events or vacations**, so those rows still follow the web
  client's code only.
- **Tests are matched to the student by study group.** An exam for an
  elective lists every class its pupils come from, so the integration shows
  it only if the student has a lesson in that study group. When the student's
  groups are unknown it falls back to the class list. A school without a
  tests board still shows tests when the pupil card lists evaluations that
  have been set but not yet marked.
- **Each pupil-card view is requested under its own module id** (timetable,
  lessons and homework, grades, behaviour), because schools grant them one by
  one. A capture confirmed it: a school that blocks the timetable refused
  every view sent under the timetable's id, and answered lessons, homework,
  grades and behaviour under their own. That school still has no timetable
  view, so its lessons carry no rooms.
- **A view the school has blocked is asked for once a day,** not every poll.
- **If switching to one of several linked schools fails,** that school keeps
  its last known values until the next successful poll.
- **A feature the school has switched off shows `unknown`,** not zero. The
  portal says which widgets are allowed, and the integration does not request
  the others.
- Homework is what teachers wrote on this week's lessons, each item dated by
  its lesson. If that view is blocked, the integration falls back to the
  dashboard's today-only list and the standing weekday timetable.
- The tests board is read for the current month and the next one.
- Lesson times assume Home Assistant runs in the school's timezone.
- Event detection lives in memory; a restart re-baselines and will not fire
  events for items that arrived while Home Assistant was down.
- Only the first page of the inbox is read (the most recent messages), and
  at most five message texts are fetched per school per poll. The shape of a
  message's text response follows the web client's code; it was not captured,
  because capturing it would have marked real messages as read.
- The notification list is the portal's preview: about five per school.
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
  the script) for up to two children per school. An account that links
  several logins is captured school by school; `--current-only` stays on the
  first.
- Classifies every text field with a **local** model. It looks for a running
  `llama-server` with a Qwen or Gemma model and refuses any address that is
  not loopback. Tokens, ids, names, contact details, grades and the text of
  messages and notifications are replaced by rules that the model cannot
  override, and any field the model does not classify is redacted.
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
| [dmatik/school](https://github.com/dmatik/school) | Self-hosted family web app with its own database. Syncs homework, lesson topics and behaviour from Webtop. Several ideas here come from it: merging double lessons, keeping earlier weeks of homework, pausing sync at night with jittered timing, a sync button, subject icons, and its use of a separate module id for the lessons view. | No |
| [t0mer/pywebtop](https://github.com/t0mer/pywebtop) | The async API wrapper this integration is built on (`pip install pywebtop`, import name `webtop`). | — |
| [t0mer/pymashov](https://github.com/t0mer/pymashov), [NirBY/ha-mashov](https://github.com/NirBY/ha-mashov) | The same idea for Mashov, the other Israeli school portal. | — |

This integration is not affiliated with SmartSchool or Webtop.
