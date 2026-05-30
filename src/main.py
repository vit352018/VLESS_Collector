"""
main.py — главный файл, который запускает всё по порядку.

Простыми словами, вот что происходит каждый час:

  Шаг 1. СБОР — идём на GitHub и в Telegram, собираем все VPN-ключи
          которые люди выкладывают бесплатно в открытый доступ.

  Шаг 2. ЧИСТКА — убираем дубли (одинаковые ключи встречаются
          сразу в нескольких источниках).

  Шаг 3. ТЕСТ — для каждого сервера делаем "звонок":
          пробуем подключиться и засекаем время ответа.
          Если сервер не отвечает — выбрасываем его.

  Шаг 4. ГЕОЛОКАЦИЯ — узнаём в какой стране каждый рабочий сервер.

  Шаг 5. ЗАПИСЬ — сохраняем рабочие серверы в удобные файлы
          (отдельно по протоколам, топ-50 быстрых и т.д.)

  Шаг 6. HTML — делаем красивую страничку со статистикой.

  Шаг 7. ЯНДЕКС ДИСК — если заданы логин/пароль, копируем
          все файлы на Яндекс Диск через WebDAV.
"""

import asyncio
import logging
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).parent))

from collector import collect_all
from tg_scraper import collect_from_telegram
from tester import batch_test
from geoip import geolocate_hosts
from writer import write_all_outputs
from html_gen import generate_html
from yandex_upload import upload_all

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("main")

# Максимальная задержка сервера в миллисекундах.
# Серверы медленнее 4 секунд — выбрасываем, они бесполезны.
MAX_LATENCY_MS = 4000


def извлечь_хост_порт_sni(cfg: str):
    """
    Из строки вида vless://uuid@1.2.3.4:443?sni=google.com#метка
    вытаскиваем адрес сервера, порт и SNI (имя домена для маскировки).

    Зачем нам это? Чтобы знать куда "стучаться" при тесте.
    """
    try:
        if cfg.lower().startswith("vmess://"):
            # VMess хранит настройки в base64-кодировке — декодируем
            import base64, json
            b64 = cfg[8:].split("#")[0].split("?")[0]
            b64 += "=" * (-len(b64) % 4)  # добиваем padding
            data = json.loads(base64.b64decode(b64).decode("utf-8", errors="ignore"))
            host = str(data.get("add", "")).strip()
            port = int(data.get("port", 0))
            sni  = data.get("sni") or data.get("host") or None
            return (host, port, sni) if host and port else None
        else:
            # VLESS, Trojan и остальные — адрес прямо в ссылке
            from urllib.parse import parse_qs
            parsed = urlparse(cfg)
            host = parsed.hostname or ""
            port = parsed.port or 0
            qs   = parse_qs(parsed.query)
            sni  = (qs.get("sni") or qs.get("peer") or [None])[0]
            return (host, port, sni) if host and port else None
    except Exception:
        return None


