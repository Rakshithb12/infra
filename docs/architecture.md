# Mikshi-VLM — Architecture

Production architecture for the Mikshi-VLM platform on AWS EKS. Tooling choices follow the agreed task sheet.

---

## 1. High-level diagram

```
                                 ┌──────────────────────────────────────────────┐
                                 │                  Developers                  │
                                 └───────────────────┬──────────────────────────┘
                                                     │ git push
                                                     ▼
                                 ┌──────────────────────────────────────────────┐
                                 │              GitHub Actions (CI/CD)          │
                                 │  build → unit/integration/smoke → ECR scan   │
                                 │  → push image → kubectl/helm rollout         │
                                 └───────────────────┬──────────────────────────┘
                                                     │ AssumeRoleWithWebIdentity
                                                     │ (GitHub OIDC → IAM role)
                                                     ▼
 ┌────────────────┐         ┌──────────────────────────────────────────────────────────┐
 │ Pulumi (IaC)   │ ──────► │                       AWS Account                       │
 │ pulumi/__main__│         │                                                          │
 └────────────────┘         │  ┌────────────────────────────────────────────────────┐  │
                            │  │                    VPC (prod)                      │  │
                            │  │                                                    │  │
                            │  │   Internet ──► Route53 ──► ACM-cert ──► ALB        │  │
                            │  │                                          │         │  │
                            │  │                                          ▼         │  │
                            │  │  ┌────────────────────────────────────────────┐    │  │
                            │  │  │            EKS cluster (prod)              │    │  │
                            │  │  │   Namespace: mikshi-vlm                    │    │  │
                            │  │  │                                            │    │  │
                            │  │  │   Ingress: nginx (reverse proxy)           │    │  │
                            │  │  │            │                               │    │  │
                            │  │  │   ┌────────┴────────────────────────┐      │    │  │
                            │  │  │   ▼        ▼          ▼          ▼ │      │    │  │
                            │  │  │ frontend  auth     chat     backend│      │    │  │
                            │  │  │                                │   │      │    │  │
                            │  │  │                                ▼   │      │    │  │
                            │  │  │                       vlm-processing      │    │  │
                            │  │  │                                            │    │  │
                            │  │  │   HPA per deployment, ASG per node group  │    │  │
                            │  │  │   cAdvisor / kubectl top                  │    │  │
                            │  │  └──────┬──────────────┬──────────────┬──────┘    │  │
                            │  │         │ IRSA         │ IRSA         │ IRSA      │  │
                            │  │         ▼              ▼              ▼           │  │
                            │  │  Secrets Manager       S3 (media)     MSK (Kafka) │  │
                            │  │                        + lifecycle                │  │
                            │  └────────────────────────────────────────────────────┘  │
                            │                                                          │
                            │  ECR repos: mikshi-vlm/{backend,auth,chat,                │
                            │             frontend,vlm-processing}                     │
                            │  CloudWatch (metrics + Logs)  ◄── OTel Collector ──┐     │
                            │  SNS topics  ──► email / paging                    │     │
                            │  AWS Budgets ──► cost alerts                       │     │
                            └────────────────────────────────────────────────────┴─────┘

  External SaaS (referenced from EKS):
    LangSmith  ── prompt versioning & registry
    MLflow     ── model / artifact registry
```

---

## 2. Environments

| Env  | Purpose                       | Replication of prod monitoring |
|------|-------------------------------|--------------------------------|
| Dev  | Engineer-driven feature work  | Minimal                        |
| QA   | Pre-prod validation, load test| Yes — same monitoring + alerts |
| Prod | Customer-facing               | Authoritative                  |

CI/CD pipelines exist per environment. QA mirrors prod's CloudWatch dashboards, log retention, OpenTelemetry collectors, and SNS alerts.

---

## 3. Services

Defined in [services.json](../services.json). Each service has its own ECR repo, deployment, optional service account, and IRSA role.

| Service          | Deployment      | IRSA permissions          | Notes                                              |
|------------------|-----------------|---------------------------|----------------------------------------------------|
| svc-backend      | backend         | secrets, s3, msk          | Core API; produces/consumes Kafka                  |
| svc-auth         | auth            | secrets                   | Authentication / token issuance                    |
| svc-chat         | chat            | secrets                   | Realtime chat surface                              |
| svc-frontend     | frontend        | —                         | Static / SSR UI                                    |
| svc-vlm-processing | vlm-processing| secrets, s3, msk          | VLM inference & async processing pipeline          |

IRSA trust + inline policies are generated by [pulumi/__main__.py](../pulumi/__main__.py).

---

## 4. AWS resources (Pulumi-managed)

| Resource                              | Where defined                                        |
|---------------------------------------|------------------------------------------------------|
| ECR repos (per service)               | [pulumi/__main__.py:88](../pulumi/__main__.py#L88)   |
| IAM roles for IRSA                    | [pulumi/__main__.py:106](../pulumi/__main__.py#L106) |
| GitHub OIDC provider                  | [pulumi/__main__.py:120](../pulumi/__main__.py#L120) |
| CI/CD IAM role (GitHub Actions)       | [pulumi/__main__.py:144](../pulumi/__main__.py#L144) |

EKS cluster, VPC, node groups, MSK, ALB, ACM, and Route53 records sit alongside this stack (or in adjacent Pulumi stacks) and follow the same Project tag (`Mikshi-VLM`).

---

## 5. Traffic flow (request path)

1. Client hits `https://<host>` → Route53 → ALB.
2. ALB terminates TLS using an ACM cert (auto-renewed) and forwards to the **nginx** ingress in the cluster.
3. Nginx routes by path/host to one of: `frontend`, `auth`, `chat`, `backend`.
4. `backend` calls `vlm-processing` synchronously for short jobs; long jobs are dispatched via MSK.
5. Services read secrets from AWS Secrets Manager (via IRSA), write media to S3, and emit telemetry through the OTel Collector.

---

## 6. CI/CD flow

1. Push to GitHub → GitHub Actions workflow runs.
2. Workflow assumes `Mikshi-VLM-cicd-role` via OIDC (no static keys).
3. Build image → run unit/integration/smoke tests → ECR push (scan-on-push enabled).
4. Image lifecycle policy on ECR retains the last N tags and expires the rest.
5. Deploy step performs a **blue/green or canary** rollout to EKS.
6. On failure, traffic flips back to the previous color/revision.

---

## 7. Observability flow

```
   Pods ──► OpenTelemetry Collector (DaemonSet)
              │
              ├──► CloudWatch Metrics      (RED / saturation, custom)
              ├──► CloudWatch Logs         (with retention policy)
              └──► (traces backend per OTel config)

   kubelet / cAdvisor ──► kubectl top / CloudWatch Container Insights
                                          │
                                          ▼
                            CloudWatch Alarms ──► SNS topic ──► email / paging
```

QA mirrors this graph end-to-end.

---

## 8. Document index

| Domain         | Detail doc                          |
|----------------|-------------------------------------|
| CI/CD          | [cicd.md](cicd.md)                  |
| Infrastructure | [infrastructure.md](infrastructure.md) |
| Observability  | [observability.md](observability.md) |
| Performance    | [performance.md](performance.md)    |
| Data / ML      | [data-ml.md](data-ml.md)            |
| Process        | [process.md](process.md)            |
