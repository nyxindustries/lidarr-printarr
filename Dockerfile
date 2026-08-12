FROM python:3.12-slim

LABEL org.opencontainers.image.source="https://github.com/nyxindustries/lidarr-printarr" \
      org.opencontainers.image.description="Acoustic-fingerprint music identifier, tagger and renamer for Lidarr" \
      org.opencontainers.image.licenses="MIT"

# fpcalc (Chromaprint) does the acoustic fingerprinting
RUN apt-get update \
    && apt-get install -y --no-install-recommends libchromaprint-tools \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY printarr ./printarr
RUN pip install --no-cache-dir .

# Default config location inside the container
VOLUME /config
WORKDIR /config

# Review web UI (enable via [web] in printarr.toml, host = "0.0.0.0")
EXPOSE 8687

ENTRYPOINT ["printarr"]
CMD ["watch"]