async def main():
    t_start = time.monotonic()
    log.info("=" * 60)
    log.info("🚀 Запуск VLESS Collector")
    log.info("=" * 60)

    # ── Шаг 1: Сбор ──────────────────────────────────────────────
    # Идём на GitHub и в Telegram, собираем все VPN-ключи
    log.info("📥 ШАГ 1: Собираем конфиги из интернета…")
    github_configs = await collect_all()
    tg_configs     = await collect_from_telegram()
    all_raw        = github_configs + tg_configs
    log.info("   Всего найдено: %d ключей", len(all_raw))

    # ── Шаг 2: Чистка ────────────────────────────────────────────
    # Убираем дубли — один и тот же ключ часто встречается
    # в разных источниках, нам не нужны копии
    log.info("🧹 ШАГ 2: Убираем дубли…")
    seen: set[str] = set()
    unique: list[str] = []
    for c in all_raw:
        key = c.split("#")[0].rstrip("?& ")
        if key not in seen:
            seen.add(key)
            unique.append(c)
    log.info("   После чистки: %d уникальных ключей", len(unique))

    if not unique:
        log.error("❌ Ни одного ключа не нашли — что-то сломалось с источниками")
        sys.exit(1)

    # ── Шаг 3: Тест ──────────────────────────────────────────────
    # Для каждого сервера пробуем подключиться.
    # Делаем это для 80 серверов ОДНОВРЕМЕННО — иначе займёт часы.
    log.info("🔍 ШАГ 3: Тестируем серверы (до %d параллельно)…", 80)

    # Сначала вытаскиваем адреса из ключей
    targets = []
    cfg_by_target: dict = {}
    for cfg in unique:
        t = извлечь_хост_порт_sni(cfg)
        if t:
            targets.append(t)
            cfg_by_target.setdefault((t[0], t[1]), []).append(cfg)
    log.info("   Серверов для проверки: %d", len(targets))

    # Запускаем тесты
    test_results = await batch_test(targets, max_workers=80)

    # Собираем только те серверы, которые ответили и ответили быстро
    working: list[tuple[str, int]] = []
    tls_map: dict[str, bool] = {}
    seen_final: set[str] = set()

    for r in sorted(test_results, key=lambda x: x.get("tcp_ms") or 9999):
        if not r["alive"]:
            continue
        latency = r.get("tcp_ms") or 9999
        if latency > MAX_LATENCY_MS:
            continue
        host = r["host"]
        port = r["port"]
        tls_map[host] = r.get("tls_ok", False)
        for cfg in cfg_by_target.get((host, port), []):
            key = cfg.split("#")[0].rstrip("?& ")
            if key not in seen_final:
                seen_final.add(key)
                working.append((cfg, latency))

    log.info("   ✅ Рабочих серверов: %d из %d", len(working), len(targets))

    if not working:
        log.warning("⚠️  Рабочих серверов нет — возможно, все источники временно недоступны")
        sys.exit(1)

    # ── Шаг 4: Геолокация ────────────────────────────────────────
    # Узнаём страну каждого сервера — чтобы в файле было
    # красиво: "🇩🇪 Germany | 142ms" вместо "1.2.3.4 | 142ms"
    log.info("🌍 ШАГ 4: Определяем страны серверов…")
    hosts = list({
        urlparse(cfg).hostname or ""
        for cfg, _ in working
        if urlparse(cfg).hostname
    })
    geo_map = await geolocate_hosts(hosts)

    # ── Шаг 5 & 6: Запись файлов и HTML ─────────────────────────
    log.info("💾 ШАГ 5: Записываем файлы…")
    stats = write_all_outputs(working, geo_map=geo_map, tls_map=tls_map)

    log.info("🌐 ШАГ 6: Генерируем страницу статистики…")
    generate_html(stats)

    # ── Шаг 7: Яндекс Диск ───────────────────────────────────────
    # Загружаем на Яндекс Диск только если заданы логин и пароль.
    # В GitHub Actions они берутся из Secrets (зашифрованных переменных).
    # Локально — из переменных окружения.
    yandex_login = os.environ.get("YANDEX_LOGIN", "").strip()
    yandex_pass  = os.environ.get("YANDEX_PASS",  "").strip()

    if yandex_login and yandex_pass:
        log.info("☁️  ШАГ 7: Загружаем на Яндекс Диск…")
        yd_result = await upload_all(yandex_login, yandex_pass)
        log.info(
            "   Загружено: %d  Ошибок: %d  Пропущено: %d",
            yd_result["uploaded"], yd_result["failed"], yd_result["skipped"],
        )
    else:
        log.info("☁️  ШАГ 7: Яндекс Диск пропущен — YANDEX_LOGIN/YANDEX_PASS не заданы")
        log.info("   Чтобы включить: добавь секреты в Settings → Secrets → Actions")

    # ── Итог ─────────────────────────────────────────────────────
    elapsed = int(time.monotonic() - t_start)
    log.info("=" * 60)
    log.info(
        "🏁 Готово! Время: %d сек. | Рабочих: %d | TLS-ок: %d",
        elapsed, stats["total_working"], stats["tls_confirmed"],
    )
    log.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
