{{/*
Application name. Defaults to the Helm release name if application.name is unset.
*/}}
{{- define "gateway-routes.appName" -}}
{{- default .Release.Name .Values.application.name -}}
{{- end -}}

{{/*
Namespace. Defaults to the release namespace.
*/}}
{{- define "gateway-routes.namespace" -}}
{{- default .Release.Namespace .Values.application.namespace -}}
{{- end -}}

{{/*
Chart name-version label value.
*/}}
{{- define "gateway-routes.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Common labels applied to every rendered resource.
*/}}
{{- define "gateway-routes.labels" -}}
helm.sh/chart: {{ include "gateway-routes.chart" . }}
app.kubernetes.io/name: {{ include "gateway-routes.appName" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
{{- end -}}

{{/*
The HTTPRoute name used across route and policy targetRefs.
*/}}
{{- define "gateway-routes.routeName" -}}
{{- default (include "gateway-routes.appName" .) .Values.gatewayAPI.routeName -}}
{{- end -}}

{{/*
The custom (per-app) Gateway name.
*/}}
{{- define "gateway-routes.customGatewayName" -}}
{{- default (printf "%s-gateway" (include "gateway-routes.appName" .)) .Values.gatewayAPI.customGatewayName -}}
{{- end -}}
