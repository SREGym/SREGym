{{/* Common labels — peer of frappe-spine.labels. */}}
{{- define "saleor-spine.labels" -}}
app.kubernetes.io/name: saleor-spine
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/* Route the agent foothold through the task-owned cluster-only resolver. */}}
{{- define "saleor-spine.confinedDns" -}}
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
Shared Saleor runtime env (api, worker, init Job — single source, no drift).
Every value here is an UPSTREAM-DOCUMENTED Saleor setting; scenario overlays
may re-value them (tier 1a) but never invent new ones (DESIGN.md §4.3/§4.5).
*/}}
{{- define "saleor-spine.saleorEnv" -}}
{{/* Call with (dict "root" $) for the app-role DATABASE_URL, or
     (dict "root" $ "dbUrl" "<postgres url>") to override it (worker dedicated
     DB role). Emitting exactly ONE DATABASE_URL avoids duplicate env keys:
     the old "list it twice, last dup wins" trick is honored by kubelet and
     client-side apply but REJECTED by server-side apply (worker deploy fails).
     SUPERUSER SPLIT: the app connects as the NOSUPERUSER role created by the
     subchart's initdb hook; the auth.username superuser backs only
     DB_ADMIN_DSN (foothold) + the grading plane. leak_probe enforces this. */}}
{{- $root := .root -}}
{{- $dbUrl := .dbUrl | default (printf "postgres://%s:%s@postgres:5432/%s" $root.Values.postgres.appUser $root.Values.postgres.appPassword $root.Values.postgres.auth.database) -}}
- name: DATABASE_URL
  value: {{ $dbUrl | quote }}
- name: SECRET_KEY
  value: {{ $root.Values.saleor.secretKey | quote }}
- name: CACHE_URL
  value: "redis://:{{ $root.Values.valkey.auth.password }}@valkey:6379/0"
- name: CELERY_BROKER_URL
  value: "amqp://{{ $root.Values.rabbitmq.auth.username }}:{{ $root.Values.rabbitmq.auth.password }}@rabbitmq:5672//"
# Saleor's SSRF guard (HTTP_IP_FILTER_ENABLED, default true) blocks webhook
# delivery to PRIVATE IP ranges — i.e. every in-cluster consumer. This
# deployment's webhook subscribers live on the cluster network by design
# (async-lane forensics: 95/95 deliveries silently filtered), so the guard is
# off as healthy baseline. NOT a fault surface.
- name: HTTP_IP_FILTER_ENABLED
  value: "False"
- name: ALLOWED_HOSTS
  value: {{ $root.Values.saleor.allowedHosts | quote }}
- name: ALLOWED_CLIENT_HOSTS
  value: {{ $root.Values.saleor.allowedHosts | quote }}
- name: PUBLIC_URL
  value: {{ $root.Values.saleor.publicUrl | quote }}
- name: EMAIL_URL
  value: "console://"
- name: RSA_PRIVATE_KEY
  value: {{ $root.Values.saleor.rsaPrivateKey | quote }}
- name: DEBUG
  value: "False"
{{- end }}
