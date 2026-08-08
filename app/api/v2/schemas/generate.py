"""POST /api/v2/generate — see docs/api-routes.md and docs/business-rules.md §1.

`AngleSpec` is a discriminated union modeled as a single object with mutually
exclusive optional fields rather than a tagged union, because the wire shape
(`{"storage_path": "..."}` / `{"synthetic": true}` / `{"skip": true}`) has no
shared discriminator key. `extra="forbid"` plus the mode validator is what
makes an angle with both `skip` and `storage_path` fail at parse time instead
of silently picking one.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.db.models.enums import Angle, SourceType, SubJobStatus


class AngleSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    storage_path: str | None = None
    synthetic: Literal[True] | None = None
    skip: Literal[True] | None = None

    @model_validator(mode="after")
    def _exactly_one_mode(self) -> "AngleSpec":
        modes_set = sum(1 for v in (self.storage_path, self.synthetic, self.skip) if v is not None)
        if modes_set != 1:
            raise ValueError(
                "exactly one of storage_path, synthetic, or skip must be set for an angle"
            )
        return self

    @property
    def mode(self) -> Literal["uploaded", "synthetic", "skipped"]:
        if self.storage_path is not None:
            return "uploaded"
        if self.synthetic:
            return "synthetic"
        return "skipped"


class GenerateJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category_code: str
    sku_reference: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    angles: dict[Angle, AngleSpec]


class ResolvedAnglePlan(BaseModel):
    angle: Angle
    source_type: SourceType
    status: SubJobStatus
    storage_path: str | None = None


class JobAcceptedResponse(BaseModel):
    job_id: str
    status: Literal["PENDING"]
    angles: list[ResolvedAnglePlan]
    poll_after_ms: int
