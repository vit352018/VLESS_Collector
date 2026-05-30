"""
Конфигурация проекта.
GITHUB_USERNAME и GITHUB_REPO читаются автоматически
из переменных окружения GitHub Actions — ничего менять не нужно.
Если запускаешь локально — они читаются из файла .env (если есть).
"""

import os
from pathlib import Path

# Пытаемся загрузить .env для локального запуска
_env = Path(__file__).parent / ".env"
if _env.exists():
    for line in _env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

# GitHub — подставляются автоматически в Actions через $GITHUB_REPOSITORY
# Формат переменной: "username/repo-name"
_repo_full = os.environ.get("GITHUB_REPOSITORY", "YOUR_USERNAME/vless-collector")
GITHUB_USERNAME, _, GITHUB_REPO = _repo_full.partition("/")

# ── Яндекс.Диск ────────────────────────────────────────────────────────────────
# OAuth-токен Яндекс.Диска. Получить: https://yandex.ru/dev/disk/poligon/
# Добавь в GitHub: Settings → Secrets → Actions → New → имя: YADISK_TOKEN
YADISK_TOKEN: str = os.environ.get("YADISK_TOKEN", "")

# Папка на Яндекс.Диске куда загружать файлы (будет создана автоматически)
YADISK_FOLDER: str = os.environ.get("YADISK_FOLDER", "vless-collector")

# ── Параметры тестирования ──────────────────────────────────────────────────────
TCP_TIMEOUT  = float(os.environ.get("TCP_TIMEOUT",  "5.0"))
MAX_WORKERS  = int(os.environ.get("MAX_WORKERS",    "80"))
MAX_LATENCY  = int(os.environ.get("MAX_LATENCY",    "4000"))
FETCH_TIMEOUT = int(os.environ.get("FETCH_TIMEOUT", "20"))
