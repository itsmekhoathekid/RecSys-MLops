{{- define "recsys-recommendation-mcp.labels" -}}
app.kubernetes.io/name: {{ .Values.name }}
app.kubernetes.io/component: recommendation-mcp
app.kubernetes.io/part-of: recsys-agentic
{{- end -}}
