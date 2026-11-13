"""Message texts (and when it is safe to fetch them) and the new-notification event."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_capture_events, async_fire_time_changed

from custom_components.webtop.const import (
    CONF_READ_UNREAD_MESSAGES,
    EVENT_NEW_MESSAGE,
    EVENT_NEW_NOTIFICATION,
    MESSAGE_BODIES_PER_POLL,
)
from custom_components.webtop.diagnostics import async_get_config_entry_diagnostics
from custom_components.webtop.models import Message, html_to_text, parse_message_body, parse_notifications

from .conftest import ENCRYPTED_ID_A, INBOX, NOTIFICATIONS, _message_body

LAST = "sensor.webtop_dnh_lvy_last_message"
UNREAD = "sensor.webtop_dnh_lvy_unread_messages"
TRIP = "שלום הורים,\nהטיול השנתי יתקיים ביום ג'.\nנא לאשר השתתפות & תשלום."
MEETING = "תזכורת: אסיפת הורים ביום חמישי."


async def _setup(hass: HomeAssistant, config_entry, **options: Any) -> None:
    config_entry.add_to_hass(hass)
    if options:
        hass.config_entries.async_update_entry(config_entry, options=options)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()


async def _advance(hass: HomeAssistant, freezer: FrozenDateTimeFactory, minutes: int = 31) -> None:
    freezer.tick(timedelta(minutes=minutes))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)


def _body_requests(fake_client) -> list[str]:
    return [c[1]["MessageId"] for c in fake_client.created[0].calls if c[0] == "GetMessagesInboxData"]


# --- parsing ----------------------------------------------------------------
@pytest.mark.parametrize(
    ("html_text", "expected"),
    [
        ("<p>שלום</p><p>עולם</p>", "שלום\nעולם"),
        ("שורה<br>שנייה<br/>שלישית", "שורה\nשנייה\nשלישית"),
        ("a &amp; b &lt;c&gt; &#39;d&#39;&nbsp;e", "a & b <c> 'd' e"),
        ("<div>  many    spaces </div>\n\n\n<div>next</div>", "many spaces\nnext"),
        ("<style>p{color:red}</style><script>alert(1)</script>text", "text"),
        ('<a href="https://example.org/x">link</a> after', "link after"),
        ("<ul><li>one</li><li>two</li></ul>", "one\ntwo"),
        ("plain text", "plain text"),
        ("", ""),
        (None, ""),
        (42, ""),
    ],
)
def test_html_to_text(html_text, expected) -> None:
    assert html_to_text(html_text) == expected


def test_parse_message_body() -> None:
    assert parse_message_body(_message_body({"MessageId": "enc-message-1"})["data"]) == TRIP
    assert parse_message_body(_message_body({"MessageId": "enc-message-2"})["data"]) == MEETING
    assert parse_message_body(None) is None
    assert parse_message_body({"messageData": None}) is None
    assert parse_message_body({"messageData": {"messageContent": None}}) is None
    assert parse_message_body({"messageData": {"messageContent": ""}}) is None  # nothing to show


def test_message_keeps_the_portal_id_but_not_in_its_public_dict() -> None:
    message = Message.from_api(INBOX["data"][0])
    assert message.message_id == "enc-message-1"
    assert "enc-message-1" not in str(message.as_dict()) and "body" not in message.as_dict()


# --- when a text may be fetched ---------------------------------------------
async def test_by_default_only_messages_already_read_are_fetched(
    hass: HomeAssistant, fake_client, config_entry
) -> None:
    """Loading a message's text marks it as read on the portal, so unread ones are left alone."""
    await _setup(hass, config_entry)
    assert _body_requests(fake_client) == ["enc-message-2"]  # the one with hasRead: 1
    request = next(c[1] for c in fake_client.created[0].calls if c[0] == "GetMessagesInboxData")
    assert request == {
        "path": "/server/api/messageBox/GetMessagesInboxData",
        "MessageId": "enc-message-2", "FilterId": 0, "IsInbox": True, "hasRead": None,
    }  # fmt: skip
    # The newest message is unread: its subject is known, its text is not.
    last = hass.states.get(LAST)
    assert last.state == "טיול שנתי" and last.attributes["body"] == ""
    assert hass.states.get(UNREAD).state == "1"


