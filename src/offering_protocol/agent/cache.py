"""Agent-side representation caching."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol


@dataclass(frozen=True, slots=True)
class CacheRecord:
    body: bytes
    etag: str | None
    expires: datetime
    final_url: str
    last_modified: str | None
    status: int
    stored: datetime


class Cache(Protocol):
    def delete(self, key: str) -> None: ...

    def get(self, key: str) -> CacheRecord | None: ...

    def set(self, key: str, record: CacheRecord) -> None: ...


class MemoryCache:
    def __init__(self) -> None:
        self._records: dict[str, CacheRecord] = {}

    def delete(self, key: str) -> None:
        self._records.pop(key, None)

    def get(self, key: str) -> CacheRecord | None:
        return self._records.get(key)

    def set(self, key: str, record: CacheRecord) -> None:
        self._records[key] = record


@dataclass(frozen=True, slots=True)
class CacheFallbacks:
    collection: timedelta = timedelta(hours=1)
    offering: timedelta = timedelta(minutes=5)
    service_document: timedelta = timedelta(hours=4)


def utc_now() -> datetime:
    return datetime.now(UTC)
