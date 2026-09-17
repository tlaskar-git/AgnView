FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

# Unprivileged runtime account. AgnView stores its token, adapters and
# notification config under $HOME/.agnview, so the home directory must be
# writable by this user.
RUN groupadd --gid 10001 agnview \
    && useradd --uid 10001 --gid 10001 --create-home --home-dir /home/agnview agnview

COPY pyproject.toml README.md ./
COPY agent_relay/ ./agent_relay/

RUN pip install --no-cache-dir -e . \
    && mkdir -p /home/agnview/.agnview \
    && chown -R agnview:agnview /app /home/agnview

ENV PYTHONUNBUFFERED=1
ENV AGENT_RELAY_PORT=8765
ENV HOME=/home/agnview

EXPOSE 8765

VOLUME ["/home/agnview/.agnview"]

USER agnview

# The dashboard root is served without authentication, so it is a safe
# liveness probe whether or not a pairing token is configured.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${AGENT_RELAY_PORT}/" > /dev/null || exit 1

ENTRYPOINT ["agnview"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8765"]
