set -euo pipefail

uv init --no-readme --python 3.12
rm -f hello.py

uv add \
  django \
  djangorestframework \
  djangorestframework-simplejwt \
  "psycopg[binary]" \
  pydantic-settings \
  taskiq \
  taskiq-redis \
  drf-spectacular \
  django-storages \
  boto3 \
  "django-phonenumber-field[phonenumbers]" \
  django-simple-history \
  django-cors-headers

uv add --dev \
  ruff \
  mypy \
  pytest \
  pytest-django \
  pytest-mock \
  factory_boy \
  pre-commit \
  django-stubs \
  djangorestframework-stubs

uv run django-admin startproject config .

mkdir -p apps
for app in core users schedule billing public_forms; do
  uv run python manage.py startapp "$app" "apps/$app"
  # Корректируем app label для модульного монолита
  if [[ "$OSTYPE" == "darwin"* ]]; then
    sed -i "" "s/name = '$app'/name = 'apps.$app'/" "apps/$app/apps.py"
  else
    sed -i "s/name = '$app'/name = 'apps.$app'/" "apps/$app/apps.py"
  fi
done

# Создание заглушки под обработчик ошибок (чтобы settings.py не падал)
mkdir -p apps/core
touch apps/core/__init__.py
cat << 'EOF' > apps/core/exceptions.py
from rest_framework.views import exception_handler
from rest_framework.response import Response

def problem_detail_exception_handler(exc: Exception, context: dict) -> Response | None:
    # Заглушка, которую мы расширим по RFC 9457 на этапе интеграции
    return exception_handler(exc, context)
EOF

echo "Базовый каркас успешно развернут!"