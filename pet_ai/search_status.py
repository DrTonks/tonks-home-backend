"""Local operator command: python -m pet_ai.search_status (no provider calls)."""
import json
import os

from sleepy_app.common.environment import load_env_file
from .config import PetAIConfig
from .search_store import SearchStore


def main():
    load_env_file(os.environ.get('SLEEPY_ENV_FILE') or None)
    config = PetAIConfig.from_env()
    days = SearchStore(config.search_db_path).stats()
    print(json.dumps({
        'provider': 'deepseek', 'enabled': config.search_enabled,
        'key_configured': bool(config.effective_search_api_key),
        'daily_attempt_limit': config.daily_search_limit, 'day_timezone': 'UTC',
        'days': days,
        'note': 'attempts includes failures and crash reservations; successes is not an upstream billing statement',
    }, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
