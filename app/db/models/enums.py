"""Native Postgres enum types. See docs/schema.md — Enums.

Values are UPPER_SNAKE and never removed once shipped; new values are added
via `ALTER TYPE ... ADD VALUE` in a migration (see docs/conventions.md).
"""

import enum


class Angle(enum.StrEnum):
    FRONT = "FRONT"
    SIDE = "SIDE"
    DIAGONAL = "DIAGONAL"
    TOP = "TOP"


class JobStatus(enum.StrEnum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    PARTIAL_SUCCESS = "PARTIAL_SUCCESS"
    FAILED = "FAILED"


class SubJobStatus(enum.StrEnum):
    PENDING = "PENDING"
    MATTING = "MATTING"
    GENERATING = "GENERATING"
    QA_REVIEW = "QA_REVIEW"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    REJECTED = "REJECTED"
    SKIPPED = "SKIPPED"


class SourceType(enum.StrEnum):
    UPLOADED = "UPLOADED"
    SYNTHETIC = "SYNTHETIC"


class Operation(enum.StrEnum):
    ANGLE_GENERATION = "ANGLE_GENERATION"
    BACKGROUND_REMOVAL = "BACKGROUND_REMOVAL"
    BACKGROUND_REPLACEMENT = "BACKGROUND_REPLACEMENT"
    MATCH = "MATCH"


class AssetKind(enum.StrEnum):
    INPUT = "INPUT"
    MATTE = "MATTE"
    OUTPUT = "OUTPUT"


class FailureClass(enum.StrEnum):
    TRANSIENT_PROVIDER = "TRANSIENT_PROVIDER"
    TRANSIENT_NETWORK = "TRANSIENT_NETWORK"
    RATE_LIMITED = "RATE_LIMITED"
    INVALID_INPUT = "INVALID_INPUT"
    SAFETY_REFUSAL = "SAFETY_REFUSAL"
    QA_REJECTED = "QA_REJECTED"
    INTERNAL = "INTERNAL"


class QAStatus(enum.StrEnum):
    NOT_APPLICABLE = "NOT_APPLICABLE"
    PASSED = "PASSED"
    FLAGGED = "FLAGGED"
    FAILED = "FAILED"


class SyncStatus(enum.StrEnum):
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
