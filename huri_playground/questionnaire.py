from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping

from .files import load_mapping

QUESTIONNAIRES_DIR = Path(__file__).parent / "questionnaires"


class InvalidAnswers(ValueError):
    pass


@dataclass(frozen=True)
class Questionnaire:
    id: str
    points: int
    item_ids: List[str]
    definition: Mapping[str, Any]  # the raw yaml, rendered as-is by the page
    scores: Mapping[str, List[str]]  # dimension -> the items it averages

    def score(self, clean: Mapping[str, int]) -> Dict[str, float]:
        """Mean of each dimension's items, on the questionnaire's own scale."""
        return {
            dimension: round(sum(clean[i] for i in items) / len(items), 2)
            for dimension, items in self.scores.items()
        }

    def validate(self, answers: Any) -> Dict[str, int]:
        """Every item answered exactly once, with a value on the scale."""
        if not isinstance(answers, Mapping):
            raise InvalidAnswers(f"{self.id}: answers must be an object")

        missing = [i for i in self.item_ids if i not in answers]
        unknown = sorted(set(answers) - set(self.item_ids))
        if missing or unknown:
            raise InvalidAnswers(
                f"{self.id}: missing items {missing}, unknown items {unknown}"
            )

        clean: Dict[str, int] = {}
        for item_id in self.item_ids:
            value = answers[item_id]
            # bool is an int subclass, and `true` is not a point on a scale.
            if isinstance(value, bool) or not isinstance(value, int):
                raise InvalidAnswers(f"{self.id}.{item_id}: not an integer")
            if not 1 <= value <= self.points:
                raise InvalidAnswers(
                    f"{self.id}.{item_id}: {value} is outside 1..{self.points}"
                )
            clean[item_id] = value
        return clean


def load_questionnaire(path: Path) -> Questionnaire:
    raw = load_mapping(path)

    item_ids = [item["id"] for section in raw["sections"] for item in section["items"]]
    if len(item_ids) != len(set(item_ids)):
        raise ValueError(f"{path}: duplicate item ids")

    scores = {dim: list(items) for dim, items in raw.get("scores", {}).items()}
    for dim, items in scores.items():
        unknown = sorted(set(items) - set(item_ids))
        if not items or unknown:
            raise ValueError(f"{path}: score {dim!r} has unknown items {unknown}")

    return Questionnaire(
        id=raw["id"],
        points=raw["scale"]["points"],
        item_ids=item_ids,
        definition=raw,
        scores=scores,
    )


def load_questionnaires(
    ids: List[str], directory: Path = QUESTIONNAIRES_DIR
) -> List[Questionnaire]:
    """Load the questionnaires to show, in the order given."""
    return [load_questionnaire(directory / f"{qid}.yaml") for qid in ids]
