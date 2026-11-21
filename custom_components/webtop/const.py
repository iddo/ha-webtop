"""Constants for the Webtop (SmartSchool) integration."""

from __future__ import annotations

DOMAIN = "webtop"

# pywebtop's import name is `webtop`; the integration package is
# `custom_components.webtop`, so the two never collide.
LIBRARY_LOGGER = "webtop"

# Config-entry data keys.
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
# Optional override for the "Data" field of the login request. Left empty, the
# integration computes it from the username (see auth.py).
CONF_DATA = "data"
DEFAULT_DATA = ""
# pywebtop's built-in default, which earlier versions of this integration
# stored. It is only valid for one username, so it is treated as "not set".
LEGACY_DEFAULT_DATA = "+Aabe7FAdVluG6Lu+0ibrA=="

# How the integration identifies itself to the portal (the HTTP User-Agent). By default it says
# what it is. The user may set another value in the setup form or the options.
CONF_USER_AGENT = "user_agent"
INTEGRATION_VERSION = "0.8.0"
DEFAULT_USER_AGENT = (
    f"ha-webtop/{INTEGRATION_VERSION} (Home Assistant integration; +https://github.com/iddo/ha-webtop)"
)
# After the portal's firewall refuses a request, wait this long before the next attempt (seconds),
# longer each time in a row. Hammering a firewall only makes a block last.
BLOCKED_BACKOFF_SECONDS = (3600, 3 * 3600, 6 * 3600)

# Options.
CONF_SCAN_INTERVAL = "scan_interval"  # minutes
# An hour is enough: between polls the portal's own notifications decide what is re-read,
# and the current lesson runs on a timer of its own.
DEFAULT_SCAN_INTERVAL = 60
MIN_SCAN_INTERVAL = 10
# Lesson times typed by the user, for schools that do not publish them:
# "08:00-08:45, 08:45-09:30, ..." in lesson order, or "1=08:00-08:45" per lesson.
CONF_LESSON_TIMES = "lesson_times"
# The same, per student (keyed by student key). These win over the portal's times; the
# account-wide value above only fills in for students whose school publishes none.
CONF_STUDENT_LESSON_TIMES = "student_lesson_times"
# On the portal, loading a message's text is what marks it as read. The text of messages the
# user has already read is fetched freely; unread ones only when this option is on.
CONF_READ_UNREAD_MESSAGES = "read_unread_messages"
DEFAULT_READ_UNREAD_MESSAGES = False
# Message texts fetched per school per poll, and kept in memory overall.
MESSAGE_BODIES_PER_POLL = 5
MESSAGE_BODY_CACHE_SIZE = 200
MESSAGE_BODY_ATTR_CHARS = 2000
MESSAGE_BODY_EVENT_CHARS = 4000

# Do not contact the portal at night; nothing changes there and it halves the load.
CONF_NIGHT_PAUSE = "night_pause"
DEFAULT_NIGHT_PAUSE = True
NIGHT_PAUSE_START_HOUR = 23  # inclusive, Home Assistant local time
NIGHT_PAUSE_END_HOUR = 6  # exclusive
# Each poll is shifted by up to this fraction of the interval, so polls do not land on the clock.
POLL_JITTER = 0.1
# Last week's lessons change rarely (a teacher filling in late); re-read them this often.
PREVIOUS_WEEK_REFRESH_HOURS = 3

# How long each view is trusted before it is read again, in hours. Every poll reads only the
# portal's notification list and unread badges (and today's changes); those cut these waits
# short the moment they show something new. The Refresh button reads everything.
VIEW_TTL_HOURS = {
    "linked": 24,  # the logins linked to the account
    "dashboard": 12,  # the children on a login and what the school allows
    "board": 6,  # exams, events, vacations
    "week": 2,  # this week's lessons, topics and homework
    "behaviour": 3,
    "grades": 6,
    "inbox": 2,
}
# What a notification announces, by its module name (lower case). An unknown module
# refreshes everything about that student.
NOTIFICATION_VIEWS = {
    "lessonsubjecthomework": ("week",),
    "lessonevents": ("behaviour",),
    "disciplineevents": ("behaviour",),
    "pupilgrades": ("grades",),
    "grades": ("grades",),
}
STUDENT_VIEWS = ("week", "behaviour", "grades")

# Every pupil-card view has its own module id (the number in its URL on the portal), and a
# school grants or blocks each one separately.
MODULE_BEHAVIOUR = 4
MODULE_GRADES = 6
MODULE_TIMETABLE = 10
MODULE_WEEK = 11
MAX_SCAN_INTERVAL = 720

REQUEST_TIMEOUT = 30.0

WEBTOP_PORTAL_URL = "https://webtop.smartschool.co.il"
MANUFACTURER = "SmartSchool"

# Events fired on the Home Assistant bus when something new shows up after
# the first successful poll of a running instance.
EVENT_NEW_HOMEWORK = f"{DOMAIN}_new_homework"
EVENT_NEW_MESSAGE = f"{DOMAIN}_new_message"
EVENT_NEW_GRADE = f"{DOMAIN}_new_grade"
EVENT_NEW_NOTIFICATION = f"{DOMAIN}_new_notification"
EVENT_NEW_BEHAVIOUR = f"{DOMAIN}_new_behaviour_event"
EVENT_NEW_TEST = f"{DOMAIN}_new_test"

# How far ahead the tests board is read, in months (the current month is always included).
TESTS_BOARD_MONTHS_AHEAD = 1

# Sensor attributes hold lists; keep them bounded so the recorder database
# does not balloon.
MAX_LIST_ATTR_ITEMS = 25
