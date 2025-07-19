# Use Python 3.12 slim image

FROM python:3.12-slim

# Set working directory

WORKDIR /app

# Install system dependencies in one layer
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium chromium-driver xvfb \
    libnss3 libxss1 libasound2 libatk1.0-0 libatk-bridge2.0-0 \
    fonts-liberation libgbm1 libxrandr2 libxdamage1 libxi6 \
    libgconf-2-4 libxcomposite1 libxcursor1 libx11-xcb1 libxext6 \
    ca-certificates && \
    apt-get clean && rm -rf /var/lib/apt/lists/* && \
    [ -f /usr/bin/chromium ] || (echo "Chromium not found at /usr/bin/chromium" && exit 1)

# Set environment variables

ENV CHROME_BIN=/usr/bin/chromium 

ENV CHROMEDRIVER_PATH=/usr/bin/chromedriver 

ENV DISPLAY=:99

# Create directories for logs and data

RUN mkdir -p /app/logs /app/data && chmod -R 777 /app/logs /app/data

# Copy requirements and install

COPY requirements.txt . 

RUN pip install --no-cache-dir -r requirements.txt

# Copy application code

COPY twickets.py .

# Start Xvfb and ChromeDriver with optimized settings

CMD ["sh", "-c", "Xvfb :99 -screen 0 1024x768x24 -nolisten tcp & /usr/bin/chromedriver --port=37207 --whitelisted-ips= --allowed-origins=* & python twickets.py"]