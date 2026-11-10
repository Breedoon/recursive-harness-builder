FROM obs-test-app:latest

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends strace \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /workspace/runtime /workspace/obs /home/agent \
    && chown 1000:1000 /workspace/runtime /workspace/obs /home/agent

CMD ["/bin/false"]
