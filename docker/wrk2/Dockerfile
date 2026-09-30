# Native DeathStarBench traffic generator used by Hotel and Social Network.
FROM ubuntu:22.04 AS builder

# DeathStarBench's fork implements the -D distribution option used by SREGym.
ARG DEATHSTARBENCH_COMMIT=6ecb09706140f8730b5385c08f1386c654c3c526
ARG LUAJIT_COMMIT=2090842410e0ba6f81fad310a77bf5432488249a

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential ca-certificates curl libssl-dev zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /build
RUN curl -fsSL https://github.com/delimitrou/DeathStarBench/archive/${DEATHSTARBENCH_COMMIT}.tar.gz \
      | tar -xz --strip-components=2 DeathStarBench-${DEATHSTARBENCH_COMMIT}/wrk2 \
    && curl -fsSL https://github.com/LuaJIT/LuaJIT/archive/${LUAJIT_COMMIT}.tar.gz \
      | tar -xz --strip-components=1 -C deps/luajit \
    # Histogram code uses portable compiler builtins; this unused x86 header
    # is the only architecture-specific dependency in the C sources.
    && sed -i '/#include <x86intrin.h>/d' src/hdr_histogram.c \
    && make -j$(nproc) \
    && make install

FROM ubuntu:22.04
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates libssl3 lua-socket \
    && mkdir -p /usr/local/lib/lua /usr/local/share/lua \
    && cp -aL /usr/lib/*-linux-gnu/lua/5.1 /usr/local/lib/lua/ \
    && ln -s /usr/share/lua/5.1 /usr/local/share/lua/5.1 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /usr/local/bin/wrk /usr/local/bin/wrk
CMD ["/bin/bash"]
