# 3 - Ingress → Gateway API migration

`migrate_ingress_to_gateway.py` reads the **Ingress** objects in a cluster and,
for each one, generates:

1. A **Helm values file** for the `gateway-routes` chart (folder `2-helm-chart`).
2. **Reference raw manifests** (`HTTPRoute`, `Gateway`, policies) for review.
3. A `MANUAL-REVIEW.txt` listing NGINX annotations with no automatic mapping.

It uses your **current kubectl context** (or `--context`) and does not modify
the cluster — it only reads Ingresses and writes files.

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
            ├── values.yaml               # feed this to the gateway-routes chart
            ├── MANUAL-REVIEW.txt          # only if unmapped annotations exist
            └── manifests/                # reference-only raw manifests
                ├── httproute.yaml
                ├── gateway.yaml           # only for custom domains
                ├── backendtrafficpolicy.yaml
                └── backendtlspolicy.yaml
```

## Deploy the generated values

```bash
helm install <app> ../2-helm-chart/gateway-routes \
  -f generated/<context>/<namespace>/<app>/values.yaml \
  -n <namespace>
```

Preview first without touching the cluster:

```bash
helm template <app> ../2-helm-chart/gateway-routes \
  -f generated/<context>/<namespace>/<app>/values.yaml \
  -n <namespace>
```

## What gets converted

| NGINX annotation | Mapped to |
|------------------|-----------|
| `proxy-connect-timeout` | `BackendTrafficPolicy.timeout.tcp.connectTimeout` |
| `proxy-read-timeout` | `BackendTrafficPolicy.timeout.http.requestTimeout` |
| `proxy-send-timeout` | `BackendTrafficPolicy.timeout.http.connectionIdleTimeout` |
| `backend-protocol: HTTPS` | `BackendTLSPolicy` (TLS to upstream) |
| `affinity: cookie` / `session-cookie-name` | `BackendTrafficPolicy.loadBalancer.consistentHash` (Cookie) |
| `ssl-redirect` / `force-ssl-redirect` | `HTTPRoute` `RequestRedirect` filter |
| `rewrite-target` | `HTTPRoute` `URLRewrite` filter |
| Ingress rules (host/path/service) | `HTTPRoute` hostnames + rules |

### Intentionally NOT converted

- **Body size / buffering** (`proxy-body-size`, `proxy-buffering`, ...): Envoy
  streams by default; no size cap is needed for most apps. These are flagged for
  manual review rather than auto-mapped.
- **Snippets** (`configuration-snippet`, `server-snippet`, `modsecurity-snippet`)
  and **auth** annotations: no safe automatic mapping — flagged for manual review.

## Recommended workflow

1. Run the script against the source cluster.
2. Review each `values.yaml` and any `MANUAL-REVIEW.txt`.
3. `helm template` to preview, then `helm install` per app.
4. Validate the `HTTPRoute` status (`Accepted=True`, `ResolvedRefs=True`).
5. Cut over DNS / traffic, then decommission the old Ingress.
