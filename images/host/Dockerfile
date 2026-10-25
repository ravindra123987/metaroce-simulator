# MetaRoCE host: libsoftmetaroce + rdma_gen (built by build-lsmr.sh into
# ./out), plus the tools scenarios use. Build context is the build dir:
#
#   docker build -f images/host/Dockerfile -t metaroce-host:latest ~/metaroce-build
FROM ubuntu:24.04 AS strip
RUN apt-get update -q && apt-get install -yq --no-install-recommends binutils && rm -rf /var/lib/apt/lists/*
COPY out /opt/metaroce
RUN find /opt/metaroce -type f \( -name '*.so*' -o -path '*/bin/*' \) -exec strip --strip-debug {} +

FROM ubuntu:24.04
ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update -q && apt-get install -yq --no-install-recommends \
      libre2-10 libsodium23 libevent-2.1-7t64 libsnappy1v5 libgflags2.2 libibverbs1 \
      libxxhash0 liblz4-1 libzstd1 libbz2-1.0 libssl3t64 \
      iproute2 tcpdump procps python3 ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=strip /opt/metaroce /opt/metaroce
ENV PATH=/opt/metaroce/bin:$PATH
CMD ["sleep", "infinity"]
