# Observability

Covers the six Observability line items from the task sheet. All p1, owner Rakshith B.

| # | Item | Tool | Status  |
|---|------|------|---------|
| 1 | Production monitoring                  | CloudWatch                       | Not Yet |
| 2 | Centralized logging with retention     | CloudWatch Logs                  | Not Yet |
| 3 | Distributed tracing                    | OpenTelemetry                    | Not Yet |
| 4 | Alerting / paging                      | SNS + escalation policy          | Not Yet |
| 5 | Per-container CPU/memory               | cAdvisor / `kubectl top pod`     | Not Yet |
| 6 | Same setup replicated for QA           | (all of the above)               | Not Yet |

---

## 1. Production monitoring — CloudWatch

**Tool:** Amazon CloudWatch (Container Insights).

### What it gives us

- Per-cluster, per-node, per-namespace, per-pod CPU / memory / network / disk metrics.
- AWS-native metrics for ALB (request count, 4xx/5xx, latency), MSK (broker, consumer lag), and S3.
- Dashboards per environment.
- Alarms wired to SNS (see §4).

### Install / wiring

CloudWatch Container Insights is enabled by deploying the **CloudWatch Agent** + **Fluent Bit** as DaemonSets in the `amazon-cloudwatch` namespace. AWS publishes a single manifest:

```bash
ClusterName=<cluster> RegionName=<region>
curl https://raw.githubusercontent.com/aws-samples/amazon-cloudwatch-container-insights/latest/k8s/quickstart/cwagent-fluent-bit-quickstart.yaml \
  | sed "s/{{cluster_name}}/$ClusterName/;s/{{region_name}}/$RegionName/" \
  | kubectl apply -f -
```

The agent's IRSA role needs `CloudWatchAgentServerPolicy` (managed by AWS).

### Dashboards

One CloudWatch dashboard per environment, with widgets for:

- Cluster CPU / memory utilization
- Per-namespace pod count + restart rate
- Per-deployment (`backend`, `auth`, `chat`, `frontend`, `vlm-processing`) CPU/memory + RED metrics from OpenTelemetry
- ALB 5xx rate, p95 latency
- MSK consumer lag (`backend`, `vlm-processing`)
- S3 4xx/5xx + request count

Dashboard JSON is checked into Pulumi (`aws.cloudwatch.Dashboard`) so it tracks code.

### Custom application metrics

Application code emits OTel metrics (counters, histograms) → OTel Collector → CloudWatch via the `awsemf` exporter. RED metrics (Rate, Errors, Duration) per HTTP route are the baseline; per-service additions documented in each service's README.

---

## 2. Centralized logging — CloudWatch Logs with retention

**Tool:** Amazon CloudWatch Logs, fed by Fluent Bit.

### Log groups

| Log group                                   | Source                            | Retention |
|---------------------------------------------|-----------------------------------|-----------|
| `/aws/containerinsights/<cluster>/application` | All container stdout/stderr     | 30 days   |
| `/aws/containerinsights/<cluster>/host`     | Node-level (kubelet, journald)    | 14 days   |
| `/aws/containerinsights/<cluster>/dataplane`| `aws-node`, `kube-proxy`, etc.    | 14 days   |
| `/mikshi-vlm/nginx/access`                  | Nginx access logs                 | 30 days   |
| `/mikshi-vlm/audit`                         | Auth events from `svc-auth`       | 365 days  |

Retention is set declaratively in Pulumi:

```python
aws.cloudwatch.LogGroup(
    "audit-logs",
    name="/mikshi-vlm/audit",
    retention_in_days=365,
    tags={"Project": "Mikshi-VLM"},
)
```

### Structure

All application logs are JSON. Required fields per line:

| Field         | Example                              |
|---------------|--------------------------------------|
| `ts`          | `2026-05-04T12:34:56.789Z`           |
| `level`       | `info` / `warn` / `error`            |
| `service`     | `backend`                            |
| `trace_id`    | (from OTel, joins logs↔traces)       |
| `span_id`     | (from OTel)                          |
| `msg`         | Human-readable                       |
| `*`           | Service-specific structured fields   |

CloudWatch Logs Insights queries assume this shape (e.g. `fields @timestamp, level, service, trace_id, msg | filter level = "error"`).

---

## 3. Distributed tracing — OpenTelemetry

**Tool:** OpenTelemetry (SDKs in services + OTel Collector in cluster).

### Topology

```
  app pod ──► OTel SDK ──► OTel Collector (DaemonSet)
                              │
                              ├── traces  ──► CloudWatch (via awsxray exporter) → X-Ray service map
                              ├── metrics ──► CloudWatch (awsemf exporter)
                              └── logs    ──► CloudWatch Logs (awscloudwatchlogs exporter, optional)
```

The Collector runs as a DaemonSet so apps point at `localhost:4317` (gRPC) / `:4318` (HTTP), avoiding cross-node hops.

### Collector config (sketch)

```yaml
receivers:
  otlp:
    protocols: { grpc: {}, http: {} }
processors:
  batch: {}
  resource:
    attributes:
      - key: deployment.environment
        value: prod
        action: insert
exporters:
  awsxray:
    region: ${env:AWS_REGION}
  awsemf:
    region: ${env:AWS_REGION}
    namespace: MikshiVLM
service:
  pipelines:
    traces:  { receivers: [otlp], processors: [batch, resource], exporters: [awsxray] }
    metrics: { receivers: [otlp], processors: [batch, resource], exporters: [awsemf] }
```

### SDK setup per service

