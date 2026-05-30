"""
Загрузка файлов на Яндекс.Диск через официальный REST API.
Не нужно устанавливать отдельные библиотеки — используем aiohttp.

Как получить токен (один раз, 5 минут):
  1. Зайди на https://oauth.yandex.ru/
  2. Нажми "Зарегистрировать новое приложение"
  3. Название: любое (например "vless-collector")
  4. Платформы: поставь галочку "Веб-сервисы"
  5. Callback URI: https://oauth.yandex.ru/verification_code
  6. Доступы: раздел "Яндекс Диск" → поставь ВСЕ галочки → Создать
  7. Скопируй "ID приложения" (это client_id)
  8. Открой в браузере:
     https://oauth.yandex.ru/authorize?response_type=token&client_id=ВАШ_CLIENT_ID
  9. Разреши доступ → в адресной строке появится токен после access_token=
  10. Скопируй токен (длинная строка) → добавь в GitHub Secrets как YADISK_TOKEN
"""

import asyncio
import logging
import sys
from pathlib import Path

import aiohttp

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import YADISK_TOKEN, YADISK_FOLDER

log = logging.getLogger("yadisk")

API = "https://cloud-api.yandex.net/v1/disk/resources"


async def _api(session: aiohttp.ClientSession, method: str, url: str, **kwargs) -> dict:
    """Вспомогательный вызов API."""
    headers = {"Authorization": f"OAuth {YADISK_TOKEN}"}
    async with session.request(method, url, headers=headers, **kwargs) as r:
        try:
            return await r.json()
        except Exception:
            return {"status": r.status}


async def ensure_folder(session: aiohttp.ClientSession, folder: str):
    """Создаёт папку на Яндекс.Диске если её нет."""
    await _api(session, "PUT", API, params={"path": f"disk:/{folder}"})


async def upload_file(session: aiohttp.ClientSession, local_path: Path, remote_name: str):
    """
    Загружает один файл на Яндекс.Диск.
    local_path  — путь к файлу на компьютере/сервере
    remote_name — имя файла на Яндекс.Диске
    """
    remote_path = f"disk:/{YADISK_FOLDER}/{remote_name}"

    # Шаг 1: получаем одноразовую ссылку для загрузки
    resp = await _api(
        session, "GET", f"{API}/upload",
        params={"path": remote_path, "overwrite": "true"},
    )
    upload_url = resp.get("href")
    if not upload_url:
        log.warning("  ✗ %s — не удалось получить ссылку: %s", remote_name, resp)
        return False

    # Шаг 2: загружаем файл по этой ссылке (обычный PUT)
    data = local_path.read_bytes()
    async with session.put(upload_url, data=data) as r:
        if r.status in (200, 201):
            log.info("  ✓ загружен: %s  (%d КБ)", remote_name, len(data) // 1024)
            return True
        else:
            log.warning("  ✗ %s — ошибка загрузки: HTTP %s", remote_name, r.status)
            return False


async def upload_all(output_dir: Path):
    """
    Загружает все выходные файлы из output/ на Яндекс.Диск.
    Пропускает служебные файлы (начинающиеся с _).
    """
    if not YADISK_TOKEN:
        log.info("⏭  YADISK_TOKEN не задан — пропускаем загрузку на Яндекс.Диск")
        return

    # Файлы которые загружаем
    files_to_upload = [
        f for f in sorted(output_dir.iterdir())
        if f.is_file() and not f.name.startswith("_")
    ]

    if not files_to_upload:
        log.warning("Нет файлов для загрузки в %s", output_dir)
        return

    log.info("☁️  Загружаю %d файлов на Яндекс.Диск → /%s/", len(files_to_upload), YADISK_FOLDER)

    connector = aiohttp.TCPConnector(ssl=True)
    async with aiohttp.ClientSession(connector=connector) as session:
        # Создаём папку (если уже есть — ничего не сломается)
        await ensure_folder(session, YADISK_FOLDER)

        # Загружаем файлы по одному (API Яндекса не любит параллельность)
        ok = 0
        for f in files_to_upload:
            success = await upload_file(session, f, f.name)
            if success:
                ok += 1
            await asyncio.sleep(0.3)  # небольшая пауза чтобы не получить rate-limit

    log.info("☁️  Яндекс.Диск: загружено %d / %d файлов", ok, len(files_to_upload))


if __name__ == "__main__":
    # Быстрая проверка токена
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    output = Path(__file__).parent.parent / "output"
    asyncio.run(upload_all(output))
