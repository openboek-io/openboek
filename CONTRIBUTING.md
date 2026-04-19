# Contributing to OpenBoek

Thank you for considering contributing to OpenBoek! Every contribution helps make self-hosted bookkeeping better for Dutch small businesses (and eventually beyond).

## 🏁 Getting Started

### Prerequisites

- Python 3.12+
- PostgreSQL 16+
- Git
- (Optional) [Ollama](https://ollama.com) for AI features

### Development Setup

```bash
# Fork and clone the repository
git clone https://github.com/YOUR_USERNAME/openboek.git
cd openboek

# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Copy and configure environment
cp .env.example .env
# Edit .env with your local PostgreSQL credentials

# Apply database migrations
psql -h localhost -U openboek -d openboek -f migrations/001_initial.sql
psql -h localhost -U openboek -d openboek -f migrations/002_features.sql

# Run the development server
uvicorn openboek.main:app --reload --host 0.0.0.0 --port 8070
```

## 🔀 Workflow

1. **Check existing issues** — someone may already be working on it
2. **Open an issue first** for significant changes (new features, architecture changes)
3. **Fork the repository** and create a feature branch:
   ```bash
   git checkout -b feature/your-feature-name
   ```
4. **Make your changes** — keep commits focused and well-described
5. **Test your changes** (see below)
6. **Submit a pull request** against `main`

### Branch Naming

- `feature/description` — new features
- `fix/description` — bug fixes
- `docs/description` — documentation changes
- `tax/nl/YYYY` — Dutch tax rule updates for a specific year
- `tax/XX` — new country tax module

## 📝 Code Style

- **Python**: follow PEP 8, use type hints
- **SQL**: uppercase keywords, lowercase identifiers
- **Templates**: Jinja2, consistent indentation
- **i18n**: all user-facing strings must use translation keys — never hardcode text
- **Bilingual**: new features must have both Dutch and English translations

### Commit Messages

Use [Conventional Commits](https://www.conventionalcommits.org/):

```
feat: add ICP reporting for EU cross-border transactions
fix: correct BTW rubriek 2a calculation for reverse charge
docs: update README with GoCardless setup instructions
tax(nl): update 2027 VPB brackets per Belastingplan
i18n: add German translations for setup wizard
```

## 🧪 Testing

```bash
# Run the test suite
pytest

# Run with coverage
pytest --cov=openboek

# Run specific test module
pytest tests/test_accounting.py
```

### What to test

- Tax calculations — exact figures matter. Include test cases with known correct outputs.
- Bank import parsers — provide sample MT940/CSV fixtures (anonymized).
- Journal entry validation — debit must equal credit, always.

## 🧾 Tax Rule Contributions

Tax rules are stored as YAML files in `tax_modules/nl/tax_rules/YYYY/`. Annual updates (new brackets, rates, thresholds) are **high-priority, easy contributions**.

### Updating Dutch tax rules for a new year

1. Copy the previous year's folder: `cp -r tax_modules/nl/tax_rules/2026 tax_modules/nl/tax_rules/2027`
2. Update values based on official Belastingdienst publications
3. Update `changelog.md` with what changed and link to the official source
4. Submit a PR with the title: `tax(nl): 2027 tax rules per Belastingplan 2027`

### Adding a new country module

See [docs/openboek-architecture.md](docs/openboek-architecture.md) section 10 for the tax module interface. A new country module needs:

- `__init__.py` implementing `TaxModuleBase`
- `entity_types.yaml` — supported business structures
- `chart_of_accounts/` — standard chart of accounts templates
- `wizard.yaml` — setup wizard questions (in the country's language + English)
- `tax_rules/YYYY/` — tax rules per year

## 🌐 Translations

Translation files live in `openboek/i18n/`. To add or improve translations:

1. Edit the appropriate JSON file (`nl.json`, `en.json`)
2. Ensure every key exists in all language files
3. Use the Dutch legal term with English explanation for tax-specific terms:
   ```json
   "btw.rubriek.1a.name": "1a — Supplies/services high rate (hoog tarief)"
   ```

## 🔒 Security

If you discover a security vulnerability, **do not** open a public issue. See [SECURITY.md](SECURITY.md) for our responsible disclosure policy.

## 📜 License

By contributing, you agree that your contributions will be licensed under the [Apache License 2.0](LICENSE).

## 💬 Questions?

Open a [discussion](https://github.com/openboek/openboek/discussions) or an issue. We're happy to help you get started.
