from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone


class SessionNotFoundError(LookupError):
    pass


class SessionVersionConflict(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SessionCursor:
    updated_at: datetime
    run_id: str

    def encode(self) -> str:
        payload = json.dumps([self.updated_at.isoformat(), self.run_id], separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")

    @classmethod
    def decode(cls, value: str | None) -> SessionCursor | None:
        if value is None:
            return None
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,512}", value):
            raise ValueError("invalid session cursor")
        try:
            decoded = json.loads(base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True))
            if not isinstance(decoded, list) or len(decoded) != 2:
                raise ValueError("invalid session cursor")
            timestamp, run_id = decoded
            if not isinstance(timestamp, str) or not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", run_id):
                raise ValueError("invalid session cursor")
            updated_at = datetime.fromisoformat(timestamp)
            if updated_at.tzinfo is None or updated_at.utcoffset() is None:
                raise ValueError("invalid session cursor")
            return cls(updated_at.astimezone(timezone.utc), run_id)
        except (ValueError, TypeError, binascii.Error, UnicodeError) as error:
            raise ValueError("invalid session cursor") from error


@dataclass(frozen=True, slots=True)
class SessionRecord:
    owner_id: str
    run_id: str
    title: str
    preview: str
    status: str
    version: int
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None

    def view(self) -> dict:
        return {
            "runId": self.run_id,
            "title": self.title,
            "preview": self.preview,
            "status": self.status,
            "version": self.version,
            "createdAt": self.created_at.isoformat(),
            "updatedAt": self.updated_at.isoformat(),
            "deleted": self.deleted_at is not None,
        }


@dataclass(frozen=True, slots=True)
class SessionPage:
    sessions: tuple[SessionRecord, ...]
    next_cursor: SessionCursor | None

    def view(self) -> dict:
        return {"sessions": [record.view() for record in self.sessions],
                "nextCursor": self.next_cursor.encode() if self.next_cursor else None}


def session_page(records: list[SessionRecord], limit: int) -> SessionPage:
    has_more = len(records) > limit
    page = tuple(records[:limit])
    cursor = SessionCursor(page[-1].updated_at, page[-1].run_id) if has_more else None
    return SessionPage(page, cursor)


def session_title(value: str) -> str:
    title = " ".join(value.split())
    if not 1 <= len(title) <= 80:
        raise ValueError("session title must be 1 to 80 characters")
    return title