async def test_text_appears_once_the_message_has_been_read_on_the_portal(
    hass: HomeAssistant, fake_client, config_entry, freezer: FrozenDateTimeFactory
) -> None:
    await _setup(hass, config_entry)
    assert hass.states.get(LAST).attributes["body"] == ""
    read_now = [{**INBOX["data"][0], "hasRead": 1}, INBOX["data"][1]]
    fake_client.created[0].responses["get_messages_inbox"] = {"status": True, "data": read_now}
    await _advance(hass, freezer)
    assert hass.states.get(LAST).attributes["body"] == TRIP
    assert _body_requests(fake_client) == ["enc-message-2", "enc-message-1"]


async def test_opt_in_fetches_unread_messages_too(hass: HomeAssistant, fake_client, config_entry) -> None:
    await _setup(hass, config_entry, **{CONF_READ_UNREAD_MESSAGES: True})
    assert _body_requests(fake_client) == ["enc-message-1", "enc-message-2"]  # newest first
    assert hass.states.get(LAST).attributes["body"] == TRIP
    # The list of messages stays light: no texts, no portal ids.
    listed = hass.states.get(UNREAD).attributes["messages"]
    assert all("body" not in m for m in listed) and "enc-message" not in str(listed)


async def test_each_text_is_fetched_once(
    hass: HomeAssistant, fake_client, config_entry, freezer: FrozenDateTimeFactory
) -> None:
    await _setup(hass, config_entry, **{CONF_READ_UNREAD_MESSAGES: True})
    await _advance(hass, freezer)
    await _advance(hass, freezer)
    assert _body_requests(fake_client) == ["enc-message-1", "enc-message-2"]
    assert hass.states.get(LAST).attributes["body"] == TRIP


async def test_an_answer_without_text_is_not_asked_for_again_but_a_failure_is(
    hass: HomeAssistant, fake_client, config_entry, freezer: FrozenDateTimeFactory
) -> None:
    fake_client.responses["GetMessagesInboxData"] = Seqish(
        {"enc-message-1": [{"status": True, "data": {"messageData": {"somethingElse": 1}}}]}
    )
    await _setup(hass, config_entry, **{CONF_READ_UNREAD_MESSAGES: True})
    await _advance(hass, freezer)
    # Message 1 answered without text: remembered as empty. Message 2 kept failing: retried.
    assert _body_requests(fake_client).count("enc-message-1") == 1
    assert _body_requests(fake_client).count("enc-message-2") == 2
    assert hass.states.get(LAST).attributes["body"] == ""
    assert hass.states.get(UNREAD).state == "1"  # the rest of the poll is unaffected


class Seqish:
    """A payload-keyed responder: listed ids answer in turn (last one repeats), others get status false."""

    def __init__(self, answers: dict[str, list[dict[str, Any]]]) -> None:
        self.answers = answers

    def __call__(self, payload: dict[str, Any]) -> dict[str, Any]:
        queue = self.answers.get(payload["MessageId"])
        if not queue:
            return {"status": False, "errorDescription": "something went wrong", "data": None}
        return queue.pop(0) if len(queue) > 1 else queue[0]


async def test_at_most_a_few_texts_per_poll(
    hass: HomeAssistant, fake_client, config_entry, freezer: FrozenDateTimeFactory
) -> None:
    many = [
        {
            **INBOX["data"][1],
            "subject": f"הודעה {n}",
            "messageId": f"old-{n}",
            "sendingDate": f"2026-10-{n:02d}T08:00:00",
        }
        for n in range(1, 9)
    ]
    fake_client.responses["get_messages_inbox"] = {"status": True, "data": many}
    fake_client.responses["GetMessagesInboxData"] = lambda payload: {
        "status": True,
        "data": {"messageData": {"messageContent": payload["MessageId"]}},
    }
    await _setup(hass, config_entry)
    assert _body_requests(fake_client) == [f"old-{n}" for n in range(8, 8 - MESSAGE_BODIES_PER_POLL, -1)]
    await _advance(hass, freezer)
    assert len(_body_requests(fake_client)) == 8  # the rest on the next poll
    assert hass.states.get(LAST).attributes["body"] == "old-8"


