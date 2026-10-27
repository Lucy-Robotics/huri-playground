import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping


class UnknownSession(KeyError):
    pass


class AlreadyAnswered(ValueError):
    pass


@dataclass
class SessionRecord:
    started_at: float
    # HuRI modules the visitor picked: they shape the impression being rated.
    modules: List[str] = field(default_factory=list)
    # Module -> option -> value the visitor picked, e.g. qag's mode.
    options: Dict[str, Dict[str, str]] = field(default_factory=dict)
    ended_at: float | None = None
    questions: int = 0
    end_reason: str | None = None
    answered: bool = False


class ResponseStore:
    """Questionnaire answers, one JSON line per visitor.

    Only sessions that really reached HuRI can submit, and only once: the
    session id handed out at ``ready`` is the ticket. Nothing from the
    conversation itself is kept, only its length.
    """

    def __init__(
        self,
        data_dir: Path,
        ticket_ttl_s: float = 2 * 3600,
        clock: Callable[[], float] = time.time,
    ):
        self.path = Path(data_dir) / "responses.jsonl"
        self.ticket_ttl_s = ticket_ttl_s
        self.clock = clock
        self._sessions: Dict[str, SessionRecord] = {}

    def open_session(
        self,
        session_id: str,
        modules: Iterable[str] = (),
        options: Mapping[str, Mapping[str, str]] | None = None,
    ) -> SessionRecord:
        self._expire()
        record = SessionRecord(
            started_at=self.clock(),
            modules=sorted(modules),
            options={m: dict(o) for m, o in (options or {}).items()},
        )
        self._sessions[session_id] = record
        return record

    def close_session(self, session_id: str, reason: str) -> None:
        record = self._sessions.get(session_id)
        if record is not None and record.ended_at is None:
            record.ended_at = self.clock()
            record.end_reason = reason

    def submit(
        self,
        session_id: str,
        answers: Mapping[str, Mapping[str, int]],
        comment: str,
        scores: Mapping[str, Mapping[str, float]] | None = None,
    ) -> Dict[str, Any]:
        self._expire()
        record = self._sessions.get(session_id)
        if record is None:
            raise UnknownSession(session_id)
        if record.answered:
            raise AlreadyAnswered(session_id)

        now = self.clock()
        ended_at = record.ended_at if record.ended_at is not None else now
        entry = {
            "submitted_at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
            "session_id": session_id,
            "modules": record.modules,
            "options": record.options,
            "conversation_seconds": round(ended_at - record.started_at, 1),
            "questions_asked": record.questions,
            "end_reason": record.end_reason,
            "answers": {qid: dict(items) for qid, items in answers.items()},
            "scores": {qid: dict(dims) for qid, dims in (scores or {}).items()},
            "comment": comment,
        }

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

        record.answered = True
        return entry

    def _expire(self) -> None:
        cutoff = self.clock() - self.ticket_ttl_s
        for session_id in [
            sid for sid, rec in self._sessions.items() if rec.started_at < cutoff
        ]:
            del self._sessions[session_id]
