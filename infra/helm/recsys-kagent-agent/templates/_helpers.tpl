{{- define "recsys-kagent-agent.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | quote }}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: recsys-mlops
{{- end }}

{{/*
Resolve the Feature/RAG MCP endpoint and credential reference as one value.

The optional shared manifest is intentionally not part of this chart's default
values. Callers can pass it to every MCP and agent release with an additional
`-f configs/agentic/mcp-auth-versions.yaml`. When it is absent, the chart keeps
the legacy .Values.mcp.url/.Values.mcp.authSecret behavior.
*/}}
{{- define "recsys-kagent-agent.mcpAuthRotation" -}}
{{- $legacyDomains := list -}}
{{- range (.Values.sandbox.allowedDomains | default (list)) -}}
  {{- if not (has . $legacyDomains) -}}
    {{- $legacyDomains = append $legacyDomains . -}}
  {{- end -}}
{{- end -}}
{{- $resolved := dict
      "enabled" false
      "revision" "legacy"
      "url" ""
      "secretName" ""
      "allowedDomains" $legacyDomains -}}
{{- $services := .Values.services | default (dict) -}}
{{- $service := get $services "featureRag" | default (dict) -}}
{{- if gt (len $service) 0 -}}
  {{- $chartNamespace := required "namespace is required" .Values.namespace | toString -}}
  {{- $serviceNamespace := required "services.featureRag.namespace is required" (get $service "namespace") | toString -}}
  {{- if ne $serviceNamespace "kagent" -}}
    {{- fail (printf "services.featureRag.namespace must be kagent, got %q" $serviceNamespace) -}}
  {{- end -}}
  {{- if ne $serviceNamespace $chartNamespace -}}
    {{- fail (printf "services.featureRag.namespace %q must match chart namespace %q" $serviceNamespace $chartNamespace) -}}
  {{- end -}}
  {{- $manifestVersion := required "version is required when services.featureRag is configured" .Values.version | toString -}}
  {{- if ne $manifestVersion "1" -}}
    {{- fail (printf "unsupported MCP auth rotation manifest version %q; expected 1" $manifestVersion) -}}
  {{- end -}}
  {{- $activeRevision := required "services.featureRag.activeRevision is required" (get $service "activeRevision") | toString -}}
  {{- $revisions := get $service "revisions" | default (dict) -}}
  {{- if not (hasKey $revisions $activeRevision) -}}
    {{- fail (printf "services.featureRag.activeRevision %q does not exist in services.featureRag.revisions" $activeRevision) -}}
  {{- end -}}
  {{- $active := get $revisions $activeRevision -}}
  {{- if not (get $active "deploy" | default false) -}}
    {{- fail (printf "services.featureRag.revisions.%s.deploy must be true while it is active" $activeRevision) -}}
  {{- end -}}
  {{- $workloadName := required (printf "services.featureRag.revisions.%s.workloadName is required" $activeRevision) (get $active "workloadName") -}}
  {{- $secretName := required (printf "services.featureRag.revisions.%s.secretName is required" $activeRevision) (get $active "secretName") -}}
  {{- $domains := list -}}
  {{- range $revision, $entry := $revisions -}}
    {{- if (get $entry "deploy" | default false) -}}
      {{- $deployedWorkload := required (printf "services.featureRag.revisions.%s.workloadName is required when deploy=true" $revision) (get $entry "workloadName") -}}
      {{- $domain := printf "%s.kagent.svc.cluster.local" $deployedWorkload -}}
      {{- if not (has $domain $domains) -}}
        {{- $domains = append $domains $domain -}}
      {{- end -}}
    {{- end -}}
  {{- end -}}
  {{- $_ := set $resolved "enabled" true -}}
  {{- $_ := set $resolved "revision" $activeRevision -}}
  {{- $_ := set $resolved "url" (printf "http://%s.kagent.svc.cluster.local:8080/mcp" $workloadName) -}}
  {{- $_ := set $resolved "secretName" $secretName -}}
  {{- $_ := set $resolved "allowedDomains" $domains -}}
{{- else -}}
  {{- $_ := set $resolved "url" (required "mcp.url is required when services.featureRag is absent" .Values.mcp.url) -}}
  {{- $_ := set $resolved "secretName" (required "mcp.authSecret is required when services.featureRag is absent" .Values.mcp.authSecret) -}}
{{- end -}}
{{- $resolved | toJson -}}
{{- end }}
