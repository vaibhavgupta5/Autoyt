FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core ca-certificates util-linux \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
COPY autoyt ./autoyt
# `deno` is the JavaScript runtime yt-dlp needs to read YouTube pages; the bgutil plugin fetches PO tokens
# from the bgutil-provider service (same version as its image in docker-compose.yml).
RUN pip install --no-cache-dir . deno "bgutil-ytdlp-pot-provider==2.0.2"

ENV AUTOYT_HOME=/app \
    HF_HOME=/app/data/models \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

ENTRYPOINT ["autoyt"]
CMD ["daemon"]
