"""To-do platform: each student's homework as a list that can be ticked off.

The items come from the portal (last week's and this week's homework). The portal has no
notion of "done", so that part is kept here: ticking an item is remembered in Home
Assistant's storage and survives restarts. Items cannot be added, renamed or deleted,
because the list mirrors the portal.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.todo import TodoItem, TodoItemStatus, TodoListEntity, TodoListEntityFeature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.storage import Store

from .const import DOMAIN
from .coordinator import WebtopConfigEntry, WebtopCoordinator
from .entity import WebtopStudentEntity
from .models import HomeworkItem, Student

STORAGE_VERSION = 1
SAVE_DELAY_SECONDS = 2


def homework_done_store(hass: HomeAssistant, entry_id: str) -> Store[dict[str, list[str]]]:
    """Where "this homework is done" is remembered, per config entry."""
    return Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}.homework_done")


class HomeworkDone:
    """Which homework items have been ticked, per student."""

    def __init__(self, store: Store[dict[str, list[str]]]) -> None:
        self._store = store
        self._done: dict[str, set[str]] = {}

    async def async_load(self) -> None:
        stored = await self._store.async_load()
        if isinstance(stored, dict):
            self._done = {key: set(uids) for key, uids in stored.items() if isinstance(uids, list)}

    def is_done(self, student_key: str, uid: str) -> bool:
        return uid in self._done.get(student_key, ())

    @callback
    def set(self, student_key: str, uid: str, done: bool) -> None:
        marked = self._done.setdefault(student_key, set())
        if done == (uid in marked):
            return
        marked.add(uid) if done else marked.discard(uid)
        self._save()

    @callback
    def prune(self, student_key: str, current: set[str]) -> None:
        """Forget ticks for homework the portal no longer lists (older than last week)."""
        marked = self._done.get(student_key)
        if marked and not marked <= current:
            marked &= current
            self._save()

    def _save(self) -> None:
        self._store.async_delay_save(self._as_dict, SAVE_DELAY_SECONDS)

    def _as_dict(self) -> dict[str, Any]:
        return {key: sorted(uids) for key, uids in self._done.items() if uids}


def homework_summary(item: HomeworkItem) -> str:
    return f"{item.subject}: {item.homework}"[:255]


def homework_description(item: HomeworkItem) -> str:
    """When and by whom the homework was given: "2026-11-10 · lessons 1, 2 · teacher"."""
    hours = item.hours or ([item.hour] if item.hour is not None else [])
    parts = [item.date[:10]]
    if hours:
        parts.append(("lesson " if len(hours) == 1 else "lessons ") + ", ".join(str(h) for h in hours))
    if item.teacher:
        parts.append(item.teacher)
    return " \u00b7 ".join(part for part in parts if part)


class WebtopHomeworkTodo(WebtopStudentEntity, TodoListEntity):
    """Homework for one student. The state is the number of items not yet ticked."""

    _attr_translation_key = "homework"
    _attr_icon = "mdi:notebook-check"
    _attr_supported_features = TodoListEntityFeature.UPDATE_TODO_ITEM

    def __init__(self, coordinator: WebtopCoordinator, student: Student, done: HomeworkDone) -> None:
        super().__init__(coordinator, student)
        self._done = done
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{student.key}_homework_todo"

    def _homework(self) -> list[HomeworkItem]:
        return self.coordinator.data.homework.get(self.student_key, [])

    @property
    def available(self) -> bool:
        return super().available and bool(self.coordinator.data.homework_ok.get(self.student_key))

    @property
    def todo_items(self) -> list[TodoItem]:
        """Newest homework first."""
        return [
            TodoItem(
                uid=item.identity(),
                summary=homework_summary(item),
                description=homework_description(item),
                status=(
                    TodoItemStatus.COMPLETED
                    if self._done.is_done(self.student_key, item.identity())
                    else TodoItemStatus.NEEDS_ACTION
                ),
            )
            for item in sorted(self._homework(), key=lambda h: (h.date, h.hour or 0), reverse=True)
        ]

    async def async_update_todo_item(self, item: TodoItem) -> None:
        """Only the tick is ours to change; the text belongs to the portal."""
        if item.uid is not None:
            self._done.set(self.student_key, item.uid, item.status == TodoItemStatus.COMPLETED)
        self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        if self.coordinator.data.homework_ok.get(self.student_key):
            self._done.prune(self.student_key, {item.identity() for item in self._homework()})
        super()._handle_coordinator_update()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WebtopConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create one homework list per student, including students that appear later."""
    coordinator = entry.runtime_data
    done = HomeworkDone(homework_done_store(hass, entry.entry_id))
    await done.async_load()
    known: set[str] = set()

    @callback
    def _add_new_students() -> None:
        new = [s for s in coordinator.data.students if s.key not in known]
        if new:
            known.update(s.key for s in new)
            async_add_entities(WebtopHomeworkTodo(coordinator, student, done) for student in new)

    _add_new_students()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_students))
