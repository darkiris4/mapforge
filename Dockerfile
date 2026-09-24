# MapForge — self-hosted map fetch / bound / convert service.
# GDAL comes bundled inside the rasterio wheels, so no system GDAL packages are needed.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MAPFORGE_DATA=/data \
    MAPFORGE_LIBRARY=/library \
    MAPFORGE_HOST=0.0.0.0 \
    MAPFORGE_PORT=8765

# ca-certificates: HTTPS to FAA/USGS/AWS. Add your DoD / enterprise CA bundle at runtime
# (mount it and reference it in an endpoint's "CA bundle" field).
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 10001 --shell /usr/sbin/nologin mapforge \
 && mkdir -p /data /library \
 && chown mapforge:mapforge /data /library

WORKDIR /opt/mapforge
COPY pyproject.toml README.md ./
COPY mapforge ./mapforge
RUN pip install . && rm -rf /opt/mapforge/build

USER mapforge
VOLUME ["/data", "/library"]
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request as u; r=u.Request('http://127.0.0.1:%s/api/info' % os.environ.get('MAPFORGE_PORT','8765'), headers={'X-MapForge-Token': os.environ.get('MAPFORGE_TOKEN','')}); u.urlopen(r, timeout=4)" || exit 1

CMD ["mapforge"]
