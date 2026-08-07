# Business Rules

Rules in this file are invariants. If code and this file disagree, one of them is a bug —
resolve it explicitly, never silently.

---

## 1. Angle matrix

- There are exactly four angles: `FRONT`, `SIDE`, `DIAGONAL`, `TOP`.
- There are seven jewelry categories. Which angles are enabled is **per category** and
  defined by the active config version — never hardcoded.
- A job may request between 1 and 4 angles. Zero requested angles is a `422`.
- An angle disabled for a category cannot be requested. A `422`, not a silent skip.
- `synthetic_allowed` is also per category per angle. Requesting synthetic generation for
  an angle where it is not allowed is a `422`.

---

## 2. Job state machine

**Parent job:**

```
PENDING ──► PROCESSING ──► COMPLETED | PARTIAL_SUCCESS | FAILED
```

`COMPLETED`, `PARTIAL_SUCCESS`, and `FAILED` are terminal — **except** that a successful
retry can move `PARTIAL_SUCCESS` or `FAILED` back to `PROCESSING`. This is the one legal
backward transition and it exists only via the retry endpoint.

**Sub-job:**

```
PENDING ──► GENERATING ──► QA_REVIEW ──► COMPLETED
                       └──► COMPLETED  (QA not applicable)
   any ──► FAILED | REJECTED
PENDING ──► SKIPPED  (set at creation, never transitions)
```

- No `MATTING` step — see `docs/decisions/0001-drop-local-matting.md`. Every
  sub-job goes `PENDING` → `GENERATING` directly, real or synthetic.
- `QA_REVIEW` is entered **only** for `SYNTHETIC` sub-jobs, and only when the similarity
  score falls below threshold. Above threshold, go straight to `COMPLETED`.
  Real-photo (`UPLOADED`) sub-jobs have no QA gate at all — see
  `docs/ai-integration.md`.

---

## 3. Parent status computation

Recomputed after every sub-job terminal transition, inside the same transaction.
`SKIPPED` sub-jobs are excluded from all counts.

Let `R` = requested (non-skipped) sub-jobs, `S` = succeeded (`COMPLETED`),
`F` = failed (`FAILED` + `REJECTED`).

| Condition | Parent status |
| :--- | :--- |
| `S + F < R` | `PROCESSING` |
| `S = R` | `COMPLETED` |
| `F = R` | `FAILED` |
| `S > 0` and `F > 0` and `S + F = R` | `PARTIAL_SUCCESS` |

`completed_at` is set on entering a terminal state and **cleared** when a retry moves the
job back to `PROCESSING`.

A single-angle job that fails is `FAILED`, not `PARTIAL_SUCCESS`. Partial success requires
at least one success and at least one failure.

---

## 4. Failure classification

Every failure is classified before it is handled. This determines both retry behavior and
what the ERP shows.

| Class | Cause | Internal backoff | Client retry offered |
| :--- | :--- | :--- | :--- |
| `RATE_LIMITED` | Provider 429 | Yes — 3 attempts, exponential + jitter | Yes |
| `TRANSIENT_PROVIDER` | Provider 5xx | Yes — 3 attempts | Yes |
| `TRANSIENT_NETWORK` | Timeout, connection reset | Yes — 3 attempts | Yes |
| `INVALID_INPUT` | Corrupt image, unsupported format | No | No |
| `SAFETY_REFUSAL` | Provider declined to generate | No | No |
| `QA_REJECTED` | Similarity gate failed, or human rejected | No | No |
| `INTERNAL` | Unhandled backend exception | No | Yes |

**"Fail-fast" applies to deterministic classes only.** Internal backoff on transient
classes happens inside the sub-task and is invisible to the client — the sub-job does not
enter `FAILED` until the backoff budget is exhausted. Retrying a network blip three times
over six seconds is not a violation of fail-fast; it is what makes fail-fast tolerable.

`INVALID_INPUT`, `SAFETY_REFUSAL`, and `QA_REJECTED` set status `REJECTED`, not `FAILED`,
and `retryable: false`. The ERP must not render a retry button for these.

---

## 5. Retry rules

- Retry operates on a **single angle**, never a whole job.
- Maximum **3 client-initiated retries** per sub-job (`attempt_count` ceiling). The fourth
  request returns `409`.
