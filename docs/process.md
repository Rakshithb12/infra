# Process

Covers the two Process line items from the task sheet.

| # | Item | Tool | Status  |
|---|------|------|---------|
| 1 | Architecture & ops documentation       | This `docs/` directory          | Not Yet → done with this set |
| 2 | Cost monitoring & budget alerts        | AWS Budgets                     | Not Yet                       |

---

## 1. Architecture & ops documentation

**Tool:** Markdown in `docs/`, versioned with the Pulumi code.

### Layout

```
docs/
  architecture.md       ← entry point + high-level diagram
  cicd.md               ← CI/CD pipelines, rollback, image scan, ECR lifecycle
  infrastructure.md     ← Nginx, TLS, Pulumi
  observability.md      ← CloudWatch, OTel, SNS, cAdvisor
  performance.md        ← Locust/JMeter, container opt, HPA/ASG
  data-ml.md            ← S3 lifecycle, LangSmith, MLflow
  process.md            ← this file
```

### Conventions

- Every doc opens with a table mapping its sheet line items → tool → status.
- Code/config samples are illustrative; the **source of truth** is [pulumi/__main__.py](../pulumi/__main__.py) and [services.json](../services.json).
- File / line references use clickable markdown links (e.g. `[pulumi/__main__.py:120](../pulumi/__main__.py#L120)`) so reviewers can jump to source.
- Diagrams are ASCII for portability; if a richer diagram is added later it goes under `docs/diagrams/` as both a source file (`.drawio` / `.excalidraw`) and an exported `.png`.

### Env setup (operator quickstart)

Steps a new operator should be able to follow from the docs alone:

1. **Install tools:** AWS CLI v2, `kubectl`, `helm`, Pulumi CLI, Docker, Python 3.12, Node 20.
2. **AWS credentials:** SSO into the account; verify with `aws sts get-caller-identity`.
3. **Kubeconfig:**
   ```powershell
   aws eks update-kubeconfig --name <cluster_name> --region <region>
   kubectl get ns
   kubectl get pods -n mikshi-vlm
   ```
4. **Pulumi:** `pulumi login`, `pulumi stack select prod`, `pulumi preview`.
5. **Read** [architecture.md](architecture.md) end-to-end before touching anything.

### Keeping docs honest

- Any Pulumi change that adds a tool or topology element requires a matching doc update in the same PR.
- Reviewers reject PRs that change architecture without updating `docs/`.
- Quarterly sweep: walk each doc, click every file/line link, verify it still resolves.

---

## 2. Cost monitoring & budget alerts — AWS Budgets

**Tool:** AWS Budgets, with notifications routed through SNS to the existing alert topics (see [observability.md](observability.md) §4).

### Budgets to define

| Budget                                | Type     | Period   | Amount                            |
|---------------------------------------|----------|----------|-----------------------------------|
| `mikshi-vlm-monthly-total`            | COST     | Monthly  | Total monthly target              |
| `mikshi-vlm-monthly-eks`              | COST     | Monthly  | EKS + EC2 portion                 |
| `mikshi-vlm-monthly-msk`              | COST     | Monthly  | MSK portion                       |
| `mikshi-vlm-monthly-s3`               | COST     | Monthly  | S3 portion                        |
| `mikshi-vlm-monthly-cloudwatch`       | COST     | Monthly  | CloudWatch + Logs portion         |

Per-service / per-resource budgets use `CostFilters` keyed on the `Project=Mikshi-VLM` tag (already applied by Pulumi to ECR repos and IAM roles — extend this tag to all resources).

### Alert thresholds

For every budget:

| Threshold | Type      | Severity | Topic                          |
|-----------|-----------|----------|--------------------------------|
| 50%       | Actual    | info     | `mikshi-vlm-alerts-info`       |
| 80%       | Actual    | warning  | `mikshi-vlm-alerts-warning`    |
| 100%      | Actual    | critical | `mikshi-vlm-alerts-critical`   |
| 100%      | Forecast  | warning  | `mikshi-vlm-alerts-warning`    |
| 120%      | Forecast  | critical | `mikshi-vlm-alerts-critical`   |

### Pulumi declaration

```python
aws.budgets.Budget(
    "monthly-total",
    name="mikshi-vlm-monthly-total",
    budget_type="COST",
    limit_amount="<USD>",
    limit_unit="USD",
    time_unit="MONTHLY",
    cost_filters=[{"name": "TagKeyValue", "values": ["user:Project$Mikshi-VLM"]}],
    notifications=[
        {
            "comparison_operator": "GREATER_THAN",
            "notification_type": "ACTUAL",
            "threshold": 80,
            "threshold_type": "PERCENTAGE",
            "subscriber_sns_topic_arns": [warning_topic.arn],
        },
        {
            "comparison_operator": "GREATER_THAN",
            "notification_type": "ACTUAL",
            "threshold": 100,
            "threshold_type": "PERCENTAGE",
            "subscriber_sns_topic_arns": [critical_topic.arn],
        },
        {
            "comparison_operator": "GREATER_THAN",
            "notification_type": "FORECASTED",
            "threshold": 100,
            "threshold_type": "PERCENTAGE",
            "subscriber_sns_topic_arns": [warning_topic.arn],
        },
    ],
)
```

### Operating rhythm

- Review the **Cost Explorer** dashboard weekly; tag-grouped by `Project=Mikshi-VLM`.
- Treat any forecast-100% page as a P2 incident: investigate within one business day.
- Dev/QA budgets enforce a separate, smaller cap so prod traffic isn't masked by experimentation cost.
- Budget changes (raising the cap) require sign-off in the doc PR — do not silently raise the limit to clear the alarm.
