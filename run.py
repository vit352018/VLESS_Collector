"""
run.py — удобный скрипт для запуска с компьютера (не через GitHub).

Использование:
  python run.py                 — полный пайплайн (сбор + тест + сохранение)
  python run.py --sources       — только скачать ключи (без теста)
  python run.py --test          — только протестировать скачанные ключи
  python run.py --upload        — только залить файлы на Яндекс Диск
  python run.py --stats         — показать текущую статистику

Чтобы работал Яндекс Диск, создай рядом файл .env:
  YANDEX_LOGIN=vasya@yandex.ru
  YANDEX_PASS=пароль_приложения

Или задай переменные окружения вручную перед запуском:
  Windows:  set YANDEX_LOGIN=vasya@yandex.ru && set YANDEX_PASS=пароль
  Linux/Mac: export YANDEX_LOGIN=vasya@yandex.ru && export YANDEX_PASS=пароль
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

# Пробуем загрузить .env файл если он есть рядом
def load_dotenv():
    env_file = Path(__file__).parent / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                os.environ.setdefault(key.strip(), val.strip())
        print("✅ Загружен файл .env")

load_dotenv()

sys.path.insert(0, str(Path(__file__).parent / "src"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("run")


def показать_статистику():
    """Читает stats.json и красиво выводит в консоль."""
    stats_file = Path("output/stats.json")
    if not stats_file.exists():
        print("❌ Файл stats.json не найден.")
        print("   Сначала запусти полный пайплайн: python run.py")
        return

    s = json.loads(stats_file.read_text(encoding="utf-8"))
    lat = s.get("latency", {})
    print(f"""
╔══════════════════════════════════════════╗
║         VLESS Collector — Статистика     ║
╠══════════════════════════════════════════╣
║  Обновлено:       {s.get("updated_msk","")[:16]:<24}║
║  Рабочих серверов: {s.get("total_working",0):<23}║
║  TLS подтверждено: {s.get("tls_confirmed",0):<23}║
╠══════════════════════════════════════════╣
║  По протоколам:                          ║""")
    for proto, cnt in s.get("by_protocol", {}).items():
        if cnt > 0:
            print(f"║    {proto:<12} {cnt:<28}║")
    print(f"""╠══════════════════════════════════════╣
