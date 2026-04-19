<div align="center">

# 📒 OpenBoek

**Self-hosted, open-source bookkeeping and tax preparation for Dutch small businesses.**

*Dutch-first. Privacy-first. Extensible to any country.*

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/Python-3.12+-green.svg)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688.svg)](https://fastapi.tiangolo.com)

<!-- TODO: Add screenshot -->
<!-- ![OpenBoek Dashboard](docs/screenshots/dashboard.png) -->

</div>

---

## What is OpenBoek?

OpenBoek is a self-hosted bookkeeping application built specifically for Dutch freelancers (ZZP), BV structures, and small businesses. Your financial data never leaves your machine — all processing, including AI-powered receipt scanning and tax advice, runs locally via [Ollama](https://ollama.com). It handles double-entry bookkeeping with RGS-compliant charts of accounts, Dutch bank statement import (MT940/CSV), and prepares your BTW-aangifte, IB, and VPB filings.

While built Dutch-first, the architecture is designed around pluggable **tax modules** — making it extensible to any country's tax system.

## ✨ Features

### What works today (Phase 1)

- **Double-entry bookkeeping** — journal entries with debit/credit validation, draft → posted → locked lifecycle
- **RGS-compliant chart of accounts** — pre-built templates for ZZP, BV, and personal entities
- **Multi-entity support** — ZZP, BV, Holding, Personal with entity isolation
- **Invoicing** — sales and purchase invoices with line items and BTW rates
- **Bank statement import** — MT940 parser for Dutch banks (ING, Rabobank, ABN AMRO)
- **Bank reconciliation** — match imported transactions to journal entries
- **Financial reports** — trial balance, profit & loss, balance sheet with date filtering
- **Authentication** — user registration/login, session-based auth, entity-level access control
- **Audit logging** — automatic, append-only audit trail for all state changes
- **Bilingual UI** — full Dutch and English support throughout
- **Dark theme** — mobile-first responsive design with Tailwind CSS + HTMX
- **Setup wizard** — guided entity creation with Dutch-specific questions

### Planned (Phases 2–4)

- 🧾 BTW-aangifte, IB, and VPB tax preparation with triple verification
- 📸 Receipt scanning via local AI OCR (Ollama + minicpm-v)
- 🤖 AI Tax Consultant — local, privacy-first, with Dutch tax knowledge base
- 🏦 Real-time bank sync via GoCardless (PSD2)
- 👫 Fiscal partnership optimization
- 💶 Multi-currency support (EUR/USD/GBP with ECB rates)
- 📄 PDF invoice generation
- 🔄 Recurring transactions

## 🚀 Quick Start

### Docker Compose (recommended)

```bash
git clone https://github.com/openboek/openboek.git
cd openboek
cp .env.example .env
# Edit .env — at minimum set a real SECRET_KEY

docker compose up -d

# Apply database schema
docker compose exec db psql -U openboek -d openboek -f /dev/stdin < migrations/001_initial.sql
docker compose exec db psql -U openboek -d openboek -f /dev/stdin < migrations/002_features.sql

# Open http://localhost:8070
# Register your first account and create an entity via the setup wizard
```

### Bare metal

```bash
git clone https://github.com/openboek/openboek.git
cd openboek

# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Set up PostgreSQL (must be running separately)
cp .env.example .env
# Edit .env with your DATABASE_URL and SECRET_KEY

# Apply migrations
psql -h localhost -U openboek -d openboek -f migrations/001_initial.sql
psql -h localhost -U openboek -d openboek -f migrations/002_features.sql

# Start the application
uvicorn openboek.main:app --host 0.0.0.0 --port 8070

# Open http://localhost:8070
```

## ⚙️ Configuration

All configuration is via environment variables (or `.env` file):

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `postgresql+asyncpg://openboek:openboek@localhost:5432/openboek` | PostgreSQL connection string |
| `SECRET_KEY` | `change-me-in-production` | Session signing key — **change this!** |
| `OLLAMA_URL` | `http://localhost:11434` | Ollama API endpoint |
| `OLLAMA_MODEL` | `gemma4` | Default LLM for AI features |
| `APP_LANG` | `nl` | Default language (`nl` or `en`) |
| `APP_PORT` | `8070` | HTTP port |
| `APP_HOST` | `0.0.0.0` | Bind address |

## 🏗️ Tech Stack

| Layer | Technology | Why |
|-------|-----------|-----|
| Backend | **FastAPI** + Python 3.12 | Async, fast, excellent OpenAPI docs |
| ORM | **SQLAlchemy 2.0** (async) | Schema flexibility, mature ecosystem |
| Database | **PostgreSQL 16** | Strong ACID, JSONB, multi-schema support |
| Frontend | **HTMX** + **Tailwind CSS** | No JS build step, server-driven, mobile-first |
| Templates | **Jinja2** | Server-side rendering |
| Auth | **Argon2** password hashing | Industry-standard secure hashing |
| Bank import | **MT940** parser | Standard format for Dutch banks |
| AI (planned) | **Ollama** (local) | Privacy — financial data never leaves your machine |
| Containerization | **Docker** + Docker Compose | Easy self-hosting |

## 📐 Architecture

OpenBoek follows a modular architecture organized by domain:

```
openboek/
├── openboek/
│   ├── main.py              # FastAPI app entry point
│   ├── config.py            # Pydantic settings
│   ├── auth/                # Authentication & sessions
│   ├── entities/            # Entity management (ZZP, BV, etc.)
│   ├── accounting/          # Double-entry bookkeeping core
│   ├── invoices/            # Sales & purchase invoices
│   ├── banking/             # MT940 import, reconciliation, GoCardless
│   ├── scanner/             # Receipt OCR pipeline
│   ├── reports/             # Financial reports
│   ├── tax/                 # Tax calculations & fiscal partner optimization
│   ├── verification/        # Triple-verification system
│   ├── wizard/              # Setup wizard
│   ├── audit/               # Audit logging
│   ├── dashboard/           # Dashboard
│   ├── i18n/                # Translations (NL/EN)
│   └── templates/           # Jinja2 templates
├── tax_modules/
│   └── nl/                  # Dutch tax module (pluggable)
├── migrations/              # SQL migrations
├── docker-compose.yml
└── Dockerfile
```

For the full architectural specification (database design, data flows, AI pipeline, security model), see [docs/openboek-architecture.md](docs/openboek-architecture.md).

## 🇳🇱 Tax Modules

OpenBoek's tax logic is not hardcoded — it lives in pluggable **tax modules** under `tax_modules/`.

### The Dutch module (`tax_modules/nl/`)

Ships with OpenBoek and includes:

- **RGS chart of accounts** templates (`zzp.yaml`, `bv.yaml`, `personal.yaml`)
- **Setup wizard** flow with Dutch-specific questions (`wizard.yaml`)
- **BTW rubrieken** mapping (1a through 5b)
- **Tax rules** (brackets, rates, deductions) as versioned YAML per fiscal year

### Adding a new country

To add support for another country (e.g., Germany):

1. Create `tax_modules/de/` with entity types, chart of accounts templates, and wizard flow
2. Implement the `TaxModuleBase` interface
3. Add tax rules as YAML files under `tax_modules/de/tax_rules/YYYY/`
4. No changes to core application code required

The tax module system is designed so that annual threshold updates (new tax brackets, rate changes) only require YAML edits — no Python code changes.

## 🤖 AI Features

OpenBoek uses **local AI models** via Ollama for privacy-sensitive financial operations. Your data never leaves your machine.

| Feature | Model | Purpose |
|---------|-------|---------|
| Receipt OCR | `minicpm-v:8b` | Extract text from scanned receipts/invoices |
| Text cleanup | `reader-lm:1.5b` | Structure raw OCR output into clean data |
| Tax consultant | `gemma4` (configurable) | Answer tax questions, review filings, suggest optimizations |

**No GPU? No problem.** All AI features degrade gracefully — bookkeeping, banking, and reporting work fully without Ollama. AI features simply show "AI unavailable" and let you proceed manually.

## 🤝 Contributing

Contributions are welcome! Whether it's fixing a bug, improving translations, updating tax rules for a new fiscal year, or adding a tax module for your country.

Please read [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines on:
- Setting up a development environment
- Code style and conventions
- Submitting pull requests
- Tax rule contributions

## 📋 Roadmap

| Phase | Focus | Status |
|-------|-------|--------|
| **Phase 1** | Foundation — bookkeeping, invoicing, bank import, reports | ✅ Complete |
| **Phase 2** | Tax Compliance — BTW/IB/VPB preparation, automated checks | 🔲 Planned |
| **Phase 3** | AI Integration — receipt OCR, AI tax consultant, knowledge base | 🔲 Planned |
| **Phase 4** | Polish & Release — verification system, backups, tests, docs | 🔲 Planned |

See the [architecture spec](docs/openboek-architecture.md#14-development-phases) for detailed phase breakdowns.

## 🔒 Security

Found a vulnerability? Please report it responsibly. See [SECURITY.md](SECURITY.md) for our disclosure policy.

## 📄 License

OpenBoek is licensed under the [Apache License 2.0](LICENSE).

**Why Apache 2.0?** It's a permissive, business-friendly license that lets anyone use, modify, and distribute OpenBoek — including in proprietary products — while providing patent protection for contributors and users. It's the same license used by Kubernetes, Apache HTTP Server, and many other foundational open-source projects.

---

<div align="center">

Built for 🇳🇱 Dutch small businesses · Privacy-first · Self-hosted

</div>
