{{- define "stack.tenant" -}}
{{- $id := required "tenantId is required (e.g. acme)" .Values.tenantId -}}
{{- if not (regexMatch "^[a-z0-9][a-z0-9-]{1,62}$" $id) -}}
{{- fail (printf "tenantId %q must be 2-63 lowercase letters, digits and hyphens" $id) -}}
{{- end -}}
{{- $id -}}
{{- end -}}

{{/* Name the gateway looks for: tenant-<id>-<service> (TENANT_SERVICE_URL). */}}
{{- define "stack.name" -}}
{{- printf "tenant-%s-%s" (include "stack.tenant" .root) .service | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* Common labels; call with (dict "root" $ "service" "user-account"). */}}
{{- define "stack.labels" -}}
app.kubernetes.io/name: {{ .service }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .service }}
app.kubernetes.io/part-of: cellarch
app.kubernetes.io/managed-by: {{ .root.Release.Service }}
helm.sh/chart: {{ .root.Chart.Name }}-{{ .root.Chart.Version }}
cellarch.io/tenant: {{ include "stack.tenant" .root }}
cellarch.io/role: tenant-service
{{- end -}}

{{- define "stack.selector" -}}
cellarch.io/tenant: {{ include "stack.tenant" .root }}
app.kubernetes.io/component: {{ .service }}
{{- end -}}
