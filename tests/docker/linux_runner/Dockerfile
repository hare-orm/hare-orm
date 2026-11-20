FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH=/root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

RUN apt-get update \
    && apt-get install -y --no-install-recommends software-properties-common gpg-agent ca-certificates curl \
    && add-apt-repository -y ppa:deadsnakes/ppa \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        make gcc g++ git rsync libsqlite3-0 \
        python3.12 python3.12-venv python3.12-dev \
        python3.13 python3.13-venv python3.13-dev \
        python3.14 python3.14-venv python3.14-dev \
    && rm -rf /var/lib/apt/lists/*

RUN curl -LsSf https://astral.sh/uv/install.sh | sh \
    && uv tool install --python python3.14 poetry

# Ubuntu keeps backward-compatible IANA names such as US/Central in tzdata-legacy.
# Pyright needs Node; SQLite GIS tests need the loadable SpatiaLite extension.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata tzdata-legacy nodejs libsqlite3-mod-spatialite \
    && rm -rf /var/lib/apt/lists/*

RUN python3.14 -c 'from zoneinfo import ZoneInfo; assert ZoneInfo("US/Central").key == "US/Central"' \
    && node --version

CMD ["/bin/bash"]
