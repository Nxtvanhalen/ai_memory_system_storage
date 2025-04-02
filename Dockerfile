# syntax=docker/dockerfile:1
FROM --platform=$BUILDPLATFORM python:3.11-slim

# Set work directory
WORKDIR /app

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    python3-dev \
    libmagic1 \
    unzip \
    poppler-utils \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first to leverage Docker cache
COPY requirements.txt .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PORT=8080

# Create simpler entrypoint that explicitly uses the PORT env var
RUN echo '#!/bin/bash\n\
exec gunicorn --bind :$PORT --workers=4 --threads=8 --timeout=0 app:app' > /entrypoint.sh \
&& chmod +x /entrypoint.sh

# Set the entrypoint
ENTRYPOINT ["/entrypoint.sh"]
