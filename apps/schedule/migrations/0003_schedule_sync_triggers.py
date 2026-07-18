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