- **Python** (`backend`, `auth`, `chat`, `vlm-processing`): `opentelemetry-distro` + `opentelemetry-instrumentation-<framework>` (FastAPI/Flask/etc), auto-instrumented at startup.
- **Frontend** (`svc-frontend`): `@opentelemetry/sdk-trace-web` + fetch instrumentation; spans sent to a public OTLP HTTP endpoint exposed via the ingress (auth-gated).

Resource attributes set per service: `service.name`, `service.version` (image tag), `deployment.environment`, `k8s.pod.name`.

### Trace ↔ log correlation

OTel SDKs inject `trace_id` and `span_id` into the logging context; both fields are required in the JSON log schema (§2). CloudWatch Logs Insights can pivot from a trace to its logs and vice versa.

---

## 4. Alerting / paging — SNS + escalation policy

**Tool:** Amazon SNS as the fan-out, with a documented escalation policy.

### Topics

| Topic                         | Purpose                              | Subscribers                        |
|-------------------------------|--------------------------------------|------------------------------------|
| `mikshi-vlm-alerts-critical`  | Pageable, 24/7                       | On-call email + SMS                |
| `mikshi-vlm-alerts-warning`   | Business-hours triage                | Team email distribution            |
| `mikshi-vlm-alerts-info`      | FYI; goes to a Slack/email digest    | Slack channel via email-to-channel |

Topics are Pulumi-managed:

```python
aws.sns.Topic("alerts-critical", name="mikshi-vlm-alerts-critical", tags={...})
```

### Alarm → topic mapping

| Alarm                                                   | Severity | Topic                            |
|---------------------------------------------------------|----------|----------------------------------|
| ALB 5xx rate > 1% over 5 min                            | critical | `mikshi-vlm-alerts-critical`     |
| ALB p95 latency > SLO over 10 min                       | warning  | `mikshi-vlm-alerts-warning`      |
| Pod CrashLoopBackOff (any deployment in `mikshi-vlm`)   | critical | `mikshi-vlm-alerts-critical`     |
| HPA at max replicas > 10 min                            | warning  | `mikshi-vlm-alerts-warning`      |
| Node CPU > 85% for 15 min                               | warning  | `mikshi-vlm-alerts-warning`      |
| MSK consumer lag > threshold for 10 min                 | critical | `mikshi-vlm-alerts-critical`     |
| ACM cert < 14 days to expiry                            | warning  | `mikshi-vlm-alerts-warning`      |
| ECR scan critical/high finding on a deployed tag        | warning  | `mikshi-vlm-alerts-warning`      |
| AWS Budgets actual > 80% of monthly budget              | warning  | `mikshi-vlm-alerts-warning`      |
| AWS Budgets forecast > 100%                             | critical | `mikshi-vlm-alerts-critical`     |

### Escalation policy

| Time after page  | Action                                        |
|------------------|-----------------------------------------------|
| 0 min            | Primary on-call paged via SMS + email         |
| 15 min, no ack   | Secondary on-call paged                       |
| 30 min, no ack   | Engineering manager paged                     |
| 60 min, no resolve | Incident channel opened, status page updated|

The on-call rotation is managed in a calendar / shared sheet; SNS subscriptions are updated weekly to point at the active on-call.

---

## 5. Per-container CPU / memory — cAdvisor / kubectl top

**Tool:** cAdvisor (built into kubelet) surfaced via `kubectl top` and CloudWatch Container Insights.

### Daily / on-call use

```bash
# Quick look
kubectl top pod -n mikshi-vlm
kubectl top pod -n mikshi-vlm --containers

# Sorted
kubectl top pod -n mikshi-vlm --sort-by=memory
kubectl top pod -n mikshi-vlm --sort-by=cpu

# Per node
kubectl top node
```

`metrics-server` must be installed for `kubectl top` to work:

```bash
kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml
```

### Continuous

cAdvisor metrics flow into CloudWatch via Container Insights (§1). Dashboards include per-pod CPU/memory time series; HPA (see [performance.md](performance.md)) consumes these via `metrics-server` for scaling decisions.

### Sizing workflow

1. Run load test (see [performance.md](performance.md)).
2. Watch `kubectl top pod` and CloudWatch Container Insights graphs for steady-state and peak.
3. Set `requests` to steady-state, `limits` to ~2× peak.
4. Re-run load test, confirm no OOMKills (`kubectl get events -n mikshi-vlm | grep OOM`).

---

## 6. Same monitoring + alerting setup replicated for QA

QA must mirror the prod observability stack so issues surface before promotion.

### Replication checklist

- [ ] Container Insights agents deployed in QA cluster (same DaemonSets).
- [ ] CloudWatch dashboards for QA created from the same Pulumi `Dashboard` resource (parameterised by stack).
- [ ] Log groups exist in QA with the **same retention values** as prod.
- [ ] OTel Collector DaemonSet deployed in QA with `deployment.environment=qa` resource attribute.
- [ ] SNS topics `mikshi-vlm-alerts-{critical,warning,info}` exist in the QA account.
- [ ] Subscriptions on QA topics go to the **QA channel** (not prod on-call) — typically email distribution and Slack.
- [ ] Same alarm definitions deployed against QA metrics (same thresholds; loosen only with documented justification).
- [ ] Per-pod cAdvisor metrics visible via `kubectl top` in QA.

This is enforced by reusing the same Pulumi program with a `qa` stack — alarms, log groups, dashboards, and SNS topics are all declared as code, so QA cannot drift from prod by accident.
