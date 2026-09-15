{{- define "recsys-recommendation-mcp.labels" -}}
app.kubernetes.io/name: {{ .Values.name }}
app.kubernetes.io/component: recommendation-mcp
app.kubernetes.io/part-of: recsys-agentic
{{- end -}}

{{- define "recsys-recommendation-mcp.revisionContexts" -}}
{{- $contexts := list -}}
{{- $services := .Values.services | default dict -}}
{{- $service := get $services "recommendation" | default dict -}}
{{- $revisions := get $service "revisions" | default dict -}}
{{- if gt (len $revisions) 0 -}}
  {{- $seenNames := dict -}}
  {{- range $revisionID, $revision := $revisions -}}
    {{- if eq (toString (get $revision "deploy")) "true" -}}
      {{- $workloadName := required (printf "services.recommendation.revisions.%s.workloadName is required when deploy=true" $revisionID) (get $revision "workloadName") -}}
      {{- $secretName := required (printf "services.recommendation.revisions.%s.secretName is required when deploy=true" $revisionID) (get $revision "secretName") -}}
      {{- if hasKey $seenNames $workloadName -}}
        {{- fail (printf "services.recommendation deploy revisions must use distinct workloadName values; %s is duplicated" $workloadName) -}}
      {{- end -}}
      {{- $_ := set $seenNames $workloadName true -}}
      {{- $contexts = append $contexts (dict "revisionID" $revisionID "name" $workloadName "secretName" $secretName) -}}
    {{- end -}}
  {{- end -}}
  {{- if eq (len $contexts) 0 -}}
    {{- fail "services.recommendation.revisions must contain at least one deploy=true revision" -}}
  {{- end -}}
{{- else -}}
  {{- $contexts = append $contexts (dict "revisionID" "legacy" "name" .Values.name "secretName" .Values.existingSecret) -}}
{{- end -}}
{{- toJson $contexts -}}
{{- end -}}

{{- define "recsys-recommendation-mcp.revisionLabels" -}}
app.kubernetes.io/name: {{ .name }}
app.kubernetes.io/component: recommendation-mcp
app.kubernetes.io/part-of: recsys-agentic
recsys.ai/auth-revision: {{ .revisionID | quote }}
{{- end -}}

{{- define "recsys-recommendation-mcp.allowedHosts" -}}
{{- $root := .root -}}
{{- $workloadName := .name -}}
{{- $legacyHost := printf "%s.%s.svc.cluster.local:%v" $root.Values.name $root.Values.namespace $root.Values.service.port -}}
{{- $revisionHost := printf "%s.%s.svc.cluster.local:%v" $workloadName $root.Values.namespace $root.Values.service.port -}}
{{- $allowedHosts := replace $legacyHost $revisionHost $root.Values.config.allowedHosts -}}
{{- if not (contains $revisionHost $allowedHosts) -}}
  {{- $allowedHosts = printf "%s,%s" $allowedHosts $revisionHost -}}
{{- end -}}
{{- $allowedHosts -}}
{{- end -}}