- Retry requires the sub-job to be in `FAILED`. `REJECTED`, `COMPLETED`, and in-flight
  states all return `409`.
- Retry requires the input asset to be unexpired. An expired input returns `409` telling
  the client to submit a new job — the image is gone and cannot be regenerated.
- Retry reuses the job's **pinned** `config_version_id`, not the currently active version.
  Angles within one job must be visually consistent; a prompt change between the original
  run and the retry would break that.
- Retry re-runs the full generation call from scratch. It does not reuse any
  artifact from the failed attempt.
- Every retry writes a new `cost_events` row. Retries cost money and must be visible in
  cost reporting.

---

## 6. Synthetic angle rules

An angle with no source photograph is `source_type: SYNTHETIC`.

- Only permitted where the category's config sets `synthetic_allowed: true`.
- Generated from the category's reference image matrix plus prompt — never from another
  angle's output. Chaining generated images compounds hallucination.
- **Every synthetic output passes the QA similarity gate before it can be `COMPLETED`.**
- The `synthetic: true` flag is always returned in the status payload. The ERP must
  visually distinguish synthetic angles from photographed ones.

The commercial reason: a generated angle can invent chain links, prong counts, or facet
geometry the physical piece does not have. Flagging is a misrepresentation control, not a
cosmetic detail.

---

## 7. QA gate

Applies to `SYNTHETIC` sub-jobs only. Real-photo angles have no QA gate — see
`docs/decisions/0001-drop-local-matting.md` for the accepted risk this carries.

| Score vs threshold | Outcome |
| :--- | :--- |
| `qa_score >= threshold` | `qa_status: PASSED`, sub-job `COMPLETED` |
| `qa_score < threshold` | `qa_status: FLAGGED`, sub-job `QA_REVIEW`, enters human queue |
| Human approves | `qa_status: PASSED`, sub-job `COMPLETED` |
| Human rejects | `qa_status: FAILED`, sub-job `REJECTED`, `failure_class: QA_REJECTED` |

Threshold comes from `config.global.qa_similarity_threshold`. Default `0.82`, to be
calibrated against real client pieces in Phase 9 — treat the default as a placeholder.

A sub-job in `QA_REVIEW` counts as neither succeeded nor failed; the parent stays
`PROCESSING`. Do not return a flagged image to the client before a human decision.

---

## 8. Idempotency

- `Idempotency-Key` is **required** on `POST /generate` and `POST /retry`.
- Uniqueness is scoped `(client_id, idempotency_key)`.
- A replayed key returns the original response. It does not create a job and does not
  bill the provider.
- Keys are retained 24 hours in Redis and permanently on the `jobs` row.
- The same key with a *different* payload returns `409`. Silently returning the old job
  for a different request is worse than erroring.

---

## 9. Config rules

- Google Sheets is the authoring surface. Postgres holds the immutable record.
- A sync computes a SHA-256 over the normalized payload. **Unchanged hash creates no new
  version.**
- Exactly one `config_versions` row is active at a time.
- Every job pins `config_version_id` at creation and never re-reads live config.
- **A Sheets outage must never fail a job.** Order of resolution: Redis cache → active
  Postgres row → hard failure only if both are unavailable.
- A sync that fails validation is recorded with `sync_status: FAILED` and does **not**
  become active. The previous version stays active.

---

## 10. Cost rules

- One `cost_events` row per provider call, including failed calls that were still billed.
- `unit_cost_usd` comes from configuration, never a hardcoded constant — provider pricing
  changes and historical rows must retain the rate that applied at the time.
- Job cost = sum of its cost events, including all retries.
- Cost is recorded even when the sub-job ends `REJECTED`. A safety refusal after a billed
  call still cost money.

---

## 11. Retention

| Asset kind | Retention | Reason |
| :--- | :--- | :--- |
| `INPUT` | 90 days | Must outlive the retry window and support audit |
| `MATTE` | 30 days | Regenerable from input |
| `OUTPUT` | Indefinite (pending client policy) | Client's catalog assets |

Asset rows are never deleted. Set `expires_at`; storage lifecycle removes the bytes. A
row whose bytes are gone still answers "what did we produce for this SKU."

---

## 12. Client-facing URL rules

- All image URLs are **signed, 1-hour TTL**, generated fresh on every status read.
- Signed URLs are never persisted to the database and never written to logs.
- Buckets are private. There is no public read path.
