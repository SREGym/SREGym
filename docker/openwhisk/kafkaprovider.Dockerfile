ARG TARGETARCH
FROM --platform=linux/amd64 openwhisk/kafkaprovider:2.1.0@sha256:63dc3d2a090493f8100cd32ee2dee883298c53cab319befd61eb1fb47534ddc6 AS runtime-amd64
FROM python:2.7.18-buster@sha256:d8fac68ebdc45b8d66d53f1ed6c1532da81109a8f5532a6ca0c951ed31107d70 AS runtime-arm64
# The upstream image retains librdkafka's cleaned source tree. Rebuild it and
# every Python native extension instead of copying AMD64 shared libraries.
COPY --from=runtime-amd64 /librdkafka/ /tmp/librdkafka/
RUN cd /tmp/librdkafka && ./configure && make -j2 && make install \
    && ldconfig && rm -rf /tmp/librdkafka
COPY kafkaprovider-requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY --from=runtime-amd64 /KafkaFeedProvider/ /KafkaFeedProvider/
ENV PORT=5000 LOCAL_DEV=False GENERIC_KAFKA=True LD_LIBRARY_PATH=/usr/local/lib
HEALTHCHECK --interval=5m --timeout=1m CMD curl -m 30 --fail http://localhost:5000/health || killall python
CMD ["/bin/bash", "-c", "cd KafkaFeedProvider && python -u app.py"]
FROM runtime-${TARGETARCH}
