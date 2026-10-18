"""Punkt startowy: python main.py. Token wyłącznie w sekretach hostingu / .env."""
import json
import logging
import os
from pathlib import Path

from hub.client import HubClient


def load_settings():
    env_file = Path('.env')
    if env_file.exists():
        for line in env_file.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"\''))
    config_file = Path('config.json')
    cfg = json.loads(config_file.read_text(encoding='utf-8')) if config_file.exists() else {}
    cfg['guild_id'] = int(os.getenv('GUILD_ID') or cfg.get('guild_id', 0))
    cfg.setdefault('data_dir', 'data')
    cfg.setdefault('max_offers_per_seller', 10)
    cfg.setdefault('max_open_orders_per_buyer', 3)
    cfg.setdefault('proof_retention_days', 30)
    cfg.setdefault('reminder_hours', 48)
    token = os.getenv('DISCORD_TOKEN', '').strip()
    if not token or token.startswith('WKLEJ'):
        raise SystemExit('Brak DISCORD_TOKEN. Ustaw sekret w hostingu albo plik .env. Nie wysyłaj tokenu na czat.')
    if cfg['guild_id'] <= 0:
        raise SystemExit('Ustaw GUILD_ID: identyfikator Twojego serwera Discord.')
    for key in ('max_offers_per_seller', 'max_open_orders_per_buyer', 'proof_retention_days', 'reminder_hours'):
        cfg[key] = int(cfg[key])
        if cfg[key] < 1:
            raise SystemExit(f'{key} musi być dodatnią liczbą.')
    return cfg, token


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    config, secret = load_settings()
    client = HubClient(config)
    client.run(secret, log_handler=None)
