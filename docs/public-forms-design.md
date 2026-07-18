# Публичные формы: обратный звонок и обратная связь — проектное решение

> **Назначение.** Проект «под ключ» двух публичных форм (`callback_request`, `feedback`):
> модели → анти-спам → уведомления → CRM.
> **Статус.** Актуальный — авторитет по публичным формам, реализовано с четырьмя
> отклонениями: успешный ответ и honeypot-дроп отдают 202 (не 204); капча проверяется
> в сервисном слое, а не в serializer.validate(); поле assigned_to не заведено
> (статусы без исполнителя); троттлинг только по IP за прокси (3/мин), пер-контактный
> счётчик не понадобился.
> **Связанные документы.** Схема — `schema.md`; единый формат ошибок — `api-core-contracts.md` §0.3.

Обе формы публичные, без JWT/сессии. Поэтому к каждому решению прикладывается
ответ на вопрос «а что если сюда придёт бот и зальёт 10 000 заявок за минуту».

---

## Шаг 1. Модели (`models.py`)

### 1.1. Хранение телефона

**Решение: `django-phonenumber-field` + `PhoneNumberField`.** В БД это обычный
`varchar`, но значение нормализуется в формат **E.164** (`+79991234567`) на уровне
валидации, до записи. Почему это лучше «просто `CharField(20)`» из текущей схемы:

- Единый канонический формат → поиск/фильтрация в админке по точному совпадению
  работают (а не «иногда с +7, иногда с 8, иногда с пробелами»).
- Валидация международного номера из коробки (`region="RU"` по умолчанию).
- Готовая интеграция для будущей отправки SMS — номер уже в E.164.

```python
from phonenumber_field.modelfields import PhoneNumberField
phone = PhoneNumberField("Телефон", region="RU", db_index=True)
```

> Альтернатива «хранить как BigInteger» — отвергнута: теряется `+`, код страны,
> ведущие нули, и появляется соблазн арифметики над тем, что числом не является.

### 1.2. Хранение интервала звонка

Три фиксированных варианта (Утро/День/Вечер). Сравнение трёх способов:

| Способ                  | Вердикт | Почему |
| ----------------------- | ------- | ------ |
| **Django `TextChoices`** | Выбор | Варианты статичны и заданы бизнесом «навсегда». Хранится `varchar`-код, человекочитаемо в БД, миграции не нужны при чтении, валидация на уровне модели и формы бесплатно. |
| Postgres `ENUM`         | Отклонено | Меняется только через `ALTER TYPE` (миграция-боль), Django поддерживает криво. Оправдан, только когда тип шарится множеством таблиц на уровне СУБД — не наш случай. |
| Справочная таблица      | Отклонено (overkill) | Нужна, только если варианты заводит **админ** через UI или у интервала есть свои атрибуты. Здесь это лишний JOIN и лишняя сущность ради трёх вечных значений. |

```python
class CallTimeWindow(models.TextChoices):
    MORNING = "MORNING", "Утро (9:00–12:00)"
    AFTERNOON = "AFTERNOON", "День (12:00–17:00)"
    EVENING = "EVENING", "Вечер (17:00–21:00)"

time_window = models.CharField(max_length=20, choices=CallTimeWindow.choices)
```

Принцип: **`TextChoices`, пока варианты не редактирует заказчик из админки.**
Как только захочет — тогда справочная таблица, не раньше.

### 1.3. CRM-поля (общие для обеих форм)

Текущая схема даёт только `status varchar` и `created_at`. Для работы менеджера
этого мало. Добавить (вынести в абстрактную базовую модель, т.к. поля общие):

```python
class ProcessingStatus(models.TextChoices):
    NEW = "NEW", "Новая"
    IN_PROGRESS = "IN_PROGRESS", "В работе"
    DONE = "DONE", "Обработана"
    SPAM = "SPAM", "Спам"

class CrmRequestBase(models.Model):
    status = models.CharField(max_length=20, choices=ProcessingStatus.choices,
                              default=ProcessingStatus.NEW, db_index=True)
    assigned_to = models.ForeignKey(User, on_delete=models.SET_NULL,
                                    null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    class Meta:
        abstract = True
```

