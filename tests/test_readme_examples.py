"""The Markdown card examples in the README must render against the real entities."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.template import Template

README = Path(__file__).parent.parent / "README.md"


def markdown_cards() -> dict[str, str]:
    """Card title (or first line) -> template, for every Markdown card in the README."""
    cards: dict[str, str] = {}
    for block in re.findall(r"```yaml\n(.*?)```", README.read_text(encoding="utf-8"), re.S):
        card = yaml.safe_load(block)
        if isinstance(card, dict) and card.get("type") == "markdown":
            cards[card.get("title") or card["content"].splitlines()[0]] = card["content"]
    return cards


def test_readme_has_the_documented_cards() -> None:
    assert set(markdown_cards()) >= {"Lessons today", "Homework this week", "Upcoming tests"}
    assert len(markdown_cards()) == 4


@pytest.fixture
async def rendered(
    hass: HomeAssistant, fake_client, config_entry, freezer: FrozenDateTimeFactory
) -> dict[str, str]:
    await hass.config.async_set_time_zone("Asia/Jerusalem")
    freezer.move_to("2026-11-10 08:10:00+02:00")  # Tuesday, during the first lesson
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    out = {}
    for title, content in markdown_cards().items():
        template = Template(content.replace("sensor.CHILD_", "sensor.nv_m_lvy_"), hass)
        out[title] = template.async_render()
    return out


async def test_lessons_card(rendered: dict[str, str]) -> None:
    text = rendered["Lessons today"]
    assert "**1. מתמטיקה** · רחל · חדר 12" in text
    assert "_שברים_" in text and "Homework: עמוד 45 תרגילים 1-5" in text
    assert "**2. אנגלית** · דוד · מעבדה 3" in text


async def test_homework_card(rendered: dict[str, str]) -> None:
    text = rendered["Homework this week"]
    assert "- **2026-11-09 · תורה**: לקרוא פרק ג" in text
    assert "- **2026-11-10 · מתמטיקה**: עמוד 45 תרגילים 1-5" in text
    assert "No homework this week." not in text


async def test_current_lesson_card(rendered: dict[str, str]) -> None:
    text = next(v for k, v in rendered.items() if "current_lesson" in k)
    assert "## מתמטיקה" in text and "רחל" in text and "חדר 12" in text
    assert "_שברים_" in text
    assert "Next: **אנגלית** at 08:45" in text


async def test_tests_card(rendered: dict[str, str]) -> None:
    text = rendered["Upcoming tests"]
    assert "- **2026-11-12** 08:00 · בוחן: אנגלית - אוצר מילים" in text
    assert "- **2026-11-18** 08:45 · מבחן: היסטוריה - פרקים 4-6" in text


async def test_cards_render_for_a_student_with_nothing(
    hass: HomeAssistant, fake_client, config_entry
) -> None:
    """Empty lists and unknown states must not break the templates."""
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    for title, content in markdown_cards().items():
        text = Template(content.replace("sensor.CHILD_", "sensor.myh_lvy_"), hass).async_render()
        assert isinstance(text, str), title
    homework = Template(markdown_cards()["Homework this week"].replace("CHILD", "myh_lvy"), hass).async_render()
    assert "No homework this week." in homework
