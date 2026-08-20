{{- define "recsys-feature-rag-mcp.labels" -}}
app.kubernetes.io/name: {{ .Values.name }}
app.kubernetes.io/component: mcp-server
app.kubernetes.io/part-of: recsys-agentic
{{- end -}}
