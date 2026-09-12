# jackcuii/hotel-reservation:latest is actually a Social Network runtime:
# none of Hotel's eight executable names exists on PATH. Keep that exact
# wrong-application failure mode using our existing multiarch Social build.
FROM ghcr.io/sregym/social-network:sha-d2c036bc3d1138f5a0bdaffd3d87f37522c3461a@sha256:70287c4756f9187613e49409291927c342724e3974f93709b21595ed1cdf1d69
RUN for service in frontend geo profile rate recommendation reservation search user; do \
      if command -v "$service"; then echo "Unexpected Hotel executable: $service" >&2; exit 1; fi; \
    done
