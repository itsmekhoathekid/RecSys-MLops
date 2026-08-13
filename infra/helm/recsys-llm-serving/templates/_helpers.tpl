{{- define "recsys-llm-serving.name" -}}
qwen35-gguf
{{- end }}

{{- define "recsys-llm-serving.labels" -}}
app.kubernetes.io/name: {{ include "recsys-llm-serving.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: recsys-llm-inference
llm-d.ai/guide: optimized-baseline
llm-d.ai/model: qwen35-0-8b-llamacpp
{{- end }}

{{- define "recsys-llm-serving.selectorLabels" -}}
app.kubernetes.io/name: {{ include "recsys-llm-serving.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
llm-d.ai/guide: optimized-baseline
{{- end }}

{{- define "recsys-llm-serving.image" -}}
{{- if .Values.image.digest -}}
{{ .Values.image.repository }}@{{ .Values.image.digest }}
{{- else -}}
{{ .Values.image.repository }}:{{ .Values.image.tag }}
{{- end -}}
{{- end }}
