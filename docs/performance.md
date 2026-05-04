# Performance

Covers the three Performance line items from the task sheet.

| # | Item | Tool | Priority | Owner     |
|---|------|------|----------|-----------|
| 1 | Load testing                                  | Locust / JMeter             | p2 | natarajan |
| 2 | Container optimization                        | Multi-stage builds + slim base + resource limits/requests | p3 | developer |
| 3 | Auto-scaling policies tuned to load test data | HPA (K8s) + ASG (EC2)       | —  | Natarajan |

The three items form one workflow: optimize containers → load test → tune autoscaling using the results.

---

## 1. Load testing — Locust / JMeter

**Tool:** Locust *or* JMeter — pick one per scenario.

| Scenario type                            | Tool to use      | Reason                                         |
|------------------------------------------|------------------|------------------------------------------------|
| HTTP API stress, scriptable user flows   | **Locust**       | Python scripts, easy to version with services  |
| Pre-existing JMX scripts, complex protocols (SOAP, JDBC) | **JMeter** | Mature ecosystem, GUI for non-engineers   |

For a Python-first stack like Mikshi-VLM, default to **Locust**.

### Where load tests live

```
loadtests/
  backend/
    locustfile.py
    scenarios/
      chat_burst.py
      vlm_inference.py
  README.md
```

Tests are versioned alongside services and triggered manually (`locust -f loadtests/backend/locustfile.py --host https://qa.mikshi-vlm.example.com`) or from a dedicated GitHub Actions workflow against QA.

### Locust skeleton

```python
# loadtests/backend/locustfile.py
from locust import HttpUser, task, between

class BackendUser(HttpUser):
    wait_time = between(0.5, 2.0)

    def on_start(self):
        self.client.post("/api/auth/login", json={"u": "loadtest", "p": "..."})

    @task(3)
    def list_items(self):
        self.client.get("/api/items")

    @task(1)
    def submit_job(self):
        self.client.post("/api/jobs", json={"prompt": "ping"})
```

Run profile per cycle:

| Phase   | Users  | Duration | Goal                                  |
|---------|--------|----------|---------------------------------------|
| Warmup  | 50     | 2 min    | Fill caches, prime HPA                |
| Steady  | 500    | 10 min   | Capture baseline RED + p95            |
| Burst   | 2000   | 5 min    | Trigger HPA scale-out                 |
| Soak    | 200    | 60 min   | Catch leaks (memory growth, FD leaks) |

### Targets

Always run load tests against **QA** (which mirrors prod per [observability.md](observability.md) §6). Never against prod.

### What to capture per run

- `requests/sec`, p50 / p95 / p99 latency, error rate (Locust UI / CSV).
- HPA scale events (`kubectl get hpa -n mikshi-vlm -w`).
- Per-pod CPU/memory peak (CloudWatch Container Insights).
- ALB 5xx rate and latency (CloudWatch).
- MSK consumer lag for `backend` and `vlm-processing`.

Results inform §3 (autoscaling tuning) and §2 (right-sizing).

---

## 2. Container optimization

**Tool set:** multi-stage Docker builds, slim base images, Kubernetes resource `requests` / `limits`.

### Multi-stage builds

Each service Dockerfile splits build vs. runtime:

```dockerfile
# ---- build stage ----
FROM python:3.12 AS build
WORKDIR /app
COPY requirements.txt .
RUN pip install --prefix=/install -r requirements.txt
COPY . .

# ---- runtime stage ----
FROM python:3.12-slim
WORKDIR /app
COPY --from=build /install /usr/local
COPY --from=build /app /app
USER 10001:10001
ENTRYPOINT ["python", "-m", "app"]
```

Outcomes: smaller image, no compilers in runtime layer, reproducible installs.

### Slim base images

| Service         | Base image                  | Reason                                   |
|-----------------|-----------------------------|------------------------------------------|
| backend         | `python:3.12-slim`          | Python service, no GPU                   |
| auth            | `python:3.12-slim`          | Same                                     |
| chat            | `python:3.12-slim`          | Same                                     |
| frontend        | `node:20-alpine` build → `nginx:alpine` runtime | Static SSR/CSR output |
| vlm-processing  | `nvidia/cuda:<ver>-cudnn-runtime-ubuntu22.04` | Needs CUDA runtime; `-runtime` not `-devel` |

