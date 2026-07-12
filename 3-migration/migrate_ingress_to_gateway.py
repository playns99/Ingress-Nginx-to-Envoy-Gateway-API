#!/usr/bin/env python3
"""
migrate_ingress_to_gateway.py
==============================================================================
Reads Ingress objects from the CURRENT kubectl context (or a named context)
and generates, for each Ingress:

  1. A Helm values file for the `gateway-routes` chart (folder 2-helm-chart).
  2. Reference raw manifests (HTTPRoute + any policies) for review.

It is intentionally generic and cloud-agnostic. Set your cluster context first:

    kubectl config use-context <cluster>
    python migrate_ingress_to_gateway.py --output ./out

Only dependency: PyYAML  (pip install pyyaml). `kubectl` must be on PATH.
==============================================================================
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.stderr.write("PyYAML is required. Install it with: pip install pyyaml\n")
    sys.exit(1)


NGINX_PREFIX = "nginx.ingress.kubernetes.io/"


# ------------------------------------------------------------------------------
# kubectl helpers
# ------------------------------------------------------------------------------
def kubectl_get_ingresses(context: Optional[str], namespace: Optional[str]) -> Dict[str, Any]:
    """Return all Ingress objects as parsed JSON."""
    cmd = ["kubectl", "get", "ingress", "-o", "json"]
    if context:
        cmd += ["--context", context]
    if namespace:
        cmd += ["-n", namespace]
    else:
        cmd += ["-A"]

    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(
            "kubectl failed.\n"
            f"Command: {' '.join(cmd)}\n"
            f"Exit code: {result.returncode}\n"
            f"STDERR: {result.stderr.strip()}"
        )
    return json.loads(result.stdout or "{}")


def current_context() -> str:
    result = subprocess.run(
        ["kubectl", "config", "current-context"],
        capture_output=True, text=True, check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown-context"


# ------------------------------------------------------------------------------
# Small conversion utilities
# ------------------------------------------------------------------------------
def sanitize(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "-", value or "").strip("-") or "unknown"


def to_int_port(value: Any) -> Optional[int]:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def is_truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"true", "yes", "1", "on"}


def to_duration(value: str) -> str:
    """Convert an NGINX numeric-seconds value to Envoy duration (e.g. '60' -> '60s')."""
    raw = str(value).strip()
    if not raw:
        return ""
    if re.fullmatch(r"\d+", raw):
        return f"{raw}s"
    if re.fullmatch(r"\d+(ms|s|m|h)", raw.lower()):
        return raw.lower()
    return raw


def nginx_annotations(annotations: Dict[str, str]) -> Dict[str, str]:
    """Return NGINX annotations with the prefix stripped."""
    out: Dict[str, str] = {}
    for key, val in (annotations or {}).items():
        if key.startswith(NGINX_PREFIX):
            out[key[len(NGINX_PREFIX):]] = str(val)
    return out


# ------------------------------------------------------------------------------
# Ingress -> route model
# ------------------------------------------------------------------------------
def collect_paths(spec: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Flatten Ingress rules into (host, path, service, port) rows plus hostnames."""
    rows: List[Dict[str, Any]] = []
    hostnames: List[str] = []

    for rule in spec.get("rules", []) or []:
        host = (rule or {}).get("host", "")
        if host and host not in hostnames:
            hostnames.append(host)
        http = (rule or {}).get("http", {}) or {}
        for p in http.get("paths", []) or []:
            p = p or {}
            svc = ((p.get("backend", {}) or {}).get("service", {}) or {})
            port_obj = svc.get("port", {}) or {}
            rows.append({
                "host": host,
                "path": p.get("path", "/") or "/",
                "pathType": p.get("pathType", "Prefix") or "Prefix",
                "serviceName": svc.get("name", ""),
                "port": to_int_port(port_obj.get("number") or port_obj.get("name")),
            })
    return rows, hostnames


def choose_primary_backend(rows: List[Dict[str, Any]], fallback: str) -> Tuple[str, int]:
    services = [r["serviceName"] for r in rows if r.get("serviceName")]
    ports = [r["port"] for r in rows if isinstance(r.get("port"), int)]
    primary_service = services[0] if services else fallback
    primary_port = ports[0] if ports else 80
    return primary_service, primary_port


