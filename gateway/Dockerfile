FROM python:3.12-slim

WORKDIR /app

# Install system dependencies for MetaTrader5 (requires libgomp)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps
COPY gateway/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY gateway/ /app/
COPY shared/ /shared/

EXPOSE 9000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "9000"]
