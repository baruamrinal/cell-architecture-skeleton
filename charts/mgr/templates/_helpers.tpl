{{- define "mgr.cell" -}}
{{- required "cellId is required (e.g. cell-1)" .Values.cellId -}}
{{- end -}}

{{- define "mgr.labels" -}}
app.kubernetes.io/name: mgr
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: mgr
app.kubernetes.io/part-of: cellarch
app.kubernetes.io/version: {{ .Values.image.tag | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
cellarch.io/cell: {{ include "mgr.cell" . }}
{{- end -}}

{{- define "mgr.selector" -}}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: mgr
{{- end -}}
