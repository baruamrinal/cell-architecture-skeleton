{{- define "platform.edgeConf" -}}
{{- $fullname := include "platform.fullname" . -}}
server {
  listen 8080;
  server_tokens off;
  client_max_body_size 1m;
  proxy_set_header Host $host;
  proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
  proxy_set_header X-Forwarded-Proto $scheme;
  location /auth/ {
    proxy_pass {{ .Values.edge.identityProviderUrl }};
  }
  # Cells' /internal API never leaves the platform (the router refuses it too).
  location /internal {
    default_type application/json;
    return 404 '{"error":"not_found","message":"Not Found"}';
  }
  {{- if .Values.edge.webUiUrl }}
  location /graphql {
    proxy_pass http://{{ $fullname }}-router:{{ .Values.router.service.port }};
  }
  location /v1/ {
    proxy_pass http://{{ $fullname }}-router:{{ .Values.router.service.port }};
  }
  location /_router/ {
    proxy_pass http://{{ $fullname }}-router:{{ .Values.router.service.port }};
  }
  location / {
    proxy_pass {{ .Values.edge.webUiUrl }};
  }
  {{- else }}
  location / {
    proxy_pass http://{{ $fullname }}-router:{{ .Values.router.service.port }};
  }
  {{- end }}
}
{{- end -}}
