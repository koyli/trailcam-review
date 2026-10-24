# Isolated runtime for the camera review: ffmpeg, Python with Pillow, and Claude Code.
# The kit folder, the footage and the secrets are mounted in at run time (see compose.yaml).
FROM debian:bookworm-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ffmpeg python3 python3-pil curl ca-certificates util-linux bash \
 && rm -rf /var/lib/apt/lists/*

# Run as your own user ID so files moved in ~/capture keep their normal ownership.
ARG UID=1000
ARG GID=1000
RUN groupadd -o -g "$GID" trailcam \
 && useradd -o -m -u "$UID" -g "$GID" -s /bin/bash trailcam

USER trailcam
ENV HOME=/home/trailcam \
    PATH=/home/trailcam/.local/bin:$PATH \
    CLAUDE_CONFIG_DIR=/home/trailcam/.claude-config \
    DISABLE_AUTOUPDATER=1

# Claude Code. Rebuild the image to update it.
RUN curl -fsSL https://claude.ai/install.sh | bash && claude --version

WORKDIR /app
ENTRYPOINT ["/app/run.sh"]
