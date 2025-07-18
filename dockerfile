# Use the latest Python slim image (Python 3.12)
FROM python:3.12-slim

# Set working directory
WORKDIR /app

# Install system dependencies for Chromium
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium \
    chromium-driver \
    libnss3 \
    libxss1 \
    libasound2 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    fonts-liberation \
    libgbm1 \
    libxrandr2 \
    libxdamage1 \
    libxi6 \
    libgconf-2-4 \
    libxcomposite1 \
    libxcursor1 \
    libx11-xcb1 \
    libxext6 \
    wget \
    ca-certificates \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Set environment variables
ENV CHROME_BIN=/usr/bin/chromium
ENV DISPLAY=:99

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY twickets.py .

# Run your Python app
CMD ["python", "twickets.py"]
