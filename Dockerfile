FROM python:3.12-slim

WORKDIR /app

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc && \
    rm -rf /var/lib/apt/lists/*

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code
COPY . .

# Create dirs for static/templates if missing
RUN mkdir -p openboek/static openboek/templates

EXPOSE ${APP_PORT:-8070}

CMD ["sh", "-c", "uvicorn openboek.main:app --host ${APP_HOST:-0.0.0.0} --port ${APP_PORT:-8070}"]
