"""Formal local persistence for non-sensitive App settings."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ..settings.settings_models import SettingsSnapshot, deserialize_settings, serialize_settings
from . import _LocalDocumentStore


class SettingsStore(ABC):
    @abstractmethod
    def load(self, principal_id: str) -> SettingsSnapshot:
        raise NotImplementedError

    @abstractmethod
    def save(self, principal_id: str, settings: SettingsSnapshot) -> None:
        raise NotImplementedError


class LocalSettingsStore(SettingsStore):
    """One per-user settings document backed by the App-owned state store."""

    def __init__(self, state: _LocalDocumentStore) -> None:
        self._state = state

    def load(self, principal_id: str) -> SettingsSnapshot:
        raw = self._state.read("settings").get(principal_id)
        if not isinstance(raw, dict):
            raise KeyError(principal_id)
        return deserialize_settings(raw)

    def save(self, principal_id: str, settings: SettingsSnapshot) -> None:
        def update(value: dict[str, Any]) -> dict[str, Any]:
            value[principal_id] = serialize_settings(settings)
            return value

        self._state.update("settings", update)