def build_rules(rows: List[Dict[str, Any]], ann: Dict[str, str]) -> List[Dict[str, Any]]:
    """Build gateway-routes 'rules' entries from Ingress paths + NGINX annotations."""
    rewrite_target = ann.get("rewrite-target", "")
    ssl_redirect = is_truthy(ann.get("ssl-redirect", "")) or is_truthy(ann.get("force-ssl-redirect", ""))

    rules: List[Dict[str, Any]] = []
    for r in rows:
        if not r.get("serviceName"):
            continue
        path_type = "PathPrefix" if str(r["pathType"]).lower() == "prefix" else "Exact"
        rule: Dict[str, Any] = {
            "matches": [{"path": {"type": path_type, "value": r["path"]}}],
            "backendRefs": [{
                "group": "",
                "kind": "Service",
                "name": r["serviceName"],
                "port": r.get("port") or 80,
                "weight": 1,
            }],
        }
        filters: List[Dict[str, Any]] = []
        if ssl_redirect:
            filters.append({
                "type": "RequestRedirect",
                "requestRedirect": {"scheme": "https", "statusCode": 301},
            })
        if rewrite_target:
            filters.append({
                "type": "URLRewrite",
                "urlRewrite": {"path": {"type": "ReplacePrefixMatch", "replacePrefixMatch": rewrite_target}},
            })
        if filters:
            rule["filters"] = filters
        rules.append(rule)
    return rules


def rules_are_trivial(rules: List[Dict[str, Any]], app_name: str, app_port: int) -> bool:
    """True when a single default '/' rule to the primary backend covers everything."""
    if len(rules) != 1:
        return False
    r = rules[0]
    if r.get("filters"):
        return False
    match = r["matches"][0]["path"]
    backend = r["backendRefs"][0]
    return (
        match["type"] == "PathPrefix"
        and match["value"] == "/"
        and backend["name"] == app_name
        and backend["port"] == app_port
    )


def build_policies(ann: Dict[str, str]) -> Tuple[Dict[str, Any], List[str]]:
    """Map supported NGINX annotations to gateway-routes policy values."""
    policies: Dict[str, Any] = {}
    review: List[str] = []

    backend_spec: Dict[str, Any] = {}

    connect_timeout = to_duration(ann.get("proxy-connect-timeout", ""))
    read_timeout = to_duration(ann.get("proxy-read-timeout", ""))
    send_timeout = to_duration(ann.get("proxy-send-timeout", ""))

    if connect_timeout:
        backend_spec.setdefault("timeout", {}).setdefault("tcp", {})["connectTimeout"] = connect_timeout
    if read_timeout or send_timeout:
        http_to = backend_spec.setdefault("timeout", {}).setdefault("http", {})
        if read_timeout:
            http_to["requestTimeout"] = read_timeout
        if send_timeout:
            http_to["connectionIdleTimeout"] = send_timeout

    # Session affinity via cookie -> consistent hash load balancing.
    cookie_name = ann.get("session-cookie-name", "")
    cookie_ttl = to_duration(ann.get("session-cookie-max-age", ""))
    if is_truthy(ann.get("affinity", "")) or ann.get("affinity", "").lower() == "cookie" or cookie_name:
        ch = backend_spec.setdefault("loadBalancer", {}).setdefault("consistentHash", {})
        ch["type"] = "Cookie"
        cookie: Dict[str, Any] = {}
        if cookie_name:
            cookie["name"] = cookie_name
        if cookie_ttl:
            cookie["ttl"] = cookie_ttl
        if cookie:
            ch["cookie"] = cookie

    if backend_spec:
        policies["backendTraffic"] = {"enabled": True, "spec": backend_spec}

    # Upstream TLS (backend-protocol: HTTPS).
    if ann.get("backend-protocol", "").strip().upper() == "HTTPS":
        policies["backendTLS"] = {"enabled": True}

    # Annotations with no clean native mapping -> flag for manual review.
    needs_review = {
        "proxy-buffer-size", "proxy-buffering", "proxy-buffers-number",
        "proxy-busy-buffers-size", "proxy-request-buffering",
        "configuration-snippet", "server-snippet", "auth-url", "auth-signin",
        "modsecurity-snippet", "rewrite-rule",
    }
    review = sorted(k for k in ann if k in needs_review)
    return policies, review


