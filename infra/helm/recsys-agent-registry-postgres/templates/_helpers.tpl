{{- define "recsys-agent-registry-postgres.name" -}}
agentregistry-postgres
{{- end }}

{{- define "recsys-agent-registry-postgres.labels" -}}
app.kubernetes.io/name: {{ include "recsys-agent-registry-postgres.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: database
app.kubernetes.io/part-of: agentregistry
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