║  Задержка (мс):                          ║
║    MIN={lat.get("min_ms",0):<6}  AVG={lat.get("avg_ms",0):<6}  P90={lat.get("p90_ms",0):<9}║
╠══════════════════════════════════════════╣
║  Топ-5 стран:                            ║""")
    for country, cnt in list(s.get("top_countries", {}).items())[:5]:
        print(f"║    {country:<18} {cnt:<22}║")
    print("╚══════════════════════════════════════════╝")


async def только_источники():
    """Шаг 1: скачать ключи из интернета, сохранить сырой список."""
    from collector import collect_all
    from tg_scraper import collect_from_telegram

    configs = await collect_all()
    tg      = await collect_from_telegram()
    all_c   = configs + tg

    # Дедупликация
    seen, unique = set(), []
    for c in all_c:
        k = c.split("#")[0].rstrip("?& ")
        if k not in seen:
            seen.add(k)
            unique.append(c)

    Path("output").mkdir(exist_ok=True)
    Path("output/_raw_configs.txt").write_text("\n".join(unique), encoding="utf-8")
    log.info("✅ Сохранено %d ключей в output/_raw_configs.txt", len(unique))


async def только_тест():
    """Шаг 3-6: тестирование, геолокация, запись файлов (нужен _raw_configs.txt)."""
    raw_file = Path("output/_raw_configs.txt")
    if not raw_file.exists():
        log.error("Нет output/_raw_configs.txt — сначала запусти: python run.py --sources")
        return

    from urllib.parse import urlparse, parse_qs
    from tester import batch_test
    from geoip import geolocate_hosts
    from writer import write_all_outputs
    from html_gen import generate_html
    import base64, json as _json

    configs = [l for l in raw_file.read_text(encoding="utf-8").splitlines() if l.strip()]
    log.info("Загружено %d конфигов для теста", len(configs))

    # Вытаскиваем адреса
    targets, cfg_by_target = [], {}
    for cfg in configs:
        try:
            if cfg.lower().startswith("vmess://"):
                b64 = cfg[8:].split("#")[0].split("?")[0]
                b64 += "=" * (-len(b64) % 4)
                data = _json.loads(base64.b64decode(b64).decode("utf-8", errors="ignore"))
                host, port = str(data.get("add", "")), int(data.get("port", 0))
                sni = data.get("sni") or None
            else:
                p = urlparse(cfg)
                host, port = p.hostname or "", p.port or 0
                sni = (parse_qs(p.query).get("sni") or [None])[0]
            if host and port:
                targets.append((host, port, sni))
                cfg_by_target.setdefault((host, port), []).append(cfg)
        except Exception:
            pass

    test_results = await batch_test(targets, max_workers=80)

    working, tls_map, seen_f = [], {}, set()
    for r in sorted(test_results, key=lambda x: x.get("tcp_ms") or 9999):
        if not r["alive"] or (r.get("tcp_ms") or 9999) > 4000:
            continue
        host = r["host"]
        tls_map[host] = r.get("tls_ok", False)
        for cfg in cfg_by_target.get((host, r["port"]), []):
            k = cfg.split("#")[0].rstrip("?& ")
            if k not in seen_f:
                seen_f.add(k)
                working.append((cfg, r["tcp_ms"]))

    log.info("✅ Рабочих: %d", len(working))
    hosts = list({urlparse(c).hostname or "" for c, _ in working if urlparse(c).hostname})
    geo_map = await geolocate_hosts(hosts)
    stats = write_all_outputs(working, geo_map=geo_map, tls_map=tls_map)
    generate_html(stats)
    log.info("Готово.")
    return stats


async def только_яндекс():
    """Только загрузить уже готовые файлы из output/ на Яндекс Диск."""
    from yandex_upload import upload_all
    login = os.environ.get("YANDEX_LOGIN", "").strip()
    pwd   = os.environ.get("YANDEX_PASS",  "").strip()

    if not login or not pwd:
        print("""
❌ Не заданы YANDEX_LOGIN и YANDEX_PASS.

Создай файл .env рядом с run.py:
  YANDEX_LOGIN=vasya@yandex.ru
  YANDEX_PASS=пароль_приложения_из_настроек_безопасности

Пароль приложения получи тут (не основной пароль!):
  https://id.yandex.ru/security/app-passwords
""")
        return
    await upload_all(login, pwd)


async def полный_пайплайн():
    """Запустить всё с нуля."""
    from main import main
    await main()


def main_cli():
    parser = argparse.ArgumentParser(
        description="VLESS Collector — управление",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Примеры:
  python run.py                 полный запуск (рекомендуется)
  python run.py --sources       только скачать ключи
  python run.py --test          только протестировать
  python run.py --upload        только залить на Яндекс Диск
  python run.py --stats         показать статистику
        """
    )
    parser.add_argument("--sources", action="store_true", help="Только скачать ключи из интернета")
    parser.add_argument("--test",    action="store_true", help="Только тестировать (нужен --sources)")
    parser.add_argument("--upload",  action="store_true", help="Только загрузить на Яндекс Диск")
    parser.add_argument("--stats",   action="store_true", help="Показать статистику")
    args = parser.parse_args()

    if args.stats:
        показать_статистику()
    elif args.sources:
        asyncio.run(только_источники())
    elif args.test:
        asyncio.run(только_тест())
    elif args.upload:
        asyncio.run(только_яндекс())
    else:
        asyncio.run(полный_пайплайн())


if __name__ == "__main__":
    main_cli()
