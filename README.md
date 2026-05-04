# Oracle Fusion SQL Agent

This repository contains the backend service and deployment configuration for the Oracle Fusion SQL Generator Agent. It provides a schema-aware, agentic SQL generation workflow backed by a DuckDB database.

## Components

- `contabo-api/`: Contains the Flask/Gunicorn API server, Dockerfile, and Nginx configuration for hosting the service on a VPS.
- `inspect_db.py`: Utility script to inspect the DuckDB metadata database.

## Deployment

The service is designed to be deployed on a Contabo VPS (or any Linux server) using Docker and Nginx.

### Setup Instructions

1. Clone this repository on your server:
   ```bash
   git clone https://github.com/pvsairam/sql-agent.git
   cd sql-agent
   ```

2. Download the `metadata.db` database file (not included in the repository due to size constraints) into the root of the project:
   ```bash
   # Example using wget (replace with actual URL if hosted elsewhere, or use scp from local)
   # wget <URL_TO_METADATA_DB> -O metadata.db
   ```

3. Configure environment variables:
   ```bash
   cd contabo-api
   cp .env.example .env
   # Edit .env with your specific configuration
   ```

4. Build and run using Docker:
   ```bash
   docker build -t sql-agent-api .
   docker run -d -p 5000:5000 --name sql-agent-container -v $(pwd)/../metadata.db:/app/metadata.db -v $(pwd)/.env:/app/.env sql-agent-api
   ```

## Database Transfer Note

The `metadata.db` file is approximately 878 MB and cannot be pushed to GitHub. You must transfer it manually to your server using tools like `scp`, `sftp`, or by uploading it to a cloud storage provider and downloading it with `wget`.