- `assigned_to → User` (`SET_NULL`) — кто из админов взял заявку. Не CASCADE: уволенный
  менеджер не должен утаскивать заявки в небытие.
- `db_index` на `status` и `created_at` — админка постоянно фильтрует «новые» и сортирует по дате.
- `updated_at` (`auto_now`) — был только `created_at`.

**Автомат обработки** (валидация переходов — в `model.clean()` или в admin-action):

```
NEW ──► IN_PROGRESS ──► DONE
 │           │
 └─────► SPAM ◄──┘        (SPAM достижим из любого нетерминального состояния)
```

`DONE` и `SPAM` — терминальные. Обратные переходы запрещать в `clean()`, чтобы
история не «отыгрывалась назад».

**История изменений.** Требование «фиксация истории» в админке решается
`django-simple-history` (`HistoricalRecords()` на модели) — даёт автоматическую
теневую таблицу с кто/когда/что менял, видимую прямо в админке. Свой велосипед на
сигналах не писать.

### 1.4. Итоговые модели

`CallbackRequest(CrmRequestBase)`: `phone`, `time_window`.
`FeedbackRequest(CrmRequestBase)`: `full_name`, `email (db_index)`, `message`.
Оба наследуют CRM-поля и `HistoricalRecords`.

---

## Шаг 2. Безопасность и анти-спам

Эшелонированная оборона: дешёвые фильтры впереди, дорогие — сзади. Бот должен
отвалиться до того, как мы потратим запрос к капче или запись в БД.

```
Запрос → [1. Honeypot] → [2. Throttle: IP] → [3. Throttle: phone/email]
       → [4. Captcha-токен] → [5. Валидация данных] → INSERT + async-уведомление
```

### 2.1. Throttling (DRF, `throttling.py`)

Два независимых измерения — нельзя ограничивать только по IP (боты ходят с пулов
адресов) и только по телефону (один IP может долбить разными номерами).

| Правило                    | Лимит (старт)      | Класс DRF |
| -------------------------- | ------------------ | --------- |
| По IP, на каждую форму     | 5 запросов / час   | `ScopedRateThrottle` (`scope = "callback"` / `"feedback"`) |
| По телефону/email          | 1 запрос / 10 мин  | кастомный `SimpleRateThrottle` с `cache_key` по нормализованному номеру |

```python
class PhoneRateThrottle(SimpleRateThrottle):
    scope = "phone"          # rate в settings: "1/10m" (условно)
    def get_cache_key(self, request, view):
        phone = request.data.get("phone")
        if not phone:
            return None       # нет телефона — это правило не применяем
        return self.cache_format % {"scope": self.scope, "ident": phone}
```

Хранилище счётчиков — **Redis** (в стеке уже есть), не LocMemCache: при нескольких
воркерах локальная память не общая, и лимит дырявится. Подключить `default` cache на Redis.

> Цифры лимитов — стартовые, подбираются по реальному трафику. Принцип: живому
> человеку 5 звонков/час хватит с запасом, боту — нет.

### 2.2. Капча (Cloudflare Turnstile / reCAPTCHA v3)

Невидимая капча: фронт получает токен виджета и кладёт его в тело запроса
(`cf-turnstile-response`). Бэкенд **обязан** верифицировать токен серверным
запросом к провайдеру — клиенту верить нельзя.

Место проверки — **сериализатор**, метод `validate()` (до записи в БД):

```python
def validate(self, attrs):
    token = self.initial_data.get("captcha_token")
    resp = requests.post(TURNSTILE_VERIFY_URL,
                         data={"secret": SECRET, "response": token,
                               "remoteip": self.context["request"].META.get("REMOTE_ADDR")},
                         timeout=5)
    if not resp.json().get("success"):
        raise ValidationError({"captcha": "Проверка не пройдена"})
    return attrs
```

