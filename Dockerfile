FROM python:3.13-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY botroll ./botroll
RUN pip install --no-cache-dir . \
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
