# Data / ML

Covers the three Data/ML line items from the task sheet.

| # | Item | Tool | Status  |
|---|------|------|---------|
| 1 | Data versioning                  | S3 with lifecycle rules | Not Yet |
| 2 | Prompt versioning & registry     | LangSmith               | Not Yet |
| 3 | Model / artifact versioning      | MLflow                  | Not Yet |

---

## 1. Data versioning — S3 with lifecycle rules

**Tool:** Amazon S3 with versioning enabled and lifecycle rules to transition / expire old object versions.

### Bucket

The single media bucket is referenced from `stmt_s3` in [pulumi/__main__.py:40](../pulumi/__main__.py#L40):

```
mikshi-vlm-media-prod-<account_id>
```

Services with the `s3` IRSA token (`backend`, `vlm-processing`) can `GetObject`, `PutObject`, `DeleteObject`, `ListBucket` against it.

### Versioning

- **Enable** S3 Versioning on the bucket. Every overwrite or delete becomes a new version, not a destructive operation.
- This gives us:
  - Recovery from accidental deletes (within the lifecycle window).
  - Audit trail of what version was served at what time.
  - Reproducibility for ML pipelines that reference a specific object version-id.

### Lifecycle rules

Storage cost grows quickly without lifecycle. Recommended rules:

| Object class                      | Rule                                                                |
|-----------------------------------|---------------------------------------------------------------------|
| Current versions (hot)            | Stay in `STANDARD`.                                                 |
| Current versions, no access 30d   | Transition to `STANDARD_IA`.                                        |
| Current versions, no access 90d   | Transition to `GLACIER_IR` (Instant Retrieval).                     |
| Noncurrent versions, age > 30d    | Transition to `GLACIER_IR`.                                         |
| Noncurrent versions, age > 365d   | Expire (delete permanently).                                        |
| Incomplete multipart uploads > 7d | Abort.                                                              |

### Pulumi declaration

To be added alongside the bucket resource:

```python
aws.s3.BucketVersioningV2(
    "media-versioning",
    bucket=media_bucket.id,
    versioning_configuration={"status": "Enabled"},
)

aws.s3.BucketLifecycleConfigurationV2(
    "media-lifecycle",
    bucket=media_bucket.id,
    rules=[
        {
            "id": "current-tiering",
            "status": "Enabled",
            "transitions": [
                {"days": 30,  "storage_class": "STANDARD_IA"},
                {"days": 90,  "storage_class": "GLACIER_IR"},
            ],
        },
        {
            "id": "noncurrent-cleanup",
            "status": "Enabled",
            "noncurrent_version_transitions": [
                {"noncurrent_days": 30, "storage_class": "GLACIER_IR"},
            ],
            "noncurrent_version_expiration": {"noncurrent_days": 365},
        },
        {
            "id": "abort-multipart",
            "status": "Enabled",
            "abort_incomplete_multipart_upload": {"days_after_initiation": 7},
        },
    ],
)
```

### Object key conventions

Use prefixes that align with lifecycle / access patterns:

```
raw/<dataset>/<yyyy>/<mm>/<dd>/<uuid>.<ext>     # immutable inputs
processed/<dataset>/<yyyy>/<mm>/<dd>/...        # pipeline outputs
artifacts/<service>/<sha>/...                   # build/deploy artifacts (rarely re-read)
```

Different prefixes can take different lifecycle rules later if access patterns diverge.

---

## 2. Prompt versioning & registry — LangSmith

**Tool:** LangSmith (LangChain) — managed prompt registry, eval, and tracing for LLM apps.

### What it stores

- **Prompts** (named, versioned, taggable).
- **Datasets** (input/output pairs for eval).
- **Runs / traces** of prompt invocations, with inputs, outputs, latency, cost.
- **Evaluations** of prompts against datasets.

### Why we use it (vs. inlining prompts in code)

- Prompts change far more often than code; coupling them to a deploy is friction.
- Non-engineers (PM, prompt engineers) need to iterate on prompts without a PR.
- Eval scores need to live with the prompt version that produced them.

### Integration

Used by services that call LLMs — primarily `vlm-processing`, possibly `backend` and `chat`.

```python
# vlm-processing/app/prompts.py
from langsmith import Client
from langchain_core.prompts import ChatPromptTemplate

client = Client()

def get_prompt(name: str, tag: str = "production"):
    # Pulls the prompt version tagged "production" from the LangSmith registry
    return client.pull_prompt(f"mikshi-vlm/{name}", include_model=False, tag=tag)

prompt: ChatPromptTemplate = get_prompt("vlm-caption")
```

### Secrets

LangSmith API key stored in AWS Secrets Manager under `Mikshi-VLM/langsmith/api-key`. Pods read it via the `secrets` IRSA token already wired in [pulumi/__main__.py:24](../pulumi/__main__.py#L24).

### Promotion flow

```
draft  ──►  qa  ──►  production
                       ▲
                       │
                  promoted only after eval passes
```

Tags in LangSmith mirror environments. The service code always pulls by tag (`production` in prod, `qa` in QA), never by raw version ID.

### Tracing

LangSmith automatically captures traces for every LLM call when the SDK is initialized. These complement OpenTelemetry traces (see [observability.md](observability.md) §3) for the LLM-specific path: token counts, prompt + completion text, model used.

---

## 3. Model / artifact versioning — MLflow

**Tool:** MLflow Model Registry.

### What it stores

- **Experiments** (runs with parameters, metrics, artifacts).
- **Models** (registered with name + version).
- **Stage transitions** (`None` → `Staging` → `Production` → `Archived`).

### Deployment

Self-hosted MLflow tracking server in the cluster (or a separate VPC-internal EC2 / EKS deployment), backed by:

- **Backend store:** RDS PostgreSQL (metadata).
- **Artifact store:** S3 bucket `mikshi-vlm-mlflow-artifacts-<account_id>`.

```
                ┌─────────────────────┐
   Trainer ───► │  MLflow Tracking    │ ──► RDS Postgres   (metadata)
                │  Server (in EKS)    │ ──► S3             (model artifacts)
                └─────────────────────┘
                         ▲
                         │ pull model uri
                         │
                vlm-processing pod
```

### Registering a model (training pipeline)

```python
import mlflow

mlflow.set_tracking_uri("https://mlflow.mikshi-vlm.internal")
mlflow.set_experiment("vlm-captioner")

with mlflow.start_run():
    mlflow.log_param("epochs", 5)
    mlflow.log_metric("val_loss", 0.42)
    mlflow.pytorch.log_model(
        model,
        artifact_path="model",
        registered_model_name="vlm-captioner",
    )
```

### Loading a model (inference pipeline)

```python
import mlflow.pytorch
model = mlflow.pytorch.load_model("models:/vlm-captioner/Production")
```

`vlm-processing` reads the `Production` alias at startup, so a stage transition in MLflow promotes a new model on the next pod restart (rolling restart triggered manually after promotion).

### IRSA + secrets

- `vlm-processing` IRSA already grants S3 access (good — needed for artifact downloads from `mikshi-vlm-media-prod-*`).
- Add the MLflow artifact bucket to the same `stmt_s3` builder (or a new `mlflow` IRSA token) in [pulumi/__main__.py](../pulumi/__main__.py).
- MLflow tracking server credentials (DB, auth) stored in `Mikshi-VLM/mlflow/*` secrets.

### Promotion flow

```
None  ──►  Staging  ──►  Production  ──►  Archived
                 ▲              ▲
                 │              │
        validated by QA   manually promoted
        eval pipeline     via MLflow UI / CLI
```

Only one model version may hold the `Production` stage at a time — this is the contract `vlm-processing` relies on.
