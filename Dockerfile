FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN mkdir -p /app/data

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN playwright install --with-deps chromium

COPY . .

ENV SIRIUS_HOST=0.0.0.0
ENV SIRIUS_PORT=8000
ENV DB_PATH=/app/data/sirius_web.sqlite3

EXPOSE 8000

CMD ["sh", "-c", "python scripts/create_encryption_key.py && exec python main.py"]