Avoid `latest` tags. Pin to digest in CI for reproducibility (`FROM python:3.12-slim@sha256:...`).

### Resource requests/limits

Every container in `mikshi-vlm` MUST declare `resources.requests` (so the scheduler can place it) and `resources.limits` (so a runaway pod can't take down a node).

```yaml
resources:
  requests:
    cpu: "200m"
    memory: "256Mi"
  limits:
    cpu: "1000m"
    memory: "1Gi"
```

Numbers are derived from §1 load tests:

- `requests.cpu` / `requests.memory` ≈ steady-state observed via `kubectl top pod`.
- `limits.memory` ≈ peak × 1.5 (avoids OOMKill on spikes).
- `limits.cpu` ≈ 2× steady-state for CPU-bound services; **omit** for latency-sensitive frontends to avoid throttling.

A `LimitRange` in the `mikshi-vlm` namespace enforces defaults so no pod ships without limits.

---

## 3. Auto-scaling — HPA (K8s) + ASG (EC2)

Two layers, tuned together with the load-test data from §1.

### HPA — pod-level scaling

One HPA per Deployment. Scales on CPU and on a custom metric where appropriate (e.g., MSK consumer lag for `vlm-processing`).

```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: backend
  namespace: mikshi-vlm
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: backend
  minReplicas: 3
  maxReplicas: 30
  metrics:
    - type: Resource
      resource:
        name: cpu
        target: { type: Utilization, averageUtilization: 60 }
  behavior:
    scaleUp:
      policies: [{ type: Percent, value: 100, periodSeconds: 60 }]
    scaleDown:
      stabilizationWindowSeconds: 300
      policies: [{ type: Percent, value: 25, periodSeconds: 60 }]
```

Per-service starting points (refined by load tests):

| Service         | min | max | Trigger metric             |
|-----------------|-----|-----|----------------------------|
| backend         | 3   | 30  | CPU 60%                    |
| auth            | 2   | 10  | CPU 60%                    |
| chat            | 2   | 20  | CPU 60% + active connections (custom) |
| frontend        | 2   | 10  | CPU 60%                    |
| vlm-processing  | 2   | 50  | CPU 70% **and** MSK consumer lag |

Custom metrics (consumer lag, active connections) are exposed by the OTel Collector to CloudWatch and pulled into the HPA via the **CloudWatch Metrics Adapter** (`k8s-cloudwatch-adapter`).

### ASG — node-level scaling

EKS managed node groups are backed by EC2 Auto Scaling Groups. Two patterns:

| Mechanism           | Use case                                             |
|---------------------|------------------------------------------------------|
| **Cluster Autoscaler** | Conservative; respects ASG min/max strictly.       |
| **Karpenter**       | Faster provisioning, bin-packs better, picks instance type per pod. |

Either is acceptable; pick one per cluster. The ASGs themselves should be defined per workload class:

| Node group          | Instance family                | Purpose                              |
|---------------------|--------------------------------|--------------------------------------|
| `general`           | `m6i.large` / `m6i.xlarge`     | `frontend`, `auth`, `chat`, `backend`|
| `gpu`               | `g5.xlarge` / `g5.2xlarge`     | `vlm-processing` only                |
| `system`            | `t3.medium`                    | `kube-system`, observability agents  |

Pods are pinned to node groups via `nodeSelector` / taints — `vlm-processing` tolerates `gpu=true:NoSchedule`, others don't, so GPU nodes are reserved.

### Tuning workflow

1. Run a Locust burst against QA (§1).
2. Watch HPA: `kubectl get hpa -n mikshi-vlm -w`.
3. If pods scale but nodes don't keep up → ASG min too low, or Karpenter provisioner too slow.
4. If HPA never triggers but latency degrades → trigger metric is wrong (CPU isn't the bottleneck); add a custom metric.
5. If pods scale up and immediately scale down → tune `behavior.scaleDown.stabilizationWindowSeconds`.
6. Lock in the tuned values in code, re-run the test, confirm SLO met.
