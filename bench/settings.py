from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
SECRET_KEY = "bench-only-not-secret"
DEBUG = False
ALLOWED_HOSTS = []
USE_TZ = True
TIME_ZONE = "Europe/Moscow"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

INSTALLED_APPS = ["django.contrib.postgres", "common", "model_x", "model_y", "model_y2", "model_y3"]

# Postgres работает в docker-контейнере pricelist-pg; сокет проброшен в .pgsock,
# поэтому подключаемся без TCP (из сетевого namespace хост-порты недоступны).
PG_SOCKET = str(BASE_DIR / ".pgsock")

# Один датасет = одна база: размеры таблиц и индексов для 1/5/20 типов цен
# считаются независимо. Обе модели (x_*, y_*) лежат в каждой базе рядом.
# Суффикс s — профиль истории single (массовые изменения только по одному типу).
DATASETS = ["t1", "t5", "t20", "t1s", "t5s", "t20s", "x10"]


def _db(name):
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": f"bench_{name}",
        "USER": "bench",
        "PASSWORD": "bench",
        "HOST": PG_SOCKET,
    }


DATABASES = {"default": _db("t5"), **{d: _db(d) for d in DATASETS}}
