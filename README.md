# Service_RUG

Microservice worker for querying and scraping the Mexican **Registro Único de Garantías Mobiliarias (RUG)** portal by RFC or CURP.

## Overview

- **Authentication**: Automates session login to the RUG portal.
- **Extraction**: Searches guarantee records, extracts details, tables, operations, and downloads certification PDFs.
- **Storage**:
  - Metadata & deduplicated records saved to **MongoDB** (`rug_garantias`, `rug_index`).
  - Raw evidence (PDFs/HTML) uploaded to **AWS S3**.
- **Execution Modes**:
  - Service Queue Worker (`tasks_queue` collection in MongoDB).
  - Single Target CLI Runner (`--id <RFC_OR_CURP>`).

## Setup & Configuration

1. Clone repository and install dependencies:
```bash
python -m venv venv
# On Windows:
venv\Scripts\activate
# On Linux/macOS:
source venv/bin/activate

pip install -r requirements.txt
```

2. Configure environment variables in `.env` (refer to `.env.example`):
```env
MONGO_URI=mongodb://localhost:27017/
AWS_S3_BUCKET=searchlook-raw-data
RUG_EMAIL=your_email@domain.com
RUG_PASS=your_password
POLL_INTERVAL=10
```

## Running

### Single ID Run
```bash
python main.py --id <RFC_OR_CURP>
```

### Worker Daemon Mode
```bash
python main.py
```

### Docker
```bash
docker build -t service-rug .
docker run --env-file .env service-rug
```