# ------------------------------------------------------------------------------
# Build the gateway-routes values doc for one Ingress
# ------------------------------------------------------------------------------
def build_values(ingress: Dict[str, Any], shared_domain_suffix: str) -> Tuple[Dict[str, Any], List[str]]:
    meta = ingress.get("metadata", {}) or {}
    spec = ingress.get("spec", {}) or {}
    ann = nginx_annotations(meta.get("annotations", {}) or {})

    name = meta.get("name", "")
    rows, hostnames = collect_paths(spec)
    app_name, app_port = choose_primary_backend(rows, name)

    rules = build_rules(rows, ann)
    policies, review = build_policies(ann)

    # Split hostnames into shared (match suffix) vs custom (everything else).
    shared_hosts, custom_hosts = [], []
    for h in hostnames:
        if shared_domain_suffix and h.endswith(shared_domain_suffix):
            shared_hosts.append(h)
        else:
            custom_hosts.append(h)
    # If no suffix given, treat all as shared.
    if not shared_domain_suffix:
        shared_hosts, custom_hosts = hostnames, []

    gateway: Dict[str, Any] = {"enabled": True}
    if shared_hosts:
        gateway["gatewayName"] = "main-gateway"
        gateway["gatewayNamespace"] = "envoy-gateway-system"
        gateway["hostnames"] = shared_hosts
    if custom_hosts:
        gateway["customHostnames"] = custom_hosts
        gateway["customTlsSecretName"] = f"{app_name}-tls-secret"

    # Only emit rules when they are non-trivial; otherwise rely on chart default.
    if rules and not rules_are_trivial(rules, app_name, app_port):
        gateway["rules"] = rules

    if policies:
        gateway["policies"] = policies

    values: Dict[str, Any] = {
        "application": {"name": app_name},
        "service": {"port": app_port},
        "gatewayAPI": gateway,
    }
    return values, review


# ------------------------------------------------------------------------------
# Raw reference manifests (independent of Helm)
# ------------------------------------------------------------------------------
def render_reference_manifests(values: Dict[str, Any], namespace: str) -> Dict[str, Dict[str, Any]]:
    app = values["application"]["name"]
    gw = values["gatewayAPI"]
    out: Dict[str, Dict[str, Any]] = {}

    parent_refs: List[Dict[str, Any]] = []
    if gw.get("hostnames"):
        parent_refs.append({"name": "main-gateway", "namespace": "envoy-gateway-system"})
    if gw.get("customHostnames"):
        parent_refs.append({"name": f"{app}-gateway", "namespace": namespace})

    hostnames = list(gw.get("hostnames", [])) + list(gw.get("customHostnames", []))

    if gw.get("rules"):
        rules = gw["rules"]
    else:
        rules = [{
            "matches": [{"path": {"type": "PathPrefix", "value": "/"}}],
            "backendRefs": [{
                "group": "", "kind": "Service", "name": app,
                "port": values["service"]["port"], "weight": 1,
            }],
        }]

    httproute = {
        "apiVersion": "gateway.networking.k8s.io/v1",
        "kind": "HTTPRoute",
        "metadata": {"name": app, "namespace": namespace},
        "spec": {"parentRefs": parent_refs, "hostnames": hostnames, "rules": rules},
    }
    out["httproute.yaml"] = httproute

    if gw.get("customHostnames"):
        listeners = [{
            "name": "https-" + sanitize(h).replace(".", "-")[:60],
            "protocol": "HTTPS", "port": 443, "hostname": h,
            "tls": {"mode": "Terminate", "certificateRefs": [
                {"kind": "Secret", "name": gw.get("customTlsSecretName", f"{app}-tls-secret")}]},
            "allowedRoutes": {"namespaces": {"from": "Same"}},
        } for h in gw["customHostnames"]]
        listeners.append({"name": "http", "protocol": "HTTP", "port": 80,
                          "allowedRoutes": {"namespaces": {"from": "Same"}}})
        out["gateway.yaml"] = {
            "apiVersion": "gateway.networking.k8s.io/v1",
            "kind": "Gateway",
            "metadata": {"name": f"{app}-gateway", "namespace": namespace},
            "spec": {"gatewayClassName": "envoy-gateway-class", "listeners": listeners},
        }

    policies = gw.get("policies", {})
    if policies.get("backendTraffic", {}).get("enabled"):
        out["backendtrafficpolicy.yaml"] = {
            "apiVersion": "gateway.envoyproxy.io/v1alpha1",
            "kind": "BackendTrafficPolicy",
            "metadata": {"name": f"{app}-backend-traffic", "namespace": namespace},
            "spec": {
                "targetRefs": [{"group": "gateway.networking.k8s.io", "kind": "HTTPRoute", "name": app}],
                **policies["backendTraffic"].get("spec", {}),
            },
        }
    if policies.get("backendTLS", {}).get("enabled"):
        out["backendtlspolicy.yaml"] = {
            "apiVersion": "gateway.networking.k8s.io/v1alpha3",
            "kind": "BackendTLSPolicy",
            "metadata": {"name": f"{app}-backend-tls", "namespace": namespace},
            "spec": {"targetRefs": [{"group": "", "kind": "Service", "name": app}],
                     **policies["backendTLS"].get("spec", {})},
        }
    return out


