FROM python:3.12-slim-bookworm AS cpu
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    CROWBARR_DATA=/config HF_HOME=/config/models/huggingface \
    TORCH_HOME=/config/models/torch NLTK_DATA=/config/models/nltk HOME=/config
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libgomp1 \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml README.md LICENSE ./
COPY crowbarr ./crowbarr
RUN pip install --no-cache-dir torch==2.8.0 torchaudio==2.8.0 torchvision==0.23.0 \
    --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir '.[inference]' torchcodec==0.7.0 \
    && pip check
RUN mkdir -p /config && chown 1000:1000 /config
USER 1000:1000
EXPOSE 8449
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8449/health', timeout=3)"
ENTRYPOINT ["crowbarr"]
