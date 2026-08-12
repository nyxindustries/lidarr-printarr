FROM python:3.12-slim

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

ENTRYPOINT ["printarr"]
CMD ["watch"]
