# «Улица Радости» — Проектная схема БД (до реализации)

> **Назначение.** Исходный набросок структуры таблиц, с которого начиналось
> проектирование. Именно эту версию разбирает `schema-audit.md`.
> **Статус.** Архив. Реализованная схема — `schema.md`; при чтении аудита и плана
> рефакторинга сверяйся с этим файлом, при работе с кодом — с актуальным.
> **Связанные документы.** `schema.md` (актуальная), `schema-audit.md`, `schema-refactoring.md`.

---

## Пользователи

### teacher_profile — Профиль преподавателя

| Поле                | Тип       | Описание                                 |
| ----------------------- | ------------ | ------------------------------------------------ |
| id                      | integer PK   | —                                               |
| user_id                 | integer      | Ссылка на auth_user (1:1)                |
| middle_name             | varchar(150) | Отчество                                 |
| photo                   | varchar(500) | S3 URL                                           |
| bio                     | text         | —                                               |
| display_order           | integer      | Порядок сортировки (default: 0) |
| created_at / updated_at | timestamp    | —                                               |

### parent — Родитель

| Поле                | Тип       | Описание                        |
| ----------------------- | ------------ | --------------------------------------- |
| id                      | integer PK   | —                                      |
| full_name               | varchar(255) | ФИО                                  |
| phone                   | varchar(20)  | Уникальный                    |
| email                   | varchar(254) | Уникальный                    |
| comments                | text         | Произвольные заметки |
| created_at / updated_at | timestamp    | —                                      |

### student — Ребёнок

| Поле                | Тип       | Описание                        |
| ----------------------- | ------------ | --------------------------------------- |
| id                      | integer PK   | —                                      |
| parent_id               | integer FK   | → parent                               |
| full_name               | varchar(255) | ФИО                                  |
| school_grade            | varchar(20)  | Класс                              |
| dob                     | date         | Дата рождения               |
| health_issues           | text         | Особенности здоровья |
| created_at / updated_at | timestamp    | —                                      |

---

## Ядро (расписание)

### activity — Кружок / услуга

| Поле                | Тип       | Описание                                                                      |
| ----------------------- | ------------ | ------------------------------------------------------------------------------------- |
| id                      | integer PK   | —                                                                                    |
| name                    | varchar(255) | Название                                                                      |
| slug                    | varchar(255) | URL-идентификатор, уникальный                                  |
| category                | varchar(20)  | `CLUB` или `SERVICE`                                                           |
| description             | text         | —                                                                                    |
| image                   | varchar(500) | S3 URL                                                                                |
| price                   | integer      | Базовая цена разового занятия,**в копейках** |
| is_active               | boolean      | —                                                                                    |
| is_featured             | boolean      | Выводить на главной (default: false)                                 |
| created_at / updated_at | timestamp    | —                                                                                    |

### time_slot — Временной слот

| Поле    | Тип     | Описание   |
| ----------- | ---------- | ------------------ |
| id          | integer PK | —                 |
| day_of_week | smallint   | 0 = Пн, 6 = Вс |
| start_time  | time       | —                 |
| end_time    | time       | —                 |

### schedule — Группа (кружок + слот + преподаватель)

| Поле                | Тип       | Описание                            |
| ----------------------- | ------------ | ------------------------------------------- |
| id                      | integer PK   | —                                          |
| activity_id             | integer FK   | → activity                                 |
| time_slot_id            | integer FK   | → time_slot                                |
| teacher_id              | integer FK   | → teacher_profile (опционально) |
| group_name              | varchar(100) | Название группы               |
| max_capacity            | integer      | Максимум детей                 |
| is_active               | boolean      | —                                          |
| created_at / updated_at | timestamp    | —                                          |

### schedule_mask — Исключения в расписании

Переопределяет конкретную дату без изменения самого расписания.

| Поле       | Тип      | Описание                                                   |
| -------------- | ----------- | ------------------------------------------------------------------ |
| id             | integer PK  | —                                                                 |
| schedule_id    | integer FK  | → schedule                                                        |
| date           | date        | Дата, которую переопределяем              |
| type           | varchar(20) | `CANCELLATION` — отмена, `RESCHEDULE` — перенос |
| new_start_time | time        | Новое время, только для `RESCHEDULE`          |

---

## Биллинг

### subscription_plan — Тарифный план

| Поле       | Тип       | Описание                                           |
| -------------- | ------------ | ---------------------------------------------------------- |
| id             | integer PK   | —                                                         |
| name           | varchar(255) | Название (напр. «12 занятий»)         |
| sessions_count | integer      | Кол-во занятий; null если безлимит |
| price          | integer      | **В копейках**                              |
| is_unlimited   | boolean      | Безлимитный тариф                          |

### subscription — Купленный абонемент

| Поле                | Тип      | Описание                                |
| ----------------------- | ----------- | ----------------------------------------------- |
| id                      | integer PK  | —                                              |
| student_id              | integer FK  | → student                                      |
| plan_id                 | integer FK  | → subscription_plan                            |
| start_date              | date        | —                                              |
| expired_at              | date        | —                                              |
| status                  | varchar(20) | `ACTIVE` / `PENDING_REFUND` / `COMPLETED` |
| created_at / updated_at | timestamp   | —                                              |

### subscription_balance — Остаток занятий

Баланс считается **по кружку**, а не по конкретной группе — ребёнок может ходить в разные группы одного кружка.

| Поле        | Тип     | Описание                     |
| --------------- | ---------- | ------------------------------------ |
| id              | integer PK | —                                   |
| subscription_id | integer FK | → subscription                      |
| activity_id     | integer FK | → activity                          |
| remaining       | integer    | Остаток занятий (≥ 0) |