# ------------------------------------------------------------------------------
# Main
# ------------------------------------------------------------------------------
def dump_yaml(path: Path, data: Any, header: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(data, sort_keys=False, default_flow_style=False)
    if header:
        text = header + text
    path.write_text(text, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate gateway-routes Helm values and reference manifests from Ingress objects.",
    )
    parser.add_argument("--context", help="kubectl context (default: current context)")
    parser.add_argument("--namespace", help="Limit to a single namespace (default: all namespaces)")
    parser.add_argument("--output", default="./generated", help="Output directory (default: ./generated)")
    parser.add_argument(
        "--shared-domain-suffix", default="",
        help="Hostnames ending with this suffix use the shared gateway; others get a custom Gateway. "
             "Example: .example.com. If empty, all hostnames are treated as shared.",
    )
    args = parser.parse_args()

    context = args.context or current_context()
    out_root = Path(args.output) / sanitize(context)

    print(f"[INFO] Context           : {context}")
    print(f"[INFO] Namespace         : {args.namespace or 'ALL'}")
    print(f"[INFO] Shared suffix      : {args.shared_domain_suffix or '(none - all shared)'}")
    print(f"[INFO] Output directory  : {out_root}")
    print("-" * 74)

    data = kubectl_get_ingresses(context, args.namespace)
    items = data.get("items", []) or []
    if not items:
        print("[WARN] No Ingress objects found.")
        return 0

    summary: List[Dict[str, Any]] = []

    for ing in items:
        meta = ing.get("metadata", {}) or {}
        ns = meta.get("namespace", "default")
        name = meta.get("name", "unknown")

        values, review = build_values(ing, args.shared_domain_suffix)
        app_dir = out_root / sanitize(ns) / sanitize(name)

        header = (
            f"# Generated from Ingress {ns}/{name}\n"
            f"# Context: {context}\n"
            f"# Deploy: helm install {sanitize(name)} <path-to>/gateway-routes -f values.yaml -n {ns}\n"
        )
        dump_yaml(app_dir / "values.yaml", values, header)

        manifests = render_reference_manifests(values, ns)
        for fname, doc in manifests.items():
            dump_yaml(
                app_dir / "manifests" / fname, doc,
                f"# Reference manifest generated from Ingress {ns}/{name}\n",
            )

        if review:
            (app_dir / "MANUAL-REVIEW.txt").write_text(
                "These NGINX annotations have no automatic mapping and need manual review:\n\n"
                + "\n".join(f"  - {NGINX_PREFIX}{a}" for a in review) + "\n",
                encoding="utf-8",
            )

        print(f"[OK]  {ns}/{name} -> {app_dir}"
              + (f"  (manual review: {len(review)})" if review else ""))
        summary.append({"namespace": ns, "ingress": name,
                        "manual_review": review, "output": str(app_dir)})

    dump_yaml(out_root / "_summary.yaml",
              {"context": context, "count": len(summary), "items": summary})
    print("-" * 74)
    print(f"[DONE] Processed {len(summary)} Ingress object(s). Summary: {out_root / '_summary.yaml'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
