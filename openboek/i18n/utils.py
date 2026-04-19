"""Internationalisation helpers — load JSON translation files and expose ``t()``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from openboek.config import settings

_LOCALES_DIR = Path(__file__).parent / "locales"
_translations: dict[str, dict[str, str]] = {}


def _load_locale(lang: str) -> dict[str, str]:
    """Load a single locale JSON file into a flat dict."""
    path = _LOCALES_DIR / f"{lang}.json"
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _ensure_loaded() -> None:
    """Lazy-load all locale files on first access."""
    if not _translations:
        for locale_file in _LOCALES_DIR.glob("*.json"):
            lang = locale_file.stem
            _translations[lang] = _load_locale(lang)


def t(key: str, lang: str | None = None, **kwargs: Any) -> str:
    """Translate *key* into *lang* (defaults to ``settings.app_lang``).

    Supports ``{placeholder}`` interpolation via *kwargs*.
    Returns the key itself if no translation is found.
    """
    _ensure_loaded()
    lang = lang or settings.app_lang
    text = _translations.get(lang, {}).get(key)
    if text is None:
        # Fallback to default language, then to key
        text = _translations.get(settings.app_lang, {}).get(key, key)
    if kwargs:
        try:
            text = text.format(**kwargs)
        except (KeyError, IndexError):
            pass
    return text


def jinja2_globals() -> dict[str, Any]:
    """Return a dict of globals to inject into Jinja2 templates."""
    return {"t": t, "_": t}
