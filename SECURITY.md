# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 0.x     | ✅ Current development |

## Reporting a Vulnerability

If you discover a security vulnerability in OpenBoek, please report it responsibly.

**Do NOT open a public GitHub issue for security vulnerabilities.**

### How to Report

Email: **security@openboek.io**

If that address is not yet active, email: **ignaciomichelena@gmail.com** with subject line `[SECURITY] OpenBoek: <brief description>`

### What to Include

- Description of the vulnerability
- Steps to reproduce
- Potential impact
- Suggested fix (if you have one)

### What to Expect

- Acknowledgment within 48 hours
- Status update within 7 days
- We will work with you on a fix before public disclosure
- Credit in the release notes (unless you prefer anonymity)

## Security Design

OpenBoek is designed with privacy and security as core principles:

- **Self-hosted only** — your financial data never leaves your machine
- **Local AI** — all AI processing via Ollama, no cloud APIs
- **No telemetry** — zero phone-home, zero analytics
- **Password hashing** — bcrypt with appropriate work factor
- **Session-based auth** — secure cookies with configurable secret key
- **Audit logging** — append-only trail of all state changes
- **SQL injection prevention** — SQLAlchemy ORM with parameterized queries

## Best Practices for Deployers

- **Never expose OpenBoek to the public internet** — use Tailscale, WireGuard, or VPN
- **Change the default `SECRET_KEY`** in production
- **Use strong database passwords**
- **Keep your Ollama instance local** — don't expose it publicly
- **Regular backups** — `pg_dump` your database regularly
