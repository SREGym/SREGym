"""Mattermost Team Edition with retained attachments and external PostgreSQL."""

from sregym.service.apps.postgres_saas import PostgresSaaS

MATTERMOST_IMAGE = "mattermost/mattermost-team-edition:11.7.0"


class Mattermost(PostgresSaaS):
    slug = "mattermost"
    frontend_port = 8065
    health_path = "/api/v4/system/ping"
    data_volumes = ("mattermost-data",)

    def application_documents(self):
        folders = ("config", "data", "logs", "plugins", "client-plugins", "bleve-indexes")
        env = {
            "MM_SQLSETTINGS_DRIVERNAME": "postgres",
            "MM_SQLSETTINGS_DATASOURCE": "postgres://mattermost:$(DATABASE_PASSWORD)@mattermost-db-rw:5432/mattermost?sslmode=require&connect_timeout=10",
            "MM_SERVICESETTINGS_SITEURL": "http://mattermost:8065",
            "MM_SERVICESETTINGS_ENABLELOCALMODE": "true",
            "MM_EMAILSETTINGS_SENDEMAILNOTIFICATIONS": "false",
            "MM_EMAILSETTINGS_REQUIREEMAILVERIFICATION": "false",
            "MM_TEAMSETTINGS_ENABLEOPENSERVER": "true",
            "MM_SERVICESETTINGS_ENABLEEMAILINVITATIONS": "false",
            "MM_PLUGINSETTINGS_ENABLE": "false",
            "MM_LOGSETTINGS_CONSOLELEVEL": "INFO",
            "MM_LOGSETTINGS_ENABLEDIAGNOSTICS": "false",
            "MM_SERVICESETTINGS_ENABLEBOTACCOUNTCREATION": "true",
            "MM_SERVICESETTINGS_ENABLEOUTGOINGWEBHOOKS": "true",
        }
        volume = [{"name": "data", "persistentVolumeClaim": {"claimName": "mattermost-data"}}]
        init = {
            "name": "prepare-storage",
            "image": "busybox:1.36.1",
            "command": [
                "sh",
                "-c",
                "mkdir -p " + " ".join(f"/storage/{p}" for p in folders) + " && chown -R 2000:2000 /storage",
            ],
            "volumeMounts": [{"name": "data", "mountPath": "/storage"}],
        }
        app = {
            "name": "mattermost",
            "image": MATTERMOST_IMAGE,
            "env": [self.secret_env("DATABASE_PASSWORD", "password", "application-database")]
            + [{"name": k, "value": v} for k, v in env.items()],
            "volumeMounts": [
                {
                    "name": "data",
                    "mountPath": "/mattermost/" + ("client/plugins" if p == "client-plugins" else p),
                    "subPath": p,
                }
                for p in folders
            ],
            "resources": {"requests": {"cpu": "200m", "memory": "512Mi"}, "limits": {"memory": "2Gi"}},
            **self.probes(),
        }
        return [self.deployment(self.slug, app, volume, initContainers=[init], securityContext={"fsGroup": 2000})]

    def record_query(self, token):
        return f"SELECT count(*) FROM posts WHERE message = 'sregym-{token}' AND deleteat = 0;"
