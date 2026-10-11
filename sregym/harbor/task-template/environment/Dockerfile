# One unprivileged container runs the whole task: a four-node k3s cluster, the
# SREGym backend that deploys and grades the problem, and the agent, which runs
# as an unprivileged user. sregym-ready (the healthcheck) starts the backend.
FROM {{backend_image}}

# Root-only: the agent must not learn which fault was injected.
RUN install -d -m 700 /etc/sregym \
    && echo '{{problem_id}}' > /etc/sregym/problem \
    && echo '{{oracle_token_sha256}}' > /etc/sregym/oracle-token-sha256 \
    && echo '{{steady_state_s}}' > /etc/sregym/steady-state-seconds \
    && chmod 600 /etc/sregym/problem /etc/sregym/oracle-token-sha256 /etc/sregym/steady-state-seconds
