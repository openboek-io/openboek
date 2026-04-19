# OpenBoek

Self-hosted, open-source bookkeeping and tax preparation for Dutch small businesses, freelancers (ZZP), and BV structures.

## Features

- **Double-entry bookkeeping** with RGS-compatible chart of accounts
- **Multi-entity support** — ZZP, BV, Holding, Personal
- **Receipt scanning** via local AI OCR (Ollama)
- **Bank import** — MT940, CSV
- **Tax preparation** — BTW-aangifte, IB, VPB
- **AI Tax Consultant** — local Ollama-powered, privacy-first
- **Bilingual** — Dutch & English throughout

## Quick Start

```bash
# Clone and configure
git clone https://github.com/openboek/openboek
cd openboek
cp .env.example .env
# Edit .env with your settings

# Run with Docker
docker compose up -d

# Apply database schema
psql -h localhost -U openboek -d openboek -f migrations/001_initial.sql

# Open http://localhost:8070
```

## Development (without Docker)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Start PostgreSQL separately, then:
uvicorn openboek.main:app --reload --host 0.0.0.0 --port 8070
```

## Configuration

All configuration is via environment variables. See `.env.example` for available options.

## License

AGPL-3.0-only — see [LICENSE](LICENSE).
