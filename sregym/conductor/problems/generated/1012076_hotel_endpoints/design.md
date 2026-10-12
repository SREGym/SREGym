# HTTP transport alias in recurring frontend endpoint export

The frontend Service normally routes HTTP port 5000 through one native
EndpointSlice to its selected frontend Pod. A service-discovery export binds the
same named Service port to the discovery administration transport, port 8500,
and publishes the application discovery peer inventory. Both its service-name
label and its Service owner UID correctly identify frontend. This is a transport
capability and pool scope error, rather than service ownership divergence.

This mirrors a maintenance endpoint export retained after an HTTP transport
naming migration. An operational export process runs inside the existing
recommendation container, launched through the Kubernetes execution API. It
reconciles every 120 seconds using its namespace service-account credentials.
Its registration is ordinary readable JSON beside its executable Python source.
The independent namespace Role limits reads to application Pods and the frontend Service;
updates are limited to its named slice. No Deployment template, application
binary, ConfigMap, database, Kubernetes system component, or monitoring setting
is modified. No extra Pod, Job, sidecar, or host process is left running.

Kubernetes merges this export with the native frontend slice. Most exported
peers refuse connections on port 8500. Consul's existing HTTP API accepts them,
returns 404 for hotel URLs, and keeps connections alive. Connection retries can
therefore settle on an accepting wrong server. All application Pods remain
Running and Ready, and legacy Endpoints remains accurate. Consul legitimately
logs content-type warnings about hotel URLs. The existing blackbox probe detects
frontend connection failures and fires ServiceEndpointDown; no artificial log
lines or alerts are introduced.

Deleting the malformed slice briefly repairs routing. The next periodic export
recreates it, including current Pod addresses, so frontend replacement, rollback,
and ordinary scaling do not eliminate the cause. The namespace export permission persists through frontend Service recreation.
The exporter lives away from the symptomatic frontend and Consul and does not appear in Deployment or
ConfigMap dumps. This makes a quick successful request after deleting a slice an
insufficient recovery check.

An expert compares direct Pod and Service behavior, inventories every slice with
`kubernetes.io/service-name=frontend`, and compares named transport ports and
backend membership against the actual Service selector and Pod ports. The
native slice resolves to port 5000; the independent export resolves to port 8500
over unrelated peers. Its correct ownerReferences do not establish that its
transport mapping is valid. Recreation after deletion indicates another writer.
The slice's manager and namespaced RBAC expose the external export authority.
Inspecting processes in existing application containers finds its executable and
registration. Stop the export or revoke its write authority before removing the
slice. Allow connection pools and the SLO window to turn over, and verify beyond
the 120-second reconciliation interval.

`inject.sh` derives all names, IPs, and UIDs from live cluster state, verifies
the peer inventory is Ready, applies namespace-scoped RBAC and the initial
slice, and starts exactly one export process. Repeated invocation updates its
registration without adding processes or objects. There are no sleep-based
startup races. The background export's timer is part of the incident behavior.
`mitigate.sh` discovers matching exporters from live process arguments and
readable source, stops them, removes the corresponding frontend-specific write
authority, then removes slices violating the live routing contract. It uses no
saved recovery state. `recover.sh` also stops the process, removes its container
files, and restores the original slice/RBAC inventory recorded by injection.
Saved state is tied to the Service UID so copies replay correctly on fresh
deployments. Supporting files contain the exporter and shared process-discovery
logic; they are part of the replay artifact.
