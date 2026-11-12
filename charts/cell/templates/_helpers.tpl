{{- define "cell.fullname" -}}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "cell.id" -}}
{{- required "cellId is required (e.g. cell-1)" .Values.cellId -}}
{{- end -}}

{{- define "cell.secretName" -}}
{{- default (include "cell.fullname" .) .Values.secrets.existingSecret -}}
{{- end -}}

{{/* Common labels; call with (dict "root" $ "component" "gateway"). */}}
{{- define "cell.labels" -}}
app.kubernetes.io/name: {{ .component }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
app.kubernetes.io/part-of: cellarch
app.kubernetes.io/managed-by: {{ .root.Release.Service }}
helm.sh/chart: {{ .root.Chart.Name }}-{{ .root.Chart.Version }}
cellarch.io/cell: {{ include "cell.id" .root }}
{{- end -}}

{{- define "cell.selector" -}}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{- define "cell.containerSecurity" -}}
securityContext:
  runAsNonRoot: true
  runAsUser: 10001
  runAsGroup: 10001
  readOnlyRootFilesystem: true
  allowPrivilegeEscalation: false
  capabilities:
    drop: ["ALL"]
  seccompProfile:
    type: RuntimeDefault
{{- end -}}