# --- events -----------------------------------------------------------------
async def test_new_message_event_carries_the_text_when_it_may_be_fetched(
    hass: HomeAssistant, fake_client, config_entry, freezer: FrozenDateTimeFactory
) -> None:
    events = async_capture_events(hass, EVENT_NEW_MESSAGE)
    await _setup(hass, config_entry, **{CONF_READ_UNREAD_MESSAGES: True})
    assert events == []  # the first poll is only a baseline

    new = {"subject": "שינוי במערכת", "sendingDate": "2026-11-10T20:30:00", "student_F_name": "רחל",
           "student_L_name": "כהן", "hasRead": 0, "filesWereAttached": 0, "messageId": "enc-message-3"}  # fmt: skip
    client = fake_client.created[0]
    client.responses["get_messages_inbox"] = {"status": True, "data": [new, *INBOX["data"]]}
    client.responses["GetMessagesInboxData"] = lambda payload: (
        {"status": True, "data": {"messageData": {"messageContent": "<p>מחר אין שיעור ראשון.</p>"}}}
        if payload["MessageId"] == "enc-message-3"
        else {"status": True, "data": None}
    )
    await _advance(hass, freezer)
    assert len(events) == 1
    assert events[0].data["subject"] == "שינוי במערכת" and events[0].data["sender"] == "כהן רחל"
    assert events[0].data["body"] == "מחר אין שיעור ראשון."


async def test_new_message_event_has_no_text_by_default(
    hass: HomeAssistant, fake_client, config_entry, freezer: FrozenDateTimeFactory
) -> None:
    events = async_capture_events(hass, EVENT_NEW_MESSAGE)
    await _setup(hass, config_entry)
    new = {"subject": "שינוי במערכת", "sendingDate": "2026-11-10T20:30:00", "hasRead": 0, "messageId": "enc-message-3"}
    fake_client.created[0].responses["get_messages_inbox"] = {"status": True, "data": [new, *INBOX["data"]]}
    await _advance(hass, freezer)
    assert [(e.data["subject"], e.data["body"]) for e in events] == [("שינוי במערכת", "")]
    assert "enc-message-3" not in _body_requests(fake_client)  # still unread, so untouched


async def test_new_notification_event(
    hass: HomeAssistant, fake_client, config_entry, freezer: FrozenDateTimeFactory
) -> None:
    events = async_capture_events(hass, EVENT_NEW_NOTIFICATION)
    await _setup(hass, config_entry)
    assert events == []  # baseline

    fresh = {"moduleName": "pupilGrades", "itemId": "enc-item-3", "date": "2026-11-10T21:00:00",
             "message": "הוזן ציון חדש במדעים", "studentID": ENCRYPTED_ID_A}  # fmt: skip
    earlier = NOTIFICATIONS["data"]["personalNotifications"]
    preview = {**NOTIFICATIONS["data"], "personalNotifications": [fresh, *earlier]}
    fake_client.created[0].responses["get_preview_unread_notifications"] = {"status": True, "data": preview}
    await _advance(hass, freezer)
    assert len(events) == 1
    assert events[0].data == {
        "entry_id": config_entry.entry_id,
        "date": "2026-11-10T21:00:00",
        "message": "הוזן ציון חדש במדעים",
        "module": "pupilGrades",
        "kind": "personal",
        "student": "נועם לוי",  # resolved from the notification's student id
    }
    await _advance(hass, freezer)
    assert len(events) == 1  # not repeated

    latest = hass.states.get("sensor.webtop_dnh_lvy_unread_notifications").attributes["latest"]
    assert latest[0]["student"] == "נועם לוי" and "enc-" not in str(latest)


def test_notification_identity() -> None:
    first, second = parse_notifications(NOTIFICATIONS["data"])
    assert first.identity() != second.identity()
    assert first.student_id == ENCRYPTED_ID_A and first.as_dict()["student"] == ""  # unresolved until matched


async def test_diagnostics_never_contain_message_text(hass: HomeAssistant, fake_client, config_entry) -> None:
    await _setup(hass, config_entry, **{CONF_READ_UNREAD_MESSAGES: True})
    dumped = str(await async_get_config_entry_diagnostics(hass, config_entry))
    for personal in ("הטיול השנתי", "אסיפת הורים", "enc-message-1", "נא לאשר"):
        assert personal not in dumped, personal
