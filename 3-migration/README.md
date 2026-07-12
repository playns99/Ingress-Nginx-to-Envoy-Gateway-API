# 3 - Ingress → Gateway API migration

`migrate_ingress_to_gateway.py` migrates NGINX **Ingress** objects to **Envoy
Gateway (Gateway API)** resources. It is **read-only against the cluster**: it
never creates, changes, or deletes anything in Kubernetes — it only reads
Ingresses and writes files locally. You choose what to deploy, and how.

## What the script does — 3 steps

**Step 1 — Extract.** It pulls every Ingress object from your current kubectl
context (or `--context <name>`) via `kubectl get ingress -o json`, across all
namespaces (or one, with `--namespace`). Each Ingress is saved as
`ingress-original.yaml` next to its generated files, so you always have the
source object for review, diffing, and rollback.

**Step 2 — Generate raw manifests.** For each Ingress it writes ready-to-apply
Gateway API manifests under `manifests/`:

| File | When it is generated |
|------|----------------------|
| `httproute.yaml` | Always — hostnames, path rules, rewrites, backends |
| `gateway.yaml` | Only if the app has custom (non-shared) domains |
| `httproute-redirect.yaml` | Only for custom domains with `ssl-redirect` — HTTP→HTTPS redirect pinned to the custom gateway's `http` listener |
| `backendtrafficpolicy.yaml` | Only if timeout / session-affinity annotations exist |
| `backendtlspolicy*.yaml` | Only for `backend-protocol: HTTPS` — one per backend Service |

**Step 3 — Generate Helm values.** For each Ingress it also writes a
`values.yaml` for the `gateway-routes` chart (folder `2-helm-chart`), which
renders the same resources as the raw manifests.

## Choose your deployment path

Both paths produce the same Gateway API resources — pick per app:

**Option A — Helm (recommended).** Deploy through the `gateway-routes` chart.
You get upgrades, rollback, and uninstall per app, and the same chart is how
you create **future routes for new apps** — write a small `values.yaml` by
hand (see `2-helm-chart/gateway-routes/examples/`), no migration script
needed.

```bash
# Preview without touching the cluster
helm template <app> ../2-helm-chart/gateway-routes \
  -f generated/<context>/<namespace>/<app>/values.yaml -n <namespace>

# Deploy
helm install <app> ../2-helm-chart/gateway-routes \
  -f generated/<context>/<namespace>/<app>/values.yaml -n <namespace>
```

**Option B — Raw manifests.** Apply the generated YAML directly. Good for a
quick review-and-apply migration or for teams that manage YAML in GitOps
without Helm:

```bash
# Server-side dry run first
kubectl apply --dry-run=server -f generated/<context>/<namespace>/<app>/manifests/

kubectl apply -f generated/<context>/<namespace>/<app>/manifests/
```

> Pick ONE option per app — applying both would create duplicate routes.

## Prerequisites

- Python 3.8+
- `kubectl` on PATH, pointed at the source cluster
- `pip install -r requirements.txt` (installs PyYAML)

```bash
kubectl config use-context <source-cluster>
pip install -r requirements.txt
```

## Usage

```bash
# All namespaces, current context
python migrate_ingress_to_gateway.py --output ./generated

# One namespace
python migrate_ingress_to_gateway.py --namespace shop --output ./generated

# Pick a context explicitly
python migrate_ingress_to_gateway.py --context prod-cluster --output ./generated

# Split shared vs custom domains: hostnames ending in .example.com attach to the
# shared gateway; everything else gets its own custom-domain Gateway.
python migrate_ingress_to_gateway.py \
  --shared-domain-suffix .example.com \
  --output ./generated
```

## Output layout

```
generated/
└── <context>/
    ├── _summary.yaml                     # index of everything generated
    └── <namespace>/
        └── <ingress-name>/
            ├── ingress-original.yaml     # STEP 1: source Ingress (audit/rollback)
            ├── values.yaml               # STEP 3: feed this to the gateway-routes chart
            ├── MANUAL-REVIEW.txt          # unmapped annotations + migration notes
            └── manifests/                # STEP 2: ready-to-apply raw manifests
                ├── httproute.yaml
                ├── gateway.yaml               # custom domains only
                ├── httproute-redirect.yaml    # custom domains with ssl-redirect only
                ├── backendtrafficpolicy.yaml  # timeouts/affinity only
                └── backendtlspolicy*.yaml     # backend-protocol HTTPS only
```

## What gets converted

| NGINX annotation | Mapped to |
|------------------|-----------|
| Ingress rules (host/path/service) | `HTTPRoute` hostnames + rules |
| `rewrite-target` | `HTTPRoute` `URLRewrite` filter |
| `proxy-connect-timeout` | `BackendTrafficPolicy.timeout.tcp.connectTimeout` |
| `proxy-read-timeout` | `BackendTrafficPolicy.timeout.http.requestTimeout` |
| `proxy-send-timeout` | `BackendTrafficPolicy.timeout.http.connectionIdleTimeout` |
| `backend-protocol: HTTPS` | `BackendTLSPolicy` per backend Service (system CAs + service hostname — see MANUAL-REVIEW notes if upstream certs are self-signed) |
| `affinity: cookie` / `session-cookie-name` | `BackendTrafficPolicy.loadBalancer.consistentHash` (Cookie) |
| `ssl-redirect` / `force-ssl-redirect` | Shared domains: nothing to do — the platform `http-to-https-redirect` route on `main-gateway` already covers them. Custom domains: `manifests/httproute-redirect.yaml` |

### Intentionally NOT converted

- **Body size / buffering** (`proxy-body-size`, `proxy-buffering`, ...): Envoy
  streams by default; no size cap is needed for most apps. These are flagged for
  manual review rather than auto-mapped.
- **Snippets** (`configuration-snippet`, `server-snippet`, `modsecurity-snippet`)
  and **auth** annotations: no safe automatic mapping — flagged for manual review.

## Recommended workflow

1. Run the script against the source cluster.
2. Per app: review `ingress-original.yaml` vs the generated output, plus any
   `MANUAL-REVIEW.txt` notes.
3. Choose Option A (Helm) or Option B (raw manifests) and preview with
   `helm template` / `kubectl apply --dry-run=server`.
4. Deploy, then validate the `HTTPRoute` status (`Accepted=True`,
   `ResolvedRefs=True`).
5. Cut over DNS / traffic, then decommission the old Ingress.
6. For **new apps after the migration**, skip the script — copy an example from
   `2-helm-chart/gateway-routes/examples/` and deploy with Helm.
