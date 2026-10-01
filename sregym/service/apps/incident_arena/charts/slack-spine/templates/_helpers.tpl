{{/*
Common labels applied to every object. Pass the root context ($).
*/}}
{{- define "slack.labels" -}}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{/*
Resource block for a component key, falling back to resources.default.
Usage: {{ include "slack.resources" (dict "root" $ "key" "message") | nindent 12 }}
*/}}
{{- define "slack.resources" -}}
{{- $r := index .root.Values.resources .key -}}
{{- if not $r -}}{{- $r = .root.Values.resources.default -}}{{- end -}}
{{- toYaml $r -}}
{{- end -}}

{{/*
True if a component is enabled (defaults to false if the key is absent).
Usage: {{ if (include "slack.enabled" (dict "root" $ "key" "message")) }}
*/}}
{{- define "slack.enabled" -}}
{{- $c := index .root.Values.components .key -}}
{{- if and $c $c.enabled -}}true{{- end -}}
{{- end -}}

{{/*
Confined execution principals route DNS through the task-owned cluster-only
filter. Include this only in main and db pod specs; the filter and proxy must
retain ClusterFirst so they can resolve their approved upstreams.
*/}}
{{- define "slack.confinedDns" -}}
{{- if include "slack.enabled" (dict "root" . "key" "main") -}}
dnsPolicy: None
dnsConfig:
  nameservers:
    - {{ .Values.agentDnsFilter.clusterIP | quote }}
  searches:
    - {{ printf "%s.svc.cluster.local" .Release.Namespace | quote }}
    - "svc.cluster.local"
    - "cluster.local"
  options:
    - name: ndots
      value: "5"
{{- end -}}
{{- end -}}
