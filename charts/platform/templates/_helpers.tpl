{{- define "platform.fullname" -}}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "platform.secretName" -}}
{{- default (include "platform.fullname" .) .Values.secrets.existingSecret -}}
{{- end -}}

{{/* Common labels; call with (dict "root" $ "component" "router"). */}}
{{- define "platform.labels" -}}
app.kubernetes.io/name: {{ .component }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
app.kubernetes.io/part-of: cellarch
app.kubernetes.io/managed-by: {{ .root.Release.Service }}
helm.sh/chart: {{ .root.Chart.Name }}-{{ .root.Chart.Version }}
{{- end -}}

{{- define "platform.selector" -}}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{- define "platform.image" -}}
{{ .root.Values.image.repository }}/{{ .component }}:{{ .root.Values.image.tag }}
{{- end -}}

{{/* Hardened container settings; call with (dict "uid" 10001). */}}
{{- define "platform.containerSecurity" -}}
securityContext:
  runAsNonRoot: true
  runAsUser: {{ .uid }}
  runAsGroup: {{ .uid }}
  readOnlyRootFilesystem: true
  allowPrivilegeEscalation: false
  capabilities:
    drop: ["ALL"]
  seccompProfile:
    type: RuntimeDefault
{{- end -}}

{{- define "platform.secretEnv" -}}
- name: {{ .name }}
  valueFrom:
    secretKeyRef:
      name: {{ include "platform.secretName" .root }}
      key: {{ .key }}
{{- end -}}
