FROM python:3.13-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY botroll ./botroll
# The commit to report on /health: --build-arg GIT_SHA=$(git rev-parse HEAD), or Railway's own variable on a
# GitHub deploy. Without either, a `git archive` upload already has it in botroll/_build.py (export-subst).
ARG GIT_SHA=""
ARG RAILWAY_GIT_COMMIT_SHA=""
RUN sha="$(printf '%s' "${GIT_SHA:-$RAILWAY_GIT_COMMIT_SHA}" | tr 'A-F' 'a-f')"; \
    if [ -n "$sha" ]; then \
      case "$sha" in *[!0-9a-f]*) echo "GIT_SHA must be a hex commit id" >&2; exit 1;; esac; \
      printf 'COMMIT = "%s"\n' "$sha" > botroll/_build.py; \
    fi \
    && pip install --no-cache-dir . \
    && useradd --system --uid 1000 --home /data --shell /usr/sbin/nologin botroll \
    && mkdir -p /data && chown botroll:botroll /data \
    && rm -rf /app
WORKDIR /
ENV BOTROLL_DATA=/data PYTHONUNBUFFERED=1
# Starts as root so a Railway volume at /data can be chowned; serve() then drops to uid 1000.
# SIGTERM is handled so a stop or redeploy doesn't wait 10s for a kill.
# --behind-proxy trusts X-Forwarded-Proto for HSTS only. Client IP stays the
# socket peer unless BOTROLL_CLIENT_IP_HEADER is set (Railway: X-Real-IP).
# Docker/Railway stamp logs in UTC unless TZ is set; board ages use BOTROLL_TZ.
CMD ["botroll", "serve", "--behind-proxy"]
