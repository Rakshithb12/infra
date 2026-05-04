# Infrastructure

Covers the three Infra line items from the task sheet.

| # | Item | Tool | Priority | Status   | Owner     |
|---|------|------|----------|----------|-----------|
| 1 | Reverse proxy                          | Nginx                   | p1 | Done     | —         |
| 2 | SSL/TLS certificates with auto-renewal | Let's Encrypt or ACM    | p3 | Not Yet  | —         |
| 3 | Infrastructure as Code                 | Pulumi                  | p1 | On Going | Natarajan |

---

## 1. Nginx — reverse proxy

**Tool:** Nginx (deployed in-cluster as the ingress).

### Role

- Single L7 entry point for all `mikshi-vlm` traffic.
- Path/host routing to `frontend`, `auth`, `chat`, `backend`.
- TLS termination (when ALB is configured to passthrough) or HTTP-only behind an ALB that terminates TLS itself.
- Provides canary weight and header-based routing for the canary rollout strategy described in [cicd.md](cicd.md).

### Layout

```
Client ──► ALB (TLS) ──► nginx Service (LoadBalancer / NodePort) ──► nginx pods
                                                                       │
                                                                       ├─► svc-frontend
                                                                       ├─► svc-auth
                                                                       ├─► svc-chat
                                                                       └─► svc-backend
```

### Ingress configuration shape

One Ingress per public host, all routing through the nginx controller in `mikshi-vlm`:

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: mikshi-vlm
  namespace: mikshi-vlm
  annotations:
    nginx.ingress.kubernetes.io/proxy-body-size: "50m"
    nginx.ingress.kubernetes.io/proxy-read-timeout: "120"
    nginx.ingress.kubernetes.io/ssl-redirect: "true"
spec:
  ingressClassName: nginx
  tls:
    - hosts: [app.mikshi-vlm.example.com]
      secretName: mikshi-vlm-tls
  rules:
    - host: app.mikshi-vlm.example.com
      http:
        paths:
          - path: /api/auth   ; pathType: Prefix ; backend: { service: { name: auth,     port: { number: 80 } } }
          - path: /api/chat   ; pathType: Prefix ; backend: { service: { name: chat,     port: { number: 80 } } }
          - path: /api        ; pathType: Prefix ; backend: { service: { name: backend,  port: { number: 80 } } }
          - path: /           ; pathType: Prefix ; backend: { service: { name: frontend, port: { number: 80 } } }
```

(Path entries shown one-per-line for readability; YAML uses standard list form.)

### Operational notes

- Access logs go to stdout → CloudWatch Logs (see [observability.md](observability.md)).
- Configmap-driven tuning lives in the `nginx-ingress` namespace, not `mikshi-vlm`.

---

## 2. SSL/TLS certificates with auto-renewal

**Tool:** Let's Encrypt or AWS ACM.

### Recommendation by surface

| Surface                                 | Tool                              | Renewal                 |
|-----------------------------------------|-----------------------------------|-------------------------|
| Public ALB (terminates TLS at the edge) | **ACM**                           | Automatic (AWS-managed) |
| Cluster-internal TLS (nginx terminates) | **Let's Encrypt** via cert-manager| Automatic (60 days)     |

Use ACM where possible — no in-cluster moving parts, integrates directly with the ALB. Use Let's Encrypt only if TLS must terminate inside the cluster.

### ACM path

1. `aws.acm.Certificate` provisioned via Pulumi for each public hostname, validation = `DNS`.
2. `aws.route53.Record` created for the validation CNAME.
3. ALB listener references the cert ARN.
4. Renewal is fully automatic; no pipeline action needed.

### Let's Encrypt path (if used)

1. Install cert-manager via Helm in the `cert-manager` namespace.
2. Define a `ClusterIssuer` of type `ACME` with HTTP-01 challenge through the nginx ingress.
3. Add `cert-manager.io/cluster-issuer: letsencrypt-prod` annotation to the Ingress; cert-manager creates and renews the `Secret` named in `tls.secretName`.
4. Renewal happens automatically ~30 days before expiry.

Monitor `certificate.expiry` via CloudWatch (custom metric pushed by a small CronJob) and alert via SNS if `< 14 days`.

---

## 3. Infrastructure as Code — Pulumi

**Tool:** Pulumi (Python).

### Current scope

[pulumi/__main__.py](../pulumi/__main__.py) currently provisions:

- One **ECR repository** per service (`mikshi-vlm/<service>`), with `scan_on_push=True`.
- One **IAM role** per service that requests IRSA, with an inline policy assembled from a token list (`secrets`, `s3`, `msk`).
- The **GitHub OIDC provider** and the **CI/CD IAM role** trusted by it.

Inputs come from Pulumi config:

| Config key       | Purpose                                                        |
|------------------|----------------------------------------------------------------|
| `account_id`     | AWS account                                                    |
| `cluster_name`   | EKS cluster name (used in CI/CD policy + IRSA)                 |
| `oidc_provider`  | EKS OIDC issuer host (no `https://`)                           |
| `github_org`     | GitHub org allowed to assume CI/CD role                        |
| `github_repos`   | List of repos in the org allowed to assume CI/CD role          |

Service list comes from [services.json](../services.json), keeping infra and app metadata in sync.

### IRSA token model

A service in `services.json` declares which permissions it needs by name:

```json
{ "name": "svc-backend", "deployment": "backend", "irsa": ["secrets", "s3", "msk"] }
```

The Pulumi program maps each token to a statement builder (`stmt_secrets`, `stmt_s3`, `stmt_msk`) and combines them into one inline policy. This keeps per-service IAM declarative and reviewable in one place.

### Stack layout

| Stack       | Purpose                                                                 |
|-------------|-------------------------------------------------------------------------|
| `prod`      | Current `Pulumi.prod.yaml`. Production account.                         |
| `qa` *(to add)* | Mirror of `prod` config against the QA account / cluster.           |
| `dev` *(to add)*| Looser policies, single shared cluster.                            |

### Operating Pulumi

```powershell
# Login to backend (S3 or Pulumi Cloud)
pulumi login s3://<state-bucket>

# Select stack
pulumi stack select prod

# Preview / apply
pulumi preview
pulumi up
```

State backend, locking, and CI runs (Pulumi Automation API or `pulumi up` from GitHub Actions) are out of scope for this doc but should be added under the same `Mikshi-VLM-cicd-role` (with extra IAM for the resources Pulumi manages).

### Outputs

The program exports:

- `<service>_role_arn` — to wire into each service's `ServiceAccount` annotation.
- `cicd_role_arn` — to register as a GitHub Actions secret/variable.

### Future additions (still Pulumi)

- VPC, EKS cluster, node groups.
- ALB + ACM cert + Route53 record.
- MSK cluster.
- S3 buckets with lifecycle (see [data-ml.md](data-ml.md)).
- ECR lifecycle policies (see [cicd.md](cicd.md)).
- CloudWatch log groups with retention (see [observability.md](observability.md)).
- SNS topics for alerting (see [observability.md](observability.md)).
- AWS Budgets (see [process.md](process.md)).
