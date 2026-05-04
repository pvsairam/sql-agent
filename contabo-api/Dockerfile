FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY app.py .

# The metadata.db will be mounted as a volume, not baked into the image
# This keeps the Docker image small (~200MB instead of 1.1GB)

EXPOSE 5000

# Run with gunicorn for production (4 workers, 120s timeout for large queries)
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "4", "--timeout", "120", "app:app"]
