{{- define "frappe-spine.agentEgressProxyConfig" -}}
{{- $root := .root -}}
{{- $hosts := .hosts | default (list) -}}
{{- $public := .public | default false -}}
admin:
  address:
    socket_address: {address: 127.0.0.1, port_value: 9901}
static_resources:
  listeners:
    - name: agent_http_proxy
      address:
        socket_address: {address: 0.0.0.0, port_value: {{ $root.Values.agentEgressProxy.port }}}
      filter_chains:
        - filters:
            - name: envoy.filters.network.http_connection_manager
              typed_config:
                "@type": type.googleapis.com/envoy.extensions.filters.network.http_connection_manager.v3.HttpConnectionManager
                stat_prefix: agent_http_proxy
                stream_idle_timeout: 3600s
                normalize_path: true
                merge_slashes: true
                internal_address_config:
                  cidr_ranges:
                    - {address_prefix: 127.0.0.0, prefix_len: 8}
                http_protocol_options:
                  allow_absolute_url: true
                upgrade_configs:
                  - upgrade_type: CONNECT
                route_config:
                  name: agent_http_proxy_routes
                  virtual_hosts:
                  {{- if $public }}
                    - name: trusted_setup_public
                      domains: ["*"]
                      routes:
                        - match: {connect_matcher: {}}
                          route:
                            cluster: setup_dynamic_forward_proxy
                            timeout: 0s
                            upgrade_configs:
                              - upgrade_type: CONNECT
                                connect_config: {}
                        - match: {prefix: "/"}
                          route:
                            cluster: setup_dynamic_forward_proxy
                            timeout: 30s
                  {{- else }}
                  {{- range $hosts }}
                    - name: {{ printf "connect-%s" (replace "." "-" .) }}
                      domains: [{{ printf "%s:443" . | quote }}]
                      routes:
                        - match: {connect_matcher: {}}
                          route:
                            cluster: agent_tls_gateway
                            timeout: 0s
                            upgrade_configs:
                              - upgrade_type: CONNECT
                                connect_config: {}
                        - match: {prefix: "/"}
                          direct_response: {status: 405}
                    - name: {{ printf "http-%s" (replace "." "-" .) }}
                      domains: [{{ . | quote }}, {{ printf "%s:80" . | quote }}]
                      routes:
                        - match:
                            prefix: "/"
                            headers:
                              - name: ":method"
                                string_match: {exact: "GET"}
                          route:
                            cluster: {{ printf "http-%s" (replace "." "-" .) }}
                            timeout: 30s
                        - match:
                            prefix: "/"
                            headers:
                              - name: ":method"
                                string_match: {exact: "HEAD"}
                          route:
                            cluster: {{ printf "http-%s" (replace "." "-" .) }}
                            timeout: 30s
                        - match: {prefix: "/"}
                          direct_response: {status: 405}
                  {{- end }}
                    - name: deny_unlisted
                      domains: ["*"]
                      routes:
                        - match: {connect_matcher: {}}
                          direct_response: {status: 403}
                        - match: {prefix: "/"}
                          direct_response: {status: 403}
                  {{- end }}
                http_filters:
                  {{- if $public }}
                  - name: envoy.filters.http.dynamic_forward_proxy
                    typed_config:
                      "@type": type.googleapis.com/envoy.extensions.filters.http.dynamic_forward_proxy.v3.FilterConfig
                      dns_cache_config:
                        name: trusted_setup_dns_cache
                        dns_lookup_family: V4_ONLY
                  {{- end }}
                  - name: envoy.filters.http.router
                    typed_config:
                      "@type": type.googleapis.com/envoy.extensions.filters.http.router.v3.Router
  clusters:
  {{- if $public }}
    - name: setup_dynamic_forward_proxy
      connect_timeout: 10s
      lb_policy: CLUSTER_PROVIDED
      cluster_type:
        name: envoy.clusters.dynamic_forward_proxy
        typed_config:
          "@type": type.googleapis.com/envoy.extensions.clusters.dynamic_forward_proxy.v3.ClusterConfig
          dns_cache_config:
            name: trusted_setup_dns_cache
            dns_lookup_family: V4_ONLY
  {{- else }}
    - name: agent_tls_gateway
      type: STRICT_DNS
      connect_timeout: 5s
      load_assignment:
        cluster_name: agent_tls_gateway
        endpoints:
          - lb_endpoints:
              - endpoint:
                  address:
                    socket_address:
                      address: agent-egress-tls-gateway
                      port_value: {{ $root.Values.agentEgressProxy.tlsGatewayPort }}
  {{- range $hosts }}
    - name: {{ printf "http-%s" (replace "." "-" .) }}
      type: STRICT_DNS
      dns_lookup_family: V4_ONLY
      connect_timeout: 10s
      load_assignment:
        cluster_name: {{ printf "http-%s" (replace "." "-" .) }}
        endpoints:
          - lb_endpoints:
              - endpoint:
                  address:
                    socket_address: {address: {{ . | quote }}, port_value: 80}
  {{- end }}
  {{- end }}
{{- end }}
