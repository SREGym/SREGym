{{/*
Common labels applied to every SRE-World-authored object. Pass the root ($).
Peer of slack.labels — namespaced to avoid collision with any upstream Frappe
helpers exported by the vendored subchart.
*/}}
{{- define "frappe-spine.labels" -}}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{/* Route the agent foothold through the task-owned cluster-only resolver. */}}
{{- define "frappe-spine.confinedDns" -}}
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

{{/*
True if a substrate addition is enabled (defaults to false if the key is
absent). Usage: {{ if (include "frappe-spine.enabled" (dict "root" $ "key" "main")) }}
*/}}
{{- define "frappe-spine.enabled" -}}
{{- $c := index .root.Values .key -}}
{{- if and $c $c.enabled -}}true{{- end -}}
{{- end -}}