Нюансы:
- `timeout` обязателен — иначе зависший провайдер вешает наш поток.
- secret-ключ — в env, не в коде (это и в `CLAUDE.md` как запрет хардкода секретов).
- `remoteip` повышает точность скоринга у v3/Turnstile.

### 2.3. Honeypot (поле-ловушка)

Самый дешёвый фильтр, **без внешних сервисов** — ставится первым. В форму добавляется
скрытое поле (например `website` или `nickname`), невидимое для человека через CSS.
Реальный пользователь его не заполнит; примитивный бот, парсящий все `<input>`, —
заполнит.

Логика в сериализаторе:

```python
def validate_website(self, value):       # website — honeypot
    if value:
        # тихо отбиваем: НЕ 400 (чтобы бот не понял), а делаем no-op и отдаём 204
        raise SilentDrop()
    return value
```

Ключевой приём: при срабатывании ловушки возвращать **успех (204)**, но ничего не
сохранять. Если вернуть ошибку — бот поймёт про ловушку и обойдёт её. Пусть думает,
что заявка ушла.

---

## Шаг 3. Асинхронные уведомления менеджерам

Требование: менеджер моментально получает заявку (Telegram/Email), но HTTP-поток
пользователя **не ждёт** отправки. Сетевой вызов к Telegram/SMTP из тела вьюхи —
антипаттерн: тормозит ответ и роняет запрос, если внешний сервис недоступен.

**Решение: вынести отправку в очередь задач.**

- **Taskiq + Redis** (в стеке Redis уже есть) — основной выбор. Вьюха делает
  `notify_managers_task.kiq(request_id)` и сразу отдаёт 202; воркер шлёт уведомление
  в фоне, с ретраями при сбое внешнего API. Брокер — Redis Streams с consumer
  group: задача подтверждается только после выполнения, сообщения убитых воркеров
  переподхватываются (at-least-once).
- Celery отклонён: Taskiq даёт тот же контракт (очередь, ретраи) с нативным
  asyncio и строгой типизацией, без второго фреймворка конфигурации.

Важно: ставить задачу в очередь **через `transaction.on_commit`**, чтобы воркер не
прочитал ещё не закоммиченную заявку (гонка между БД-транзакцией и брокером).

```python
transaction.on_commit(lambda: async_to_sync(notify_managers_task.kiq)(instance.pk))
```

---

## Шаг 4. CRM-процессинг в админке

- Список с `list_filter = ("status", "created_at")`, `search_fields` по телефону/email.
- **Admin actions**: «Взять в работу» (ставит `IN_PROGRESS` + `assigned_to = request.user`),
  «Пометить спамом», «Закрыть» — это и есть переходы автомата из 1.3, валидируемые
  централизованно, а не ручным редактированием поля `status`.
- История правок — вкладка от `django-simple-history` (см. 1.3).
- `readonly_fields` на `created_at`, `phone/email` (контактные данные заявки правит
  не менеджер).

---

## Сводка решений

| Вопрос                       | Решение |
| ---------------------------- | ------- |
| Телефон                      | `PhoneNumberField`, E.164, `db_index` |
| Интервал звонка              | `TextChoices` (не ENUM, не справочник) |
| CRM-состояния                | `TextChoices` автомат NEW→IN_PROGRESS→DONE/SPAM + `assigned_to` |
| История изменений            | `django-simple-history` |
| Анти-спам, порядок           | honeypot → throttle(IP) → throttle(phone/email) → captcha |
| Throttling                   | `ScopedRateThrottle` (IP) + кастомный (phone/email), счётчики в Redis |
| Капча                        | серверная верификация токена в `serializer.validate()` |
| Honeypot                     | скрытое поле, при срабатывании — тихий 204 без записи |
| Уведомления                  | Taskiq + Redis Streams, постановка через `transaction.on_commit` |
