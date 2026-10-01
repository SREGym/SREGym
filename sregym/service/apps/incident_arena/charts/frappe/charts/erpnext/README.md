# Frappe / ERPNext

[Frappe](https://frappe.io)/[ERPNext](https://erpnext.com) Free and Open Source Enterprise Resource Planning (ERP).

## TL;DR;

For evaluation setup simple in-cluster NFS server to make the `nfs` storage class with RWX capabilities available for use. Make sure you have default storage class (`kubectl get sc`) already on your cluster before creating `nfs` storage class.

```shell
kubectl create namespace nfs
helm repo add nfs-ganesha-server-and-external-provisioner https://kubernetes-sigs.github.io/nfs-ganesha-server-and-external-provisioner
helm upgrade --install -n nfs in-cluster nfs-ganesha-server-and-external-provisioner/nfs-server-provisioner --set 'storageClass.mountOptions={vers=4.1}' --set persistence.enabled=true --set persistence.size=8Gi
```

Install ERPNext using the `nfs` storage class.

```shell
kubectl create namespace erpnext
helm repo add frappe https://helm.erpnext.com
helm upgrade --install frappe-bench --namespace erpnext frappe/erpnext --set persistence.worker.storageClass=nfs --set mariadb-sts.enabled=true
```

# Contents

1. [Introduction](#introduction)
2. [Parameters](#parameters)
3. [Requirements](#requirements)
    1. [Storage Class with ReadWriteMany access mode](#storage-class-with-readwritemany-access-mode)
    2. [Database](#database)
    3. [Managed Redis](#managed-redis)
4. [Configuration](#configuration)
    1. [Existing PVC](#existing-pvc)
    2. [Existing Storage Class](#existing-storage-class)
    3. [External Database](#external-database)
    4. [Using Bitnami Subcharts (Legacy)](#using-bitnami-subcharts-legacy)
5. [Installation](#installation)
6. [Generate Additional Resources](#generate-additional-resources)
    1. [Create new site](#create-new-site)
    2. [Create multiple new site](#create-multiple-sites)
    3. [Create Ingress](#create-ingress)
    4. [Create HTTPRoute](#create-httproute)
    5. [Backup site](#backup-site)
    6. [Backup multiple sites](#backup-multiple-sites)
    7. [Migrate site](#migrate-site)
    8. [Migrate multiple sites](#migrate-multiple-sites)
    9. [Drop Site](#drop-site)
    10.[Drop Multiple sites](#drop-multiple-sites)
    11. [Configure service hosts](#configure-service-hosts)
    12. [Fix volume permission](#fix-volume-permission)
    13. [Clear cache](#clear-cache)
7. [Uninstall the Chart](#uninstall-the-chart)
8. [Migrating from Bitnami Subcharts](#migrating-from-bitnami-subcharts)

## Introduction

This chart bootstraps a Frappe/ERPNext deployment on a Kubernetes cluster using the Helm package manager.

## Parameters

The following table lists the configurable parameters of the ERPNext chart and their default values.

### erpnext

![Version: 8.0.55](https://img.shields.io/badge/Version-8.0.55-informational?style=flat-square) ![Type: application](https://img.shields.io/badge/Type-application-informational?style=flat-square) ![AppVersion: v16.20.1](https://img.shields.io/badge/AppVersion-v16.20.1-informational?style=flat-square)

Kubernetes Helm Chart for ERPNext and Frappe Framework Apps.

## Requirements

| Repository | Name | Version |
|------------|------|---------|
| https://charts.bitnami.com/bitnami | mariadb-subchart(mariadb) | 11.5.7 |
| https://charts.bitnami.com/bitnami | postgresql-subchart(postgresql) | 12.1.6 |
| https://charts.bitnami.com/bitnami | redis-cache(redis) | 17.15.2 |
| https://charts.bitnami.com/bitnami | redis-queue(redis) | 17.15.2 |
| https://valkey-io.github.io/valkey-helm | valkey-cache(valkey) | 0.9.3 |
| https://valkey-io.github.io/valkey-helm | valkey-queue(valkey) | 0.9.3 |
| oci://ghcr.io/dragonflydb/dragonfly/helm | dragonfly-cache(dragonfly) | v1.34.2 |
| oci://ghcr.io/dragonflydb/dragonfly/helm | dragonfly-queue(dragonfly) | v1.34.2 |

## Values

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| dragonfly-cache.enabled | bool | `false` |  |
| dragonfly-queue.enabled | bool | `false` |  |
| dragonfly-queue.storage.enabled | bool | `false` |  |
| dragonfly-queue.storage.size | string | `"8Gi"` |  |
| externalRedis.cache | string | `""` |  |
| externalRedis.queue | string | `""` |  |
| fullnameOverride | string | `""` |  |
| httproute.annotations | object | `{}` |  |
| httproute.enabled | bool | `false` |  |
| httproute.hostnames[0] | string | `"erp.example.com"` |  |
| httproute.name | string | `""` |  |
| httproute.parentRefs[0].gatewayName | string | `"public-gateway"` |  |
| httproute.parentRefs[0].gatewayNamespace | string | `"networking"` |  |
| httproute.parentRefs[0].gatewaySectionName | string | `"http"` |  |
| httproute.rules[0].matches[0].path | string | `"/"` |  |
| httproute.rules[0].matches[0].pathType | string | `"PathPrefix"` |  |
| image.pullPolicy | string | `"IfNotPresent"` |  |
| image.repository | string | `"frappe/erpnext"` |  |
| image.tag | string | `"v16.20.1"` |  |
| imagePullSecrets | list | `[]` |  |
| ingress.annotations | object | `{}` |  |
| ingress.enabled | bool | `false` |  |
| ingress.hosts[0].host | string | `"erp.cluster.local"` |  |
| ingress.hosts[0].paths[0].path | string | `"/"` |  |
| ingress.hosts[0].paths[0].pathType | string | `"ImplementationSpecific"` |  |
| ingress.tls | list | `[]` |  |
| jobs.backup.affinity | object | `{}` |  |
| jobs.backup.backoffLimit | int | `0` |  |
| jobs.backup.enabled | bool | `false` |  |
| jobs.backup.nodeSelector | object | `{}` |  |
| jobs.backup.resources | object | `{}` |  |
| jobs.backup.siteName | string | `"erp.cluster.local"` |  |
| jobs.backup.tolerations | list | `[]` |  |
| jobs.backup.withFiles | bool | `true` |  |
| jobs.backupMultipleSites.affinity | object | `{}` |  |
| jobs.backupMultipleSites.backoffLimit | int | `0` |  |
| jobs.backupMultipleSites.enabled | bool | `false` |  |
| jobs.backupMultipleSites.nodeSelector | object | `{}` |  |
| jobs.backupMultipleSites.resources | object | `{}` |  |
| jobs.backupMultipleSites.sites[0].name | string | `"erp.cluster.local"` |  |
| jobs.backupMultipleSites.tolerations | list | `[]` |  |
| jobs.backupMultipleSites.withFiles | bool | `true` |  |
| jobs.clearCache.affinity | object | `{}` |  |
| jobs.clearCache.backoffLimit | int | `0` |  |
| jobs.clearCache.enabled | bool | `false` |  |
| jobs.clearCache.initContainer.deployment.name | string | `""` |  |
| jobs.clearCache.initContainer.enabled | bool | `false` |  |
| jobs.clearCache.initContainer.image | string | `"alpine/kubectl:1.35.0"` |  |
| jobs.clearCache.initContainer.timeout | string | `"600s"` |  |
| jobs.clearCache.nodeSelector | object | `{}` |  |
| jobs.clearCache.redisFlush.enabled | bool | `false` |  |
| jobs.clearCache.resources | object | `{}` |  |
| jobs.clearCache.serviceAccountName | string | `""` |  |
| jobs.clearCache.tolerations | list | `[]` |  |
| jobs.configure.affinity | object | `{}` |  |
| jobs.configure.args | list | `[]` |  |
| jobs.configure.backoffLimit | int | `0` |  |
| jobs.configure.command | list | `[]` |  |
| jobs.configure.enabled | bool | `true` |  |
| jobs.configure.envVars | list | `[]` |  |
| jobs.configure.fixVolume | bool | `true` |  |
| jobs.configure.nodeSelector | object | `{}` |  |
| jobs.configure.resources | object | `{}` |  |
| jobs.configure.tolerations | list | `[]` |  |
| jobs.createMultipleSites.adminExistingSecret | string | `""` |  |
| jobs.createMultipleSites.adminExistingSecretKey | string | `"password"` |  |
| jobs.createMultipleSites.adminPassword | string | `"changeit"` |  |
| jobs.createMultipleSites.affinity | object | `{}` |  |
| jobs.createMultipleSites.backoffLimit | int | `0` |  |
| jobs.createMultipleSites.dbType | string | `"mariadb"` |  |
| jobs.createMultipleSites.enabled | bool | `false` |  |
| jobs.createMultipleSites.forceCreate | bool | `false` |  |
| jobs.createMultipleSites.nodeSelector | object | `{}` |  |
| jobs.createMultipleSites.resources | object | `{}` |  |
| jobs.createMultipleSites.sites[0].installApps[0] | string | `"erpnext"` |  |
| jobs.createMultipleSites.sites[0].name | string | `"erp.cluster.local"` |  |
| jobs.createMultipleSites.tolerations | list | `[]` |  |
| jobs.createSite.adminExistingSecret | string | `""` |  |
| jobs.createSite.adminExistingSecretKey | string | `"password"` |  |
| jobs.createSite.adminPassword | string | `"changeit"` |  |
| jobs.createSite.affinity | object | `{}` |  |
| jobs.createSite.backoffLimit | int | `0` |  |
| jobs.createSite.dbType | string | `"mariadb"` |  |
| jobs.createSite.enabled | bool | `false` |  |
| jobs.createSite.forceCreate | bool | `false` |  |
| jobs.createSite.installApps[0] | string | `"erpnext"` |  |
| jobs.createSite.nodeSelector | object | `{}` |  |
| jobs.createSite.resources | object | `{}` |  |
| jobs.createSite.siteName | string | `"erp.cluster.local"` |  |
| jobs.createSite.tolerations | list | `[]` |  |
| jobs.custom.affinity | object | `{}` |  |
| jobs.custom.backoffLimit | int | `0` |  |
| jobs.custom.containers | list | `[]` |  |
| jobs.custom.enabled | bool | `false` |  |
| jobs.custom.initContainers | list | `[]` |  |
| jobs.custom.jobName | string | `""` |  |
| jobs.custom.labels | object | `{}` |  |
| jobs.custom.nodeSelector | object | `{}` |  |
| jobs.custom.restartPolicy | string | `"Never"` |  |
| jobs.custom.tolerations | list | `[]` |  |
| jobs.custom.volumes | list | `[]` |  |
| jobs.dropMultipleSites.affinity | object | `{}` |  |
| jobs.dropMultipleSites.backoffLimit | int | `0` |  |
| jobs.dropMultipleSites.enabled | bool | `false` |  |
| jobs.dropMultipleSites.forced | bool | `false` |  |
| jobs.dropMultipleSites.nodeSelector | object | `{}` |  |
| jobs.dropMultipleSites.resources | object | `{}` |  |
| jobs.dropMultipleSites.sites[0].name | string | `"site1.example.com"` |  |
| jobs.dropMultipleSites.sites[1].name | string | `"site2.example.com"` |  |
| jobs.dropMultipleSites.tolerations | list | `[]` |  |
| jobs.dropSite.affinity | object | `{}` |  |
| jobs.dropSite.backoffLimit | int | `0` |  |
| jobs.dropSite.enabled | bool | `false` |  |
| jobs.dropSite.forced | bool | `false` |  |
| jobs.dropSite.nodeSelector | object | `{}` |  |
| jobs.dropSite.resources | object | `{}` |  |
| jobs.dropSite.siteName | string | `"erp.cluster.local"` |  |
| jobs.dropSite.tolerations | list | `[]` |  |
| jobs.migrate.affinity | object | `{}` |  |
| jobs.migrate.backoffLimit | int | `0` |  |
| jobs.migrate.enabled | bool | `false` |  |
| jobs.migrate.nodeSelector | object | `{}` |  |
| jobs.migrate.resources | object | `{}` |  |
| jobs.migrate.siteName | string | `"erp.cluster.local"` |  |
| jobs.migrate.skipFailing | bool | `false` |  |
| jobs.migrate.tolerations | list | `[]` |  |
| jobs.migrateMultipleSites.affinity | object | `{}` |  |
| jobs.migrateMultipleSites.backoffLimit | int | `0` |  |
| jobs.migrateMultipleSites.enabled | bool | `false` |  |
| jobs.migrateMultipleSites.nodeSelector | object | `{}` |  |
| jobs.migrateMultipleSites.resources | object | `{}` |  |
| jobs.migrateMultipleSites.sites[0].name | string | `"erp.cluster.local"` |  |
| jobs.migrateMultipleSites.skipFailing | bool | `false` |  |
| jobs.migrateMultipleSites.tolerations | list | `[]` |  |
| jobs.volumePermissions.affinity | object | `{}` |  |
| jobs.volumePermissions.backoffLimit | int | `0` |  |
| jobs.volumePermissions.enabled | bool | `false` |  |
| jobs.volumePermissions.nodeSelector | object | `{}` |  |
| jobs.volumePermissions.resources | object | `{}` |  |
| jobs.volumePermissions.tolerations | list | `[]` |  |
| mariadb-sts.enabled | bool | `false` |  |
| mariadb-sts.image.pullPolicy | string | `"IfNotPresent"` |  |
| mariadb-sts.image.repository | string | `"mariadb"` |  |
| mariadb-sts.image.tag | string | `"10.6"` |  |
| mariadb-sts.myCnf | string | `"[mysqld]\nskip-character-set-client-handshake\nskip-innodb-read-only-compressed\ncharacter-set-server=utf8mb4\ncollation-server=utf8mb4_unicode_ci\n"` |  |
| mariadb-sts.persistence.size | string | `"8Gi"` |  |
| mariadb-sts.resources | object | `{}` |  |
| mariadb-sts.rootPassword | string | `"changeit"` |  |
| mariadb-subchart.enabled | bool | `false` |  |
| mariadb-subchart.image.repository | string | `"bitnamilegacy/mariadb"` |  |
| mariadb-subchart.image.tag | string | `"10.6.17-debian-11-r10"` |  |
| mariadb.enabled | bool | `false` |  |
| nameOverride | string | `""` |  |
| nginx.affinity | object | `{}` |  |
| nginx.autoscaling.enabled | bool | `false` |  |
| nginx.autoscaling.maxReplicas | int | `3` |  |
| nginx.autoscaling.minReplicas | int | `1` |  |
| nginx.autoscaling.targetCPU | int | `75` |  |
| nginx.autoscaling.targetMemory | int | `75` |  |
| nginx.defaultTopologySpread.maxSkew | int | `1` |  |
| nginx.defaultTopologySpread.topologyKey | string | `"kubernetes.io/hostname"` |  |
| nginx.defaultTopologySpread.whenUnsatisfiable | string | `"DoNotSchedule"` |  |
| nginx.envVars | list | `[]` |  |
| nginx.environment.clientMaxBodySize | string | `"50m"` |  |
| nginx.environment.frappeSiteNameHeader | string | `"$host"` |  |
| nginx.environment.proxyReadTimeout | string | `"120"` |  |
| nginx.environment.upstreamRealIPAddress | string | `"127.0.0.1"` |  |
| nginx.environment.upstreamRealIPHeader | string | `"X-Forwarded-For"` |  |
| nginx.environment.upstreamRealIPRecursive | string | `"off"` |  |
| nginx.initContainers | list | `[]` |  |
| nginx.livenessProbe.initialDelaySeconds | int | `5` |  |
| nginx.livenessProbe.periodSeconds | int | `10` |  |
| nginx.livenessProbe.tcpSocket.port | int | `8080` |  |
| nginx.nodeSelector | object | `{}` |  |
| nginx.podAnnotations | object | `{}` |  |
| nginx.readinessProbe.initialDelaySeconds | int | `5` |  |
| nginx.readinessProbe.periodSeconds | int | `10` |  |
| nginx.readinessProbe.tcpSocket.port | int | `8080` |  |
| nginx.replicaCount | int | `1` |  |
| nginx.resources | object | `{}` |  |
| nginx.service.port | int | `8080` |  |
| nginx.service.type | string | `"ClusterIP"` |  |
| nginx.sidecars | list | `[]` |  |
| nginx.tolerations | list | `[]` |  |
| persistence.logs.accessModes[0] | string | `"ReadWriteMany"` |  |
| persistence.logs.enabled | bool | `false` |  |
| persistence.logs.size | string | `"8Gi"` |  |
| persistence.worker.accessModes[0] | string | `"ReadWriteMany"` |  |
| persistence.worker.enabled | bool | `true` |  |
| persistence.worker.size | string | `"8Gi"` |  |
| podSecurityContext.supplementalGroups[0] | int | `1000` |  |
| postgresql-sts.enabled | bool | `false` |  |
| postgresql-sts.image.pullPolicy | string | `"IfNotPresent"` |  |
| postgresql-sts.image.repository | string | `"postgres"` |  |
| postgresql-sts.image.tag | string | `"15"` |  |
| postgresql-sts.persistence.size | string | `"8Gi"` |  |
| postgresql-sts.postgresPassword | string | `"changeit"` |  |
| postgresql-sts.postgresUser | string | `"postgres"` |  |
| postgresql-sts.resources | object | `{}` |  |
| postgresql-subchart.enabled | bool | `false` |  |
| postgresql-subchart.image.repository | string | `"bitnamilegacy/postgresql"` |  |
| postgresql-subchart.image.tag | string | `"14"` |  |
| postgresql.enabled | bool | `false` |  |
| redis-cache.enabled | bool | `false` |  |
| redis-cache.image.repository | string | `"bitnamilegacy/redis"` |  |
| redis-cache.image.tag | string | `"7.0"` |  |
| redis-queue.enabled | bool | `false` |  |
| redis-queue.image.repository | string | `"bitnamilegacy/redis"` |  |
| redis-queue.image.tag | string | `"7.0"` |  |
| securityContext.capabilities.add[0] | string | `"CAP_CHOWN"` |  |
| serviceAccount.create | bool | `true` |  |
| socketio.affinity | object | `{}` |  |
| socketio.autoscaling.enabled | bool | `false` |  |
| socketio.autoscaling.maxReplicas | int | `3` |  |
| socketio.autoscaling.minReplicas | int | `1` |  |
| socketio.autoscaling.targetCPU | int | `75` |  |
| socketio.autoscaling.targetMemory | int | `75` |  |
| socketio.envVars | list | `[]` |  |
| socketio.initContainers | list | `[]` |  |
| socketio.livenessProbe.initialDelaySeconds | int | `5` |  |
| socketio.livenessProbe.periodSeconds | int | `10` |  |
| socketio.livenessProbe.tcpSocket.port | int | `9000` |  |
| socketio.nodeSelector | object | `{}` |  |
| socketio.podAnnotations | object | `{}` |  |
| socketio.readinessProbe.initialDelaySeconds | int | `5` |  |
| socketio.readinessProbe.periodSeconds | int | `10` |  |
| socketio.readinessProbe.tcpSocket.port | int | `9000` |  |
| socketio.replicaCount | int | `1` |  |
| socketio.resources | object | `{}` |  |
| socketio.service.port | int | `9000` |  |
| socketio.service.type | string | `"ClusterIP"` |  |
| socketio.sidecars | list | `[]` |  |
| socketio.tolerations | list | `[]` |  |
| valkey-cache.enabled | bool | `true` |  |
| valkey-cache.image.repository | string | `"valkey/valkey"` |  |
| valkey-cache.image.tag | string | `"7.2"` |  |
| valkey-queue.enabled | bool | `true` |  |
| valkey-queue.image.repository | string | `"valkey/valkey"` |  |
| valkey-queue.image.tag | string | `"7.2"` |  |
| worker.default.affinity | object | `{}` |  |
| worker.default.autoscaling.enabled | bool | `false` |  |
| worker.default.autoscaling.maxReplicas | int | `3` |  |
| worker.default.autoscaling.minReplicas | int | `1` |  |
| worker.default.autoscaling.targetCPU | int | `75` |  |
| worker.default.autoscaling.targetMemory | int | `75` |  |
| worker.default.envVars | list | `[]` |  |
| worker.default.initContainers | list | `[]` |  |
| worker.default.livenessProbe.override | bool | `false` |  |
| worker.default.livenessProbe.probe | object | `{}` |  |
| worker.default.nodeSelector | object | `{}` |  |
| worker.default.podAnnotations | object | `{}` |  |
| worker.default.readinessProbe.override | bool | `false` |  |
| worker.default.readinessProbe.probe | object | `{}` |  |
| worker.default.replicaCount | int | `1` |  |
| worker.default.resources | object | `{}` |  |
| worker.default.sidecars | list | `[]` |  |
| worker.default.tolerations | list | `[]` |  |
| worker.defaultTopologySpread.maxSkew | int | `1` |  |
| worker.defaultTopologySpread.topologyKey | string | `"kubernetes.io/hostname"` |  |
| worker.defaultTopologySpread.whenUnsatisfiable | string | `"DoNotSchedule"` |  |
| worker.gunicorn.affinity | object | `{}` |  |
| worker.gunicorn.args | list | `[]` |  |
| worker.gunicorn.autoscaling.enabled | bool | `false` |  |
| worker.gunicorn.autoscaling.maxReplicas | int | `3` |  |
| worker.gunicorn.autoscaling.minReplicas | int | `1` |  |
| worker.gunicorn.autoscaling.targetCPU | int | `75` |  |
| worker.gunicorn.autoscaling.targetMemory | int | `75` |  |
| worker.gunicorn.envVars | list | `[]` |  |
| worker.gunicorn.initContainers | list | `[]` |  |
| worker.gunicorn.livenessProbe.initialDelaySeconds | int | `5` |  |
| worker.gunicorn.livenessProbe.periodSeconds | int | `10` |  |
| worker.gunicorn.livenessProbe.tcpSocket.port | int | `8000` |  |
| worker.gunicorn.nodeSelector | object | `{}` |  |
| worker.gunicorn.podAnnotations | object | `{}` |  |
| worker.gunicorn.readinessProbe.initialDelaySeconds | int | `5` |  |
| worker.gunicorn.readinessProbe.periodSeconds | int | `10` |  |
| worker.gunicorn.readinessProbe.tcpSocket.port | int | `8000` |  |
| worker.gunicorn.replicaCount | int | `1` |  |
| worker.gunicorn.resources | object | `{}` |  |
| worker.gunicorn.service.port | int | `8000` |  |
| worker.gunicorn.service.type | string | `"ClusterIP"` |  |
| worker.gunicorn.sidecars | list | `[]` |  |
| worker.gunicorn.tolerations | list | `[]` |  |
| worker.healthProbe | string | `"exec:\n  command:\n    - bash\n    - -c\n    - |-\n      echo \"Pinging backing services\";\n      {{- if (index .Values \"mariadb-sts\").enabled }}\n      wait-for-it {{ include \"erpnext.fullname\" . }}-mariadb-sts:3306 -t 1;\n      {{- else if (index .Values \"postgresql-sts\").enabled }}\n      wait-for-it {{ include \"erpnext.fullname\" . }}-postgresql-sts:5432 -t 1;\n      {{- else if or .Values.mariadb.enabled (get .Values \"mariadb-subchart\").enabled }}\n      (\n        wait-for-it {{ .Release.Name }}-mariadb-subchart:3306 -t 0 || \\\n        wait-for-it {{ .Release.Name }}-mariadb:3306 -t 0 || \\\n        wait-for-it {{ .Release.Name }}-mariadb-subchart-primary:3306 -t 0 || \\\n        wait-for-it {{ .Release.Name }}-mariadb-primary:3306 -t 1\n      )\n      {{- else if or .Values.postgresql.enabled (get .Values \"postgresql-subchart\").enabled }}\n      (\n        wait-for-it {{ .Release.Name }}-postgresql-subchart:5432 -t 0 || \\\n        wait-for-it {{ .Release.Name }}-postgresql:5432 -t 1\n      )\n      {{- else if or .Values.postgresql.enabled (index .Values \"postgresql-subchart\").enabled }}\n      wait-for-it {{ .Release.Name }}-postgresql-subchart:5432 -t 1;\n      {{- else if .Values.dbHost }}\n      wait-for-it {{ .Values.dbHost }}:{{ .Values.dbPort }} -t 1;\n      {{- end }}\n      {{- if .Values.externalRedis.cache }}\n      wait-for-it $(echo {{ .Values.externalRedis.cache }} | sed 's,redis://,,') -t 1;\n      {{- else if (index .Values \"valkey-cache\").enabled }}\n      wait-for-it {{ .Release.Name }}-valkey-cache:6379 -t 1;\n      {{- else if (index .Values \"dragonfly-cache\").enabled }}\n      wait-for-it {{ .Release.Name }}-dragonfly-cache:6379 -t 1;\n      {{- else if (index .Values \"redis-cache\").enabled }}\n      wait-for-it {{ .Release.Name }}-redis-cache-master:6379 -t 1;\n      {{- end }}\n      {{- if .Values.externalRedis.queue }}\n      wait-for-it $(echo {{ .Values.externalRedis.queue }} | sed 's,redis://,,') -t 1;\n      {{- else if (index .Values \"valkey-queue\").enabled }}\n      wait-for-it {{ .Release.Name }}-valkey-queue:6379 -t 1;\n      {{- else if (index .Values \"dragonfly-queue\").enabled }}\n      wait-for-it {{ .Release.Name }}-dragonfly-queue:6379 -t 1;\n      {{- else if (index .Values \"redis-queue\").enabled }}\n      wait-for-it {{ .Release.Name }}-redis-queue-master:6379 -t 1;\n      {{- end }}\ninitialDelaySeconds: 15\nperiodSeconds: 5\ntimeoutSeconds: 5\n"` |  |
| worker.long.affinity | object | `{}` |  |
| worker.long.autoscaling.enabled | bool | `false` |  |
| worker.long.autoscaling.maxReplicas | int | `3` |  |
| worker.long.autoscaling.minReplicas | int | `1` |  |
| worker.long.autoscaling.targetCPU | int | `75` |  |
| worker.long.autoscaling.targetMemory | int | `75` |  |
| worker.long.envVars | list | `[]` |  |
| worker.long.initContainers | list | `[]` |  |
| worker.long.livenessProbe.override | bool | `false` |  |
| worker.long.livenessProbe.probe | object | `{}` |  |
| worker.long.nodeSelector | object | `{}` |  |
| worker.long.podAnnotations | object | `{}` |  |
| worker.long.readinessProbe.override | bool | `false` |  |
| worker.long.readinessProbe.probe | object | `{}` |  |
| worker.long.replicaCount | int | `1` |  |
| worker.long.resources | object | `{}` |  |
| worker.long.sidecars | list | `[]` |  |
| worker.long.tolerations | list | `[]` |  |
| worker.scheduler.affinity | object | `{}` |  |
| worker.scheduler.envVars | list | `[]` |  |
| worker.scheduler.initContainers | list | `[]` |  |
| worker.scheduler.livenessProbe.override | bool | `false` |  |
| worker.scheduler.livenessProbe.probe | object | `{}` |  |
| worker.scheduler.nodeSelector | object | `{}` |  |
| worker.scheduler.podAnnotations | object | `{}` |  |
| worker.scheduler.readinessProbe.override | bool | `false` |  |
| worker.scheduler.readinessProbe.probe | object | `{}` |  |
| worker.scheduler.replicaCount | int | `1` |  |
| worker.scheduler.resources | object | `{}` |  |
| worker.scheduler.sidecars | list | `[]` |  |
| worker.scheduler.tolerations | list | `[]` |  |
| worker.short.affinity | object | `{}` |  |
| worker.short.autoscaling.enabled | bool | `false` |  |
| worker.short.autoscaling.maxReplicas | int | `3` |  |
| worker.short.autoscaling.minReplicas | int | `1` |  |
| worker.short.autoscaling.targetCPU | int | `75` |  |
| worker.short.autoscaling.targetMemory | int | `75` |  |
| worker.short.envVars | list | `[]` |  |
| worker.short.initContainers | list | `[]` |  |
| worker.short.livenessProbe.override | bool | `false` |  |
| worker.short.livenessProbe.probe | object | `{}` |  |
| worker.short.nodeSelector | object | `{}` |  |
| worker.short.podAnnotations | object | `{}` |  |
| worker.short.readinessProbe.override | bool | `false` |  |
| worker.short.readinessProbe.probe | object | `{}` |  |
| worker.short.replicaCount | int | `1` |  |
| worker.short.resources | object | `{}` |  |
| worker.short.sidecars | list | `[]` |  |
| worker.short.tolerations | list | `[]` |  |

----------------------------------------------

The above parameters map to the env variables defined in frappe_docker. For more information please refer to the frappe_docker images documentation.

## Requirements

### Storage Class with ReadWriteMany access mode

Frappe framework sites are stored in shared volume that needs to be accessed by multiple pods. Read more about Storage Classes. Here are some alternatives available for RWX volumes.

- [AWS EFS](https://docs.aws.amazon.com/eks/latest/userguide/efs-csi.html): Managed shared filesystem by Amazon.
- [Google Filestore](https://cloud.google.com/filestore): Managed shared filesystem by Google.
- [AzureFile](https://docs.microsoft.com/en-us/azure/aks/azure-files-dynamic-pv): Managed shared filesystem by Microsoft.
- [External NFS Server](https://kubernetes-sigs.github.io/nfs-subdir-external-provisioner): Provisioner based on NFS server setup outside cluster. Separately hosted NFS server is needed in this case.
- [In-cluster NFS Server](https://kubernetes-sigs.github.io/nfs-ganesha-server-and-external-provisioner): Provisioner based on in-cluster NFS server.
- [More Cloud Native Storage alternatives](https://landscape.cncf.io/card-mode?category=cloud-native-storage&grouping=category): Make sure the `storageclass` has `ReadWriteMany` access mode to use it as storage for sites.

> **Note**: If you are running a single-node cluster or deploying to a specific node using node affinity, you can use ReadWriteOnce (RWO) access mode if your storage class supports it. This is because all pods will be scheduled on the same node, eliminating the need for multi-node access.

### Database

Database is not deployed by default and must be explicitly enabled.

**Note:** ERPNext requires MariaDB. PostgreSQL can be used for custom Frappe apps that support it.

Recommended alternatives as per priority:

- [Managed DB](https://github.com/frappe/frappe/wiki/Using-Frappe-with-Amazon-RDS-(or-any-other-DBaaS)): Recommended AWS MariaDB RDS.
- [Self hosted MariaDB](https://github.com/frappe/frappe/wiki/Setup-MariaDB-Server): Self hosted mariadb server setup for Debian or Ubuntu.
- [Bitnami Subcharts](https://github.com/bitnami/charts/tree/main/bitnami/mariadb): For backward compatibility, the Bitnami MariaDB and PostgreSQL subcharts can be enabled. See the Using Bitnami Subcharts (Legacy) section.

### Managed Redis

By default, this chart deploys two Valkey instances, one for caching and one for the queue. Valkey is used as an in-memory database, and having it in the cluster provides the lowest latency.

For backward compatibility or other requirements, DragonflyDB or the Bitnami Redis subchart can be enabled instead. See the Using Bitnami Subcharts (Legacy) section.

Alternatively, any managed Redis-compatible service can be used by providing the connection details in your `values.yaml`.

## Configuration

Create a `custom-values.yaml` file to override the default settings. Below are examples for common configurations.

### Existing Storage Class

Make following changes to `custom-values.yaml`:

```yaml
persistence:
  worker:
    storageClass: "rook-cephfs"
```

Make sure the storage class called `rook-cephfs` is available on the cluster.

### Existing PVC

Make following changes to `custom-values.yaml`:

```yaml
persistence:
  worker:
    existingClaim: existing-sites
```

Make sure the PVC called `existing-sites` exists in the namespace.

### Access Modes

Make following changes to custom-values.yaml:

```yaml
persistence:
  worker:
    accessModes:
      - ReadWriteMany  # default mode
```

You can configure this to use ReadWriteOnce (RWO) if you are running a single-node cluster or deploying to a specific node using node affinity.

### External Database

To use an external database (like Amazon RDS), disable the built-in and subchart databases and provide the connection details.

```yaml
# Disable all in-cluster databases
mariadb-sts:
  enabled: false
postgresql-sts:
  enabled: false
mariadb-subchart:
  enabled: false
postgresql-subchart:
  enabled: false

dbHost: "1.2.3.4"
dbPort: "3306"
dbRootUser: "admin"
# Use dbExistingSecret for production environments
dbExistingSecret: "my-external-db-secret"
dbExistingSecretPasswordKey: "password"
```

### Using Bitnami Subcharts (Legacy)

> **Note for Upgrading Users**: If you are upgrading from a previous version where you used the default Bitnami subchart, you must now explicitly set `mariadb-subchart: true` (or `mariadb.enabled: true` for backward compatibility) in your `custom-values.yaml` to prevent Helm from disabling it during the upgrade.

To use the classic Bitnami subcharts for the database or cache/queue, disable the new built-in components and enable the corresponding subcharts.

```yaml
# Disable new built-in MariaDB and Valkey
mariadb-sts:
  enabled: false
valkey-cache:
  enabled: false
valkey-queue:
  enabled: false
# Enable and configure classic Bitnami subcharts
mariadb-subchart:
  enabled: true
  # Pass values to the bitnami/mariadb subchart here
  # primary:
  #   persistence:
  #     size: 10Gi

redis-cache:
  enabled: true
  # Pass values to the bitnami/redis subchart here

redis-queue:
  enabled: true
  # Pass values to the bitnami/redis subchart here
```

## Installation

Create namespace for erpnext

```shell
kubectl create namespace erpnext
```

Add helm frappe helm repo

```shell
helm repo add frappe https://helm.erpnext.com
```

Install helm chart and create release

```shell
helm install frappe-bench -n erpnext -f custom-values.yaml frappe/erpnext
```

For rest of the document `frappe-bench` is treated as the helm release name and `custom-values.yaml` file will contain required values to override.

## Generate Additional Resources

These resources can be generated and created along with installation and upgrade of the helm release.
Instead of doing that we can have better control if we use `helm template` command and generate the required resources. That way we get yaml files that can also be committed to gitops repo. Generating same job multiple times will not cause any problem as the job names will have Unix timestamp.

Following sections discuss different resources that can be created using `helm template` command.

### Create new site

Make following changes to `custom-values.yaml`:

```yaml
jobs:
  createSite:
    enabled: true
    siteName: "erp.example.com"
    adminPassword: "secret"
```

Note: `erp.example.com` must be configured DNS entry and change the `adminPassword` to something more secure.

Generate Job YAML

```shell
helm template frappe-bench -n erpnext frappe/erpnext -f custom-values.yaml -s templates/job-create-site.yaml > create-new-site-job.yaml
```

Create Job resource

```shell
kubectl apply -f create-new-site-job.yaml
```

### Create Ingress

Make following changes to `custom-values.yaml`:

```yaml
ingress:
  enabled: true
  ingressName: "erp-example-com"
  annotations:
    cert-manager.io/cluster-issuer: letsencrypt-prod
    kubernetes.io/ingress.class: nginx
    kubernetes.io/tls-acme: "true"
  hosts:
  - host: erp.example.com
    paths:
    - path: /
      pathType: ImplementationSpecific
  tls:
  - secretName: erp-example-com-tls
    hosts:
    - erp.example.com
```

Note:

- `erp.example.com` must be configured DNS entry.
- Change `annotations` as per requirement.
- Change `secretName` as per convenience.

Generate Ingress YAML

```shell
helm template frappe-bench -n erpnext frappe/erpnext -f custom-values.yaml -s templates/ingress.yaml > ingress.yaml
```

Create Ingress resource

```shell
kubectl apply -f ingress.yaml
```

### Create HTTPRoute

If your cluster is using GatewayAPI, make the following changes to `custom-values.yaml`:

```yaml
httproute:
  enabled: true
  name: "erp-example-com-httproute"
  annotations: {}
  parentRefs:
    - gatewayName: "public-gateway"              # name of your Gateway resource
      gatewayNamespace: "networking"            # namespace where the Gateway is defined
      gatewaySectionName: "http"                # listener name (optional), check your gateway requirements
  hostnames:
    - "erp.example.com"
  rules:
    - matches:
        - pathType: "PathPrefix"
          path: "/"
```

Note:

- A Gateway resource (e.g., `public-gateway`) must already exist in the cluster.
- `erp.example.com` must be an existing and configured DNS entry.
- `gatewayNamespace`, `gatewaySectionName`, and other fields can be customized as needed.
- Change `annotations` as per requirement, if any.

Generate the HTTPRoute YAML

```shell
helm template frappe-bench -n erpnext frappe/erpnext -f custom-values.yaml -s templates/httproute.yaml > httproute.yaml
```

Create HTTPRoute resource

```shell
kubectl apply -f httproute.yaml
```

### Backup site

Make following changes to `custom-values.yaml`:

```yaml
jobs:
  backup:
    enabled: true
    siteName: "erp.example.com"
    withFiles: true
    push:
      enabled: false
      bucket: "erpnext"
      region: "us-east-1"
      accessKey: "ACCESSKEY"
      secretKey: "SECRETKEY"
      endpoint: http://store.storage.svc.cluster.local
```

Note:

- Site `erp.example.com` must exist.
- To push backup to S3, enter S3 credentials and set `jobs.backup.push.enabled` to `true`.

Generate Job YAML

```shell
helm template frappe-bench -n erpnext frappe/erpnext -f custom-values.yaml -s templates/job-backup.yaml > job-backup.yaml
```

Create Job resource

```shell
kubectl apply -f job-backup.yaml
```

### Migrate site

Make following changes to `custom-values.yaml`:

```yaml
jobs:
  migrate:
    enabled: true
    siteName: "erp.example.com"
```

Note: Site `erp.example.com` must exist.

Generate Job YAML

```shell
helm template frappe-bench -n erpnext frappe/erpnext -f custom-values.yaml -s templates/job-migrate-site.yaml > job-migrate-site.yaml
```

Create Job resource

```shell
kubectl apply -f job-migrate-site.yaml
```

### Drop site

Make following changes to `custom-values.yaml`:

```yaml
jobs:
  dropSite:
    enabled: true
    siteName: "erp.example.com"
```

Note: Site `erp.example.com` must exist.

Generate Job YAML

```shell
helm template frappe-bench -n erpnext frappe/erpnext -f custom-values.yaml -s templates/job-drop-site.yaml > job-drop-site.yaml
```

Create Job resource

```shell
kubectl apply -f job-drop-site.yaml
```

### Configure service hosts

By default this job configures service hosts automatically as per name of the helm release.

To manually set hosts make following changes to `custom-values.yaml`:

```yaml
# Disable in-cluster databases and caches
mariadb-sts:
  enabled: false
valkey-cache:
  enabled: false
valkey-queue:
  enabled: false

# Configure external services
dbHost: "db-instance.123456789012.us-east-1.rds.amazonaws.com"
dbPort: 3306

externalRedis:
  cache: "redis://redis-cache.7abc2d.0001.usw2.cache.amazonaws.com:6379"
  queue: "redis://redis-queue.7abc2d.0001.usw2.cache.amazonaws.com:6379"

jobs:
  configure:
    enabled: true
```

Notes:

- Change the hosts as per configuration
- If `jobs.configure.fixVolume` is set to `true` it will run command as root to change ownership of files and directories in volume.

Generate Job YAML

```shell
helm template frappe-bench -n erpnext frappe/erpnext -f custom-values.yaml -s templates/job-configure-bench.yaml > job-configure-bench.yaml
```

Create Job resource

```shell
kubectl apply -f job-configure-bench.yaml
```

### Fix volume permission

Make following changes to `custom-values.yaml`:

```yaml
jobs:
  volumePermissions:
    enabled: true
```

Generate Job YAML

```shell
helm template frappe-bench -n erpnext frappe/erpnext -f custom-values.yaml -s templates/job-fix-volume-permission.yaml > job-fix-volume-permission.yaml
```

Create Job resource

```shell
kubectl apply -f job-fix-volume-permission.yaml
```

### Clear cache

Make following changes to `custom-values.yaml`:

```yaml
jobs:
  clearCache:
    enabled: true
  # Configure initContainer to wait for rollout
  initContainer:
    enabled: true
    image: alpine/kubectl:1.35.0
    deployment:
      name: "gunicorn"
    timeout: 600s
```

Generate Job YAML

```shell
helm template frappe-bench -n erpnext frappe/erpnext -f custom-values.yaml -s templates/job-clear-cache.yaml > job-clear-cache.yaml
```

Create Job resource

```shell
kubectl apply -f job-clear-cache.yaml
```

## Uninstall the Chart

To uninstall/delete the `frappe-bench` release:

```shell
helm --namespace erpnext delete frappe-bench
```

The command removes all the Kubernetes components installed by the chart and deletes the release.

## Migrating from Bitnami Subcharts

> **IMPORTANT:** If you are upgrading from a previous version (7.x or older) and relied on the default in-cluster database (Bitnami MariaDB or PostgreSQL), you **must** read this section and the [MIGRATION.md](https://github.com/frappe/helm/blob/main/erpnext/MIGRATION.md) guide before performing `helm upgrade`. The new chart version changes the default behavior, and failing to update your `custom-values.yaml` can lead to the accidental uninstallation of your database and potential data loss.
