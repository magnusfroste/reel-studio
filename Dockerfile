FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    REEL_TRANSPORT=http \
    REEL_OUTPUT_DIR=/data \
    HOST=0.0.0.0 \
    PORT=8000

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        python3 \
        python3-pip \
        python3-venv \
        xvfb \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY reel_studio ./reel_studio
COPY examples ./examples

RUN python3 -m pip install --break-system-packages . \
    && python3 -m playwright install --with-deps chromium

VOLUME ["/data"]
EXPOSE 8000

CMD ["python3", "-m", "reel_studio.server"]
