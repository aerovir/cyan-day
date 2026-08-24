"""Точка входа для запуска бота в Docker/локально.

Просто переиспользует app.main.
"""

from app.main import main

if __name__ == "__main__":
    raise SystemExit(main())
