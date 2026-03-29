# Use a specific Python 3.13.3 base image
FROM python:3.13.3-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    curl \
    dnsutils \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY . ./backend
RUN pip3 install --no-cache-dir -r backend/requirements.txt
EXPOSE 5000
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "5000"]
