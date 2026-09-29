FROM grafana/grafana:12.3.1

LABEL org.opencontainers.image.source="https://github.com/SREGym/SREGym"

# Keep plugins outside the chart's /var/lib/grafana volume. A fresh pod must
# use the baked files even when its data volume is empty.
ENV GF_PATHS_PLUGINS=/opt/grafana/plugins \
    GF_PLUGINS_PREINSTALL_DISABLED=true \
    GF_PLUGINS_PUBLIC_KEY_RETRIEVAL_DISABLED=true \
    GF_ANALYTICS_CHECK_FOR_UPDATES=false \
    GF_ANALYTICS_CHECK_FOR_PLUGIN_UPDATES=false

USER root
RUN mkdir -p "$GF_PATHS_PLUGINS" \
    && grafana cli --pluginsDir "$GF_PATHS_PLUGINS" plugins install grafana-opensearch-datasource 2.34.3 \
    && chown -R 472:0 "$GF_PATHS_PLUGINS"
USER 472
