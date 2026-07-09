"""Триггеры БД: денормализованные колонки ``schedule`` всегда равны ``time_slot``.

Механизм синхронизации живёт на том же уровне, что и защищаемый инвариант
(``ExclusionConstraint``, R8): Python-хуки (``Model.save``) игнорируются
``bulk_create()``, ``QuerySet.update()``, raw SQL и админ-actions — триггер
BEFORE INSERT OR UPDATE неотключаем ни одним из этих путей (Defense in Depth).

Закрываются обе дыры рассинхронизации:

1. Любая запись в ``schedule`` (включая bulk) → ``schedule_sync_time_slot``
   принудительно перечитывает день/время из ``time_slot``. Прямые записи в
   денормализованные колонки тем самым тоже невозможны — триггер их перетирает,
   колонки де-факто read-only производные.
2. Правка самого ``time_slot`` → ``time_slot_propagate_change`` обновляет все
   ссылающиеся ``schedule``; этот UPDATE заново прогоняет и триггер (1), и оба
   exclusion-констрейнта — перенос слота, создающий пересечение
   учителя/кабинета, откатится ошибкой БД, а не молча испортит сетку.

BEFORE-триггер срабатывает до проверки констрейнтов, поэтому INSERT из Django
с NULL в денормализованных колонках валиден: значения подставляются раньше
NOT NULL / CHECK / EXCLUDE.

Зависимость ``0002_initial`` — штатная автогенерация
(``uv run python manage.py makemigrations schedule``), см. ``0001``.
"""

from django.db import migrations

FORWARD_SQL = """
CREATE OR REPLACE FUNCTION schedule_sync_time_slot() RETURNS trigger AS $$
BEGIN
    SELECT ts.day_of_week, ts.start_time, ts.end_time
      INTO STRICT NEW.day_of_week, NEW.start_time, NEW.end_time
      FROM time_slot AS ts
     WHERE ts.id = NEW.time_slot_id;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER schedule_sync_time_slot_trg
BEFORE INSERT OR UPDATE ON schedule
FOR EACH ROW
EXECUTE FUNCTION schedule_sync_time_slot();

CREATE OR REPLACE FUNCTION time_slot_propagate_change() RETURNS trigger AS $$
BEGIN
    IF (NEW.day_of_week, NEW.start_time, NEW.end_time)
       IS DISTINCT FROM
       (OLD.day_of_week, OLD.start_time, OLD.end_time) THEN
        -- Фиктивный self-assignment будит BEFORE-триггер schedule,
        -- который и перечитает актуальные значения из time_slot.
        UPDATE schedule
           SET time_slot_id = time_slot_id
         WHERE time_slot_id = NEW.id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER time_slot_propagate_change_trg
AFTER UPDATE ON time_slot
FOR EACH ROW
EXECUTE FUNCTION time_slot_propagate_change();
"""

REVERSE_SQL = """
DROP TRIGGER IF EXISTS time_slot_propagate_change_trg ON time_slot;
DROP FUNCTION IF EXISTS time_slot_propagate_change();
DROP TRIGGER IF EXISTS schedule_sync_time_slot_trg ON schedule;
DROP FUNCTION IF EXISTS schedule_sync_time_slot();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("schedule", "0002_initial"),
    ]

    operations = [
        migrations.RunSQL(sql=FORWARD_SQL, reverse_sql=REVERSE_SQL),
    ]
