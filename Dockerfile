FROM python:3.10-slim

ENV PYTHONUNBUFFERED=1 \
    DATA_DIR=/app/data
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin botuser \
    && mkdir -p /app/data \
    && chown -R botuser:botuser /app

COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=botuser:botuser . .
USER botuser

EXPOSE 7860
CMD ["python", "main.py"]
