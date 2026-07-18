# План рефакторинга схемы БД «Улица Радости» — Шаг 5

> **Назначение.** Actionable-чеклист изменений схемы: что добавить / удалить /
> перепроектировать. Формат пункта: **что → зачем → влияние на фичу** (Шаг 5 ТЗ).
> **Статус.** Выполнен — план реализован в коде (модели, миграции, констрейнты).
> Хранится как обоснование решений; актуальное состояние схемы — `schema.md`.
> **Связанные документы.** `schema-legacy.md` (что рефакторили), `schema.md` (что
> получилось), `schema-audit.md` (почему), `auth-flow.md`.

---

## A. Что ДОБАВИТЬ

### A1. Таблицы

| Таблица                  | Что                                                              | Зачем (→ влияние) |
| ------------------------ | --------------------------------------------------------------- | ----------------- |
| `subscription_slot`      | Связь M2M «абонемент ↔ выбранный слот» с полем `remaining` (default 4) | Прямое выражение правила «4 фишки на конкретный слот». Закрывает R1. → Фичи 3, 4 |
| `event_registration`     | `event_id, name, phone, email, attendees_count, amount, status` | В `schema.md` упоминается из `transaction.event_registration_id`, но самой таблицы нет. Без неё анонимная запись на ивент невозможна. → Фича 5 |
| `room` (кабинет)         | `id, name, is_active`                                           | Нужен как объект для `ExclusionConstraint` против двойного бронирования помещения. → Фича 7, маски |
| `service_inquiry` *(опц.)* | Заявки по «прочим услугам» (если решите собирать, а не только показывать) | По ТЗ услуги «звоните/пишите» — таблица нужна, только если будет форма. → Фича 1 |

`magic_tokens`, `callback_request`, `feedback`, `gallery_photo`, `event` — в `schema.md`
уже есть, добавлять не нужно. Проверить только индексы (см. A3).

### A2. Поля

| Таблица.поле                        | Тип                       | Зачем (→ влияние) |
| ----------------------------------- | ------------------------- | ----------------- |
| `magic_tokens.code` + `attempts_count` | `varchar(6)` + `int` (взамен magic-link `token`) | Переход на OTP-код вместо magic-link. Лимит попыток ввода против перебора. → `auth-flow.md` |
| `transaction.parent_id`             | сделать `NOT NULL` (кроме `type=EVENT`) | Теневая регистрация удалена: пробное/абонемент всегда от авторизованного родителя. `null` только у анонимного ивента. → `auth-flow.md` |
| `transaction.external_payment_id`   | `varchar(255) UNIQUE`     | Идемпотентность вебхука ЮКассы. Закрывает R5. → Фича 13 |
| `transaction.metadata`              | `JSONB`                   | Сырой ответ/чек/способ оплаты от ЮКассы. → Фича 13 |
| `transaction.expires_at`            | `timestamp`               | TTL для перехода `pending → expired`. → R6 |
| `subscription.status`               | enum `pending_payment/active/depleted/expired/canceled` | Замена смешанного `ACTIVE/PENDING_REFUND/COMPLETED`. → R6, фича 9 |
| `enrollment.subscription_id`        | FK → `subscription` (nullable для пробных) | Привязка записи к источнику фишек. Закрывает R2. → Фича 3 |
| `enrollment.activity_id`            | FK → `activity` (денормализация) | Нужно для partial-unique пробного «1 на кружок». → Фича 4 |
| `enrollment.status`                 | enum `HELD/REGULAR/CANCELLED` (взамен/в дополнение к `is_active`) | Бронь места до оплаты. Закрывает R4. → Фича 13 |
| `attendance.enrollment_id`          | FK → `enrollment`         | Однозначный «адрес» списываемой фишки. Закрывает R3. → Фича 3 |
| `attendance.positive_comment / negative_comment` | `text`       | Есть в `schema.md`, но не связано с источником — оставить, см. A3. → ЛК |
| `parent.lead_source`                | `varchar(100)`            | «Откуда узнали» из формы checkout (есть в API, нет в схеме). → Фича 8 |
| `subscription_plan.is_unlimited`    | `boolean`                 | Тариф «безлимит» (15 000 ₽). → Фича 9 |
| `subscription_plan.sessions_count`  | сделать `nullable`        | `null` = безлимит. → Фича 9 |
| `activity.params`                   | `JSONB`                   | Гибкие атрибуты SERVICE-услуг без ALTER TABLE. → Фича 1 |
| `room_id` в `schedule` и `schedule_mask` | FK → `room` (nullable) | Кабинет слота и его переопределение при переносе. → R8, R9 |
| `schedule_mask.new_day_of_week`     | `smallint nullable`       | Перенос на другой день недели, не только время. → R9 |

### A3. Индексы и Constraints

