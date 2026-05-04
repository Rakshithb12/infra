# CI/CD

Covers the seven CI/CD line items from the task sheet.

| # | Item | Tool | Priority | Status     | Owner     |
|---|------|------|----------|------------|-----------|
| 1 | CI/CD pipeline for Dev                       | GitHub Actions | —  | Done     | —         |
| 2 | CI/CD pipeline for QA                        | GitHub Actions | p1 | Not Yet  | —         |
| 3 | CI/CD pipeline for Prod                      | GitHub Actions | p1 | On Going | Natarajan |
| 4 | Pipeline testing & validation                | GitHub Actions jobs | p2 | Not Yet | — |
| 5 | Rollback strategy — blue/green or canary     | EKS Deployment strategy | p3 | On Going | — |
| 6 | Container image scanning in pipeline         | ECR scan-on-push | p3 | Not Yet | — |
| 7 | Image registry lifecycle                     | ECR lifecycle policy | p2 | On Going | — |

---

## 1–3. CI/CD pipelines (Dev, QA, Prod)

**Tool:** GitHub Actions, authenticated to AWS via OIDC.

### How it works

- The GitHub OIDC provider and the `Mikshi-VLM-cicd-role` IAM role are provisioned by Pulumi:
  - OIDC provider: [pulumi/__main__.py:120](../pulumi/__main__.py#L120)
  - CI/CD role: [pulumi/__main__.py:144](../pulumi/__main__.py#L144)
- The role's trust policy restricts assumption to repos under `${github_org}` listed in `github_repos` (Pulumi config).
- The role's inline policy grants:
  - ECR auth + push (`GetAuthorizationToken`, `BatchCheckLayerAvailability`, `InitiateLayerUpload`, `UploadLayerPart`, `CompleteLayerUpload`, `PutImage`, …).
  - EKS describe (`DescribeCluster`, `ListClusters`) on the configured cluster ARN.

### Workflow shape (per service)

One reusable workflow consumed per service in `services.json`:

```yaml
# .github/workflows/deploy.yml
on:
  push:
    branches: [main]            # prod
    paths: ['<service-path>/**']
  pull_request:                 # dev / preview
permissions:
  id-token: write               # required for OIDC
  contents: read
jobs:
  build-test-deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: arn:aws:iam::${{ vars.AWS_ACCOUNT_ID }}:role/Mikshi-VLM-cicd-role
          aws-region: ${{ vars.AWS_REGION }}
      - run: <unit + integration tests>
      - uses: aws-actions/amazon-ecr-login@v2
      - run: docker build -t $ECR/mikshi-vlm/<svc>:$SHA .
      - run: docker push  $ECR/mikshi-vlm/<svc>:$SHA
      - run: aws eks update-kubeconfig --name $CLUSTER
      - run: kubectl set image deploy/<deployment> <ctr>=$ECR/mikshi-vlm/<svc>:$SHA -n mikshi-vlm
      - run: kubectl rollout status deploy/<deployment> -n mikshi-vlm --timeout=5m
      - run: <smoke tests>
```

### Per-environment differences

| Setting               | Dev                  | QA                   | Prod                          |
|-----------------------|----------------------|----------------------|-------------------------------|
| Trigger               | PR / feature branch  | merge to `qa`        | merge to `main`               |
| Cluster               | dev EKS              | qa EKS               | prod EKS                      |
| Approval              | None                 | None                 | GitHub Environment "prod" with required reviewer |
| Rollout strategy      | Rolling              | Blue/green or canary | Blue/green or canary          |
| Smoke tests           | Required             | Required + load smoke| Required                      |

---

## 4. Pipeline testing & validation

Three test stages run inside the workflow before any image is promoted:

| Stage          | Where                                   | Pass criteria              |
|----------------|-----------------------------------------|----------------------------|
| Unit           | Inside the build container, pre-image   | All tests green            |
| Integration    | `docker compose` or testcontainers job  | All tests green            |
| Smoke          | Against the freshly deployed pod, post-rollout | Health endpoint 200 + key user journey |

Services with `tests: true` in [services.json](../services.json) (`backend`, `auth`, `chat`, `vlm-processing`) require all three. `frontend` has `tests: false` and runs only smoke + build.

A failure at any stage:

- Pre-deploy stage → workflow fails, no image pushed.
- Post-deploy smoke fail → automatic rollback (see §5).

---

## 5. Rollback strategy — blue/green or canary

**Tool:** Native Kubernetes Deployment strategies on EKS.

Two supported patterns; pick per service:

### Blue/Green

- Two Deployments (`<svc>-blue`, `<svc>-green`) behind one Service.
- CI deploys to the inactive color, runs smoke tests, then flips the Service `selector`.
- Rollback = flip `selector` back. Old pods remain warm for fast revert.

### Canary

- Single Deployment with `strategy.type: RollingUpdate` and tight `maxSurge`/`maxUnavailable`, OR a second small Deployment receiving a fraction of traffic via the nginx ingress (`nginx.ingress.kubernetes.io/canary: "true"`, `canary-weight`).
- CI gradually increases the canary weight (e.g. 5 → 25 → 100) with a wait + smoke check between steps.
- Rollback = set canary weight to 0 and delete the canary Deployment.

Choice:
- **frontend, auth, chat** → canary via nginx ingress weight.
- **backend, vlm-processing** → blue/green (stateful Kafka consumer groups make traffic-splitting awkward).

---

## 6. Container image scanning in pipeline (ECR scan)

**Tool:** Amazon ECR scan-on-push.

Already enabled per repo in Pulumi:

```python
# pulumi/__main__.py:88
aws.ecr.Repository(
    f"ecr-{name}",
    name=f"mikshi-vlm/{name}",
    image_scanning_configuration=aws.ecr.RepositoryImageScanningConfigurationArgs(scan_on_push=True),
    ...
)
```

### Pipeline integration

After `docker push`, the workflow polls findings and fails on critical/high vulnerabilities:

```yaml
- name: Wait for ECR scan and gate on findings
  run: |
    aws ecr wait image-scan-complete \
      --repository-name mikshi-vlm/<svc> \
      --image-id imageTag=$SHA
    findings=$(aws ecr describe-image-scan-findings \
      --repository-name mikshi-vlm/<svc> \
      --image-id imageTag=$SHA \
      --query 'imageScanFindings.findingSeverityCounts' \
      --output json)
    echo "$findings"
    crit=$(echo "$findings" | jq '.CRITICAL // 0')
    high=$(echo "$findings" | jq '.HIGH // 0')
    [ "$crit" -eq 0 ] && [ "$high" -eq 0 ]
```

Failure stops the deploy. Findings are attached to the workflow run summary.

---

## 7. Image registry lifecycle — tag strategy + cleanup

**Tool:** ECR lifecycle policies + tag conventions.

### Tag strategy

| Tag form               | Source         | Retention                    |
|------------------------|----------------|------------------------------|
| `<git-sha>`            | Every build    | Last 30 untagged-by-env      |
| `dev-<sha>`            | Dev build      | Last 20                      |
| `qa-<sha>`             | QA promotion   | Last 20                      |
| `prod-<sha>`           | Prod promotion | Last 50                      |
| `prod-latest`          | Mutable alias  | Always 1                     |

The deployed image in EKS is always referenced by immutable `<env>-<sha>` — never `latest`.

### Lifecycle policy (per repo)

```json
{
  "rules": [
    { "rulePriority": 1,
      "description": "Keep last 50 prod-tagged images",
      "selection": { "tagStatus": "tagged", "tagPrefixList": ["prod-"], "countType": "imageCountMoreThan", "countNumber": 50 },
      "action": { "type": "expire" } },
    { "rulePriority": 2,
      "description": "Keep last 20 qa-tagged images",
      "selection": { "tagStatus": "tagged", "tagPrefixList": ["qa-"], "countType": "imageCountMoreThan", "countNumber": 20 },
      "action": { "type": "expire" } },
    { "rulePriority": 3,
      "description": "Keep last 20 dev-tagged images",
      "selection": { "tagStatus": "tagged", "tagPrefixList": ["dev-"], "countType": "imageCountMoreThan", "countNumber": 20 },
      "action": { "type": "expire" } },
    { "rulePriority": 4,
      "description": "Expire untagged after 7 days",
      "selection": { "tagStatus": "untagged", "countType": "sinceImagePushed", "countUnit": "days", "countNumber": 7 },
      "action": { "type": "expire" } }
  ]
}
```

This will be added to [pulumi/__main__.py](../pulumi/__main__.py) as `aws.ecr.LifecyclePolicy` per repo.
