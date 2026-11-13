ARG UPSTREAM_IMAGE=ghcr.io/sregym/slack-spine:20261014-loadgen@sha256:db43c7ee572a95c87fb5ff089c413aa3319c3b978a8bc9ea46332ed2fbae1665
FROM ${UPSTREAM_IMAGE}
COPY session_dependencies.py sregym_loadgen.py /app/
CMD ["python", "sregym_loadgen.py"]
