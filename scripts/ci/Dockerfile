# Python and uv base pinned by digest; upgrade deliberately and requalify both jobs.
FROM ghcr.io/astral-sh/uv:0.8.15-python3.11-bookworm-slim@sha256:a5496800aed99aa347f859d355073138f7a9929ec2049a6e116c1cdf36676533
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 ci \
    && mkdir /workspace && chown ci:ci /workspace
WORKDIR /workspace
COPY --chown=10001:10001 . /workspace
ENV HOME=/home/ci UV_NO_CONFIG=1 UV_LINK_MODE=copy \
    TRADING_MODE=PAPER BROKER_ENVIRONMENT=PAPER \
    LIVE_TRADING_ACKNOWLEDGED=false KILL_SWITCH=true PYTHONUNBUFFERED=1
USER 10001:10001
# Network is available only for locked dependency preparation. Checks run with --network=none.
RUN uv --version && python --version && git --version \
    && uv lock --check && uv sync --frozen --group dev
CMD ["python", "scripts/ci_checks.py", "--offline"]
