# Keep the existing installer and 46 service images. Only its embedded
# database/helper image references change; no nested submodule edits are needed.
FROM ghcr.io/sregym/train-ticket-deploy:latest@sha256:3a78d7b1c5dc002552dd9b90cc83c3cd35bca16efc31aff30c37291ad361efa5 AS original
FROM --platform=$BUILDPLATFORM python:3.12-slim AS manifests
RUN pip install --no-cache-dir PyYAML==6.0.3
WORKDIR /work
COPY --from=original /usr/local/bin/deployment/ /work/deployment/
COPY pin_images.py /work/pin_images.py
COPY --from=image-releases images.lock.json /work/images.lock.json
RUN python pin_images.py deployment images.lock.json
FROM original
COPY --from=manifests /work/deployment/ /usr/local/bin/deployment/