| Объект                                                        | Зачем |
| ------------------------------------------------------------- | ----- |
| `UNIQUE(external_payment_id)` на `transaction`                | Идемпотентность вебхука (R5) |
| `UNIQUE(student, activity) WHERE type='TRIAL'` на `enrollment`| «1 пробное на ребёнка на кружок» (R7) |
| `ExclusionConstraint` по `(teacher, day_of_week, tsrange(start,end))` на `schedule` | Нет двойной занятости учителя (R8). Требует `CREATE EXTENSION btree_gist` |
| Аналогичный `ExclusionConstraint` по `room`                   | Нет двойного бронирования кабинета (R8) |
| `CHECK(remaining >= 0)` на `subscription_slot`                | Баланс по слоту не уходит в минус (R1) |
| `UNIQUE(subscription_id, slot_id)` на `subscription_slot`     | Один слот в абонементе один раз |
| `db_index` на `schedule_mask.date`, `attendance.date`         | Частый поиск по дате при раскрытии календаря |
| `db_index` на `transaction.status`, `subscription.status`     | Фоновые задачи по протуханию/исчерпанию фильтруют по статусу |

---

## B. Что УДАЛИТЬ / переосмыслить как избыточное

| Объект                                              | Действие | Зачем |
| --------------------------------------------------- | -------- | ----- |
| `subscription_balance` (баланс по `activity`)       | Заменить на `subscription_slot` (баланс по слоту) | Баланс по кружку не выражает «4 фишки на конкретный слот» и допускает «перетекание» фишек между группами разного времени. Это корень R1 |
| `subscription.status = PENDING_REFUND`              | Удалить из текущего enum | Возврат — это операция/состояние транзакции, а не абонемента. Не смешивать оси «оплата» и «расход» |
| Прямой счётчик «занято» как хранимое поле (если возникнет соблазн) | Не вводить | Вместимость — кросс-табличный агрегат, считается запросом (Шаг 4 аудита), не денормализуется |
| Дубль `trial_date` без `type`-валидации             | Оставить поле, но связать инвариантом в `clean()` | Поле осмысленно только при `type=TRIAL` |

Жёстких «мусорных» связей в `schema.md` немного — основная проблема не в лишнем,
а в **недостающем** (раздел A). Главное удаление здесь концептуальное: уйти от
баланса-по-кружку к балансу-по-слоту.

---

## C. Что ПЕРЕПРОЕКТИРОВАТЬ (типы и связи)

### C1. Баланс фишек: `activity` → `slot`
Перенести `remaining` с уровня кружка на уровень слота абонемента
(`subscription_balance` → `subscription_slot`). Цепочка списания становится
однозначной: `attendance → enrollment → subscription_slot`. **Корневое изменение, P0.**

### C2. Связка `enrollment ↔ subscription`
Добавить `enrollment.subscription_id` и `enrollment.status (HELD/REGULAR/CANCELLED)`.
Запись перестаёт быть «просто галочкой» и становится узлом, связывающим оплату,
место и журнал. Поле `is_active` поглощается статусом (или остаётся производным).

### C3. Транзакция как платёж, а не «заявка»
`transaction` дополнить `external_payment_id (unique)`, `metadata (jsonb)`,
`expires_at`, расширить enum статусов до `draft/pending/succeeded/canceled/expired`.
Это делает её полноценным узлом интеграции с ЮКассой (Шаг 1.1 аудита).

### C4. Статусная модель абонемента
`subscription.status` → `pending_payment/active/depleted/expired/canceled`
(см. автомат в Шаге 1.2). Развести «фишки кончились» (обратимо) и «месяц вышел» (нет).

### C5. Маски переноса
`schedule_mask` расширить до полного переопределения занятия: `new_day_of_week`,
`new_start_time`, `new_end_time`, `new_room_id`, `new_teacher_id (опц.)`. Сейчас
перенос «однорукий» — только время (R9).

### C6. Единый денежный тип
Зафиксировать **во всей схеме** деньги как `integer` в копейках (как уже заявлено
в `schema.md` для большинства полей). Не смешивать с рублями/дробными. Один тип —
ноль ошибок округления и конвертации.

---

## D. Порядок применения (итерациями)

Катить не всё сразу — по приоритету из карты рисков аудита.

**P0 — деньги и места не работают корректно без этого:**
1. `subscription_slot` (C1) + `CHECK(remaining>=0)` + `UNIQUE(subscription, slot)`.
2. `enrollment.subscription_id` + `enrollment.status (HELD/...)` (C2, R4).
3. `attendance.enrollment_id` (R3) — замкнуть цепочку списания.
4. `transaction.external_payment_id UNIQUE` + идемпотентный вебхук (C3, R5).

**P1 — целостность под нагрузкой:**
5. Статусные модели `subscription` / `transaction` (C3, C4) + `expires_at`.
6. partial-unique пробного + `enrollment.activity_id` (R7).
7. `room` + два `ExclusionConstraint` (R8).

**P2 — расширяемость и удобство:**
8. `event_registration` (если ещё не заведена под фичу 5).
9. JSONB-поля (`transaction.metadata`, `activity.params`), `parent.lead_source`.
10. Расширение `schedule_mask` (C5), `subscription_plan.is_unlimited` (фича 9).

Каждый шаг P0 — отдельная миграция с обратной совместимостью данных (бэкфилл
существующих абонементов в `subscription_slot` по их слотам перед включением
`NOT NULL`/constraints).
