from typing import Literal

from pydantic import BaseModel, ConfigDict


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Fact(Record):
    area: Literal["venue", "schedule", "food", "judging", "budget"]
    key: str
    value: str | int | float | bool | list[str] | None
    kind: Literal["fact", "rule", "proposal", "correction", "instruction"]
    authority: Literal["owner", "participant", "unknown"]
    operator: Literal["eq", "ge", "le", "contains", "excludes", "none"]
    evidence: str


class Message(Record):
    evidence: str
    source: str
    date: str
    disposition: Literal["relevant", "irrelevant", "invalid"]
    reason: str
    facts: list[Fact]


class Extraction(Record):
    messages: list[Message]
