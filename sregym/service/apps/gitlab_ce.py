"""GitLab CE prototype: external PostgreSQL and Redis, persistent Gitaly data."""

from sregym.service.apps.postgres_saas import PostgresSaaS

GITLAB_IMAGE = "gitlab/gitlab-ce:18.11.12-ce.0"
REDIS_IMAGE = "redis:7.4.2-alpine"


class GitLabCE(PostgresSaaS):
    slug = "gitlab-ce"
    frontend_port = 80
    health_path = "/-/health"
    startup_timeout = 1500
    data_volumes = ("gitlab-config", "gitlab-data", "gitlab-redis")
    auxiliary_deployments = ("gitlab-redis",)

    def probes(self, path=None):
        # GitLab's readiness endpoint is allowlisted to localhost by default.
        # Keep that restriction; the oracle separately tests the routed service.
        check = {
            "exec": {
                "command": ["curl", "--fail", "--silent", "--max-time", "5", "http://127.0.0.1/-/readiness?all=1"]
            },
            "timeoutSeconds": 6,
            "periodSeconds": 5,
        }
        return {"readinessProbe": check, "startupProbe": {**check, "failureThreshold": self.startup_timeout // 5}}

    def application_documents(self):
        config = "\n".join(
            [
                "external_url 'http://gitlab-ce'",
                "letsencrypt['enable'] = false",
                "postgresql['enable'] = false",
                "redis['enable'] = false",
                "gitlab_rails['db_adapter'] = 'postgresql'",
                "gitlab_rails['db_host'] = 'gitlab-ce-db-rw'",
                "gitlab_rails['db_port'] = 5432",
                "gitlab_rails['db_database'] = 'gitlab_ce'",
                "gitlab_rails['db_username'] = 'gitlab_ce'",
                "gitlab_rails['db_password'] = ENV.fetch('DATABASE_PASSWORD')",
                "gitlab_rails['db_sslmode'] = 'require'",
                "gitlab_rails['redis_host'] = 'gitlab-redis'",
                "gitlab_rails['redis_port'] = 6379",
                "gitlab_rails['initial_root_password'] = ENV.fetch('ROOT_PASSWORD')",
                "gitlab_rails['gitlab_signup_enabled'] = false",
                "gitlab_rails['smtp_enable'] = false",
                "gitlab_rails['gitlab_email_enabled'] = false",
                "gitlab_rails['usage_ping_enabled'] = false",
                "puma['worker_processes'] = 0",
                "puma['min_threads'] = 2",
                "puma['max_threads'] = 4",
                "sidekiq['concurrency'] = 5",
                "prometheus_monitoring['enable'] = false",
                "gitlab_kas['enable'] = false",
                "registry['enable'] = false",
                "nginx['worker_processes'] = 1",
            ]
        )
        volumes = [{"name": name, "persistentVolumeClaim": {"claimName": name}} for name in self.data_volumes[:2]]
        volumes += [
            {"name": "shm", "emptyDir": {"medium": "Memory", "sizeLimit": "256Mi"}},
            {"name": "logs", "emptyDir": {"sizeLimit": "512Mi"}},
        ]
        app = {
            "name": "gitlab",
            "image": GITLAB_IMAGE,
            "env": [
                {"name": "GITLAB_OMNIBUS_CONFIG", "value": config},
                self.secret_env("DATABASE_PASSWORD", "password", "application-database"),
                self.secret_env("ROOT_PASSWORD", "password"),
                self.secret_env("BENCHMARK_TOKEN", "token"),
            ],
            "volumeMounts": [
                {"name": "gitlab-config", "mountPath": "/etc/gitlab"},
                {"name": "gitlab-data", "mountPath": "/var/opt/gitlab"},
                {"name": "shm", "mountPath": "/dev/shm"},
                {"name": "logs", "mountPath": "/var/log/gitlab"},
            ],
            "resources": {"requests": {"cpu": "500m", "memory": "3Gi"}, "limits": {"memory": "6Gi"}},
            **self.probes(),
        }
        redis = {
            "name": "redis",
            "image": REDIS_IMAGE,
            "args": ["redis-server", "--appendonly", "yes", "--appendfsync", "always"],
            "volumeMounts": [{"name": "data", "mountPath": "/data"}],
            "readinessProbe": {"exec": {"command": ["redis-cli", "ping"]}},
            "resources": {"requests": {"cpu": "50m", "memory": "64Mi"}, "limits": {"memory": "256Mi"}},
        }
        return [
            self.deployment(self.slug, app, volumes),
            self.service("gitlab-redis", 6379),
            self.deployment(
                "gitlab-redis", redis, [{"name": "data", "persistentVolumeClaim": {"claimName": "gitlab-redis"}}]
            ),
        ]

    def initialize(self):
        # Only the per-attempt benchmark token is provisioned; no external account is used.
        ruby = """u = User.find_by_username!('root')
t = u.personal_access_tokens.find_or_initialize_by(name: 'sregym-prototype')
t.scopes = ['api']; t.expires_at = Date.today + 30
t.set_token(ENV.fetch('BENCHMARK_TOKEN')); t.save!
puts 'Benchmark API access initialized'
"""
        self.command(
            "exec", "-i", f"deployment/{self.slug}", "--", "gitlab-rails", "runner", "-", input_text=ruby, timeout=300
        )

    def record_query(self, token):
        return f"SELECT count(*) FROM issues WHERE title = 'sregym-{token}';"
