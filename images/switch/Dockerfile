# Fabric switch image:
# Linux kernel forwarding, tc for shaping/ECN/netem, tcpdump for captures.
FROM alpine:3.20
RUN apk add --no-cache iproute2 iproute2-tc tcpdump ethtool python3
CMD ["sleep", "infinity"]