Уникальный индекс: `(subscription_id, activity_id)`

---

## Транзакции

### transaction — Платёж

| Поле                | Тип         | Описание                                                      |
| ----------------------- | -------------- | --------------------------------------------------------------------- |
| id                      | varchar(36) PK | UUID, генерируется на бэке                          |
| parent_id               | integer FK     | → parent; null для анонимных ивентов              |
| amount                  | integer        | **В копейках**                                         |
| external_payment_id     | varchar(255)   | ID платежа от ЮКассы |
| status                  | varchar(20)    | `PENDING` / `SUCCEEDED` / `CANCELLED`                           |
| type                    | varchar(20)    | `SUBSCRIPTION` / `TRIAL` / `EVENT`                              |
| subscription_id         | integer FK     | → subscription; заполнен если type=SUBSCRIPTION          |
| event_registration_id   | integer FK     | → event_registration; заполнен если type=EVENT           |
| created_at / updated_at | timestamp      | —                                                                    |

---

## Логистика

### enrollment — Запись в группу

| Поле       | Тип         | Описание                                                                                                                                                         |
| -------------- | -------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| id             | integer PK     | —                                                                                                                                                                       |
| transaction_id | varchar(36) FK | → transaction; null если перевод сделан вручную администратором (метод протектид  в классе админа))) |
| student_id     | integer FK     | → student (not null)                                                                                                                                                  |
| schedule_id    | integer FK     | → schedule (not null)                                                                                                                                                    |
| type           | varchar(20)    | `REGULAR` — постоянная запись, `TRIAL` — пробное                                                                                     |
| trial_date     | date           | Дата пробного; обязательно если type=TRIAL                                                                                              |
| is_active      | boolean        | Активна ли запись                                                                                                                             |
| created_at     | timestamp      | —                                                                                                                                                                       |

Уникальный индекс: `(student_id, schedule_id)` — нельзя записаться в одну группу дважды.

### attendance — Посещаемость

| Поле         | Тип      | Описание                                                       |
| ---------------- | ----------- | ---------------------------------------------------------------------- |
| id               | integer PK  | —                                                                     |
| student_id       | integer FK  | → student                                                             |
| schedule_id      | integer FK  | → schedule                                                            |
| date             | date        | Дата занятия                                                |
| status           | varchar(10) | `PRESENT` / `ABSENT` / `EXCUSED`                                 |
| positive_comment | text        | Позитивная обратная связь от педагога |
| negative_comment | text        | Негативная обратная связь от педагога |
| created_at       | timestamp   | —                                                                     |

---

## Мероприятия

### event — Мероприятие

| Поле                | Тип       | Описание                                             |
| ----------------------- | ------------ | ------------------------------------------------------------ |
| id                      | integer PK   | —                                                           |
| title                   | varchar(255) | —                                                           |
| slug                    | varchar(255) | Уникальный                                         |
| description             | text         | —                                                           |
| date_time               | timestamp    | Дата и время начала                          |
| price                   | integer      | **В копейках**; 0 если бесплатно |
| status                  | varchar(10)  | `PLANNED` / `ARCHIVED`                            |
| image                   | varchar(500) | S3 URL                                                       |
| created_at / updated_at | timestamp    | —                                                           |

### event_registration — Регистрация на мероприятие

| Поле                | Тип       | Описание                                                               |
| ----------------------- | ------------ | ------------------------------------------------------------------------------ |
| id                      | integer PK   | —                                                                             |
| event_id                | integer FK   | → event                                                                       |
| name                    | varchar(255) | Имя участника                                                      |
| phone                   | varchar(20)  | —                                                                             |
| email                   | varchar(254) | —                                                                             |
| attendees_count         | integer      | Кол-во мест (default: 1)                                              |
| amount                  | integer      | **В копейках**; бэк считает: price × attendees_count |
| status                  | varchar(20)  | `PENDING` / `CONFIRMED` / `CANCELLED`                             |
| created_at / updated_at | timestamp    | —                                                                             |

---

## Контент и CRM

### gallery_photo — Фотография

| Поле   | Тип       | Описание                                  |
| ---------- | ------------ | ------------------------------------------------- |
| id         | integer PK   | —                                                |
| image      | varchar(500) | S3 URL                                            |
| alt        | varchar(255) | Alt-текст (может быть пустым) |
| created_at | timestamp    | —                                                |

### callback_request — Заявка на обратный звонок

| Поле    | Тип      | Описание                                    |
| ----------- | ----------- | --------------------------------------------------- |
| id          | integer PK  | —                                                  |
| phone       | varchar(20) | —                                                  |
| time_window | varchar(20) | Удобное время, напр.`09:00-12:00` |
| status      | varchar(20) | `NEW` / `IN_PROGRESS` / `DONE`         |
| created_at  | timestamp   | —                                                  |

### feedback — Обращение с сайта

| Поле   | Тип       | Описание                            |
| ---------- | ------------ | ------------------------------------------- |
| id         | integer PK   | —                        |
| name       | varchar(255) | Опционально |
|            |              |                                             |
| email      | varchar(254) | обяз.                                   |
| message    | text         | Текст обращения               |
| status     | varchar(20)  | `NEW` / `REVIEWED`               |
| created_at | timestamp    | —                |

### magic_tokens — Токены для входа без пароля

| Поле   | Тип       | Описание                                 |
| ---------- | ------------ | ------------------------------------------------ |
| id         | integer PK   | —                                               |
| email      | varchar(250) | Почта, которой выдан токен |
| token      | varchar(64)  | Одноразовый токен                |
| expires_at | timestamp    | Срок действия                        |
| is_used    | boolean      | Использован ли                      |
| created_at | timestamp    | —                                               |
