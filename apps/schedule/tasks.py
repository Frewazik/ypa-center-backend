from __future__ import annotations

import datetime
import logging

from django.conf import settings
from django.core.mail import send_mail

from apps.billing.models import EnrollmentType
from apps.billing.selectors import lesson_seat_holders
from apps.schedule.models import DayOfWeek, MaskType, Schedule, ScheduleMask
from apps.schedule.services import (
    LessonChange,
    _effective_session,
    _teacher_full_name,
)
from apps.users.models import Parent
from config.tkq import broker

logger = logging.getLogger(__name__)


@broker.task(retry_on_error=True, max_retries=3)
def send_lesson_change_email_task(
    parent_id: int, schedule_id: int, target_date: str, change: LessonChange
) -> None:
    # ПОЧЕМУ аргументы, а не id маски: после «отмены отмены» маски уже нет,
    # а письмо «занятие состоится» всё равно нужно. Ставится строго из
    # on_commit (services._notify_families). Текущее состояние маски
    # перечитывается: если её успели удалить или вернуть, письмо устарело
    session_date = datetime.date.fromisoformat(target_date)
    parent = Parent.objects.filter(pk=parent_id).first()
    schedule = (
        Schedule.objects.select_related("activity").filter(pk=schedule_id).first()
    )
    if parent is None or schedule is None:
        logger.error(
            "Письмо об изменении занятия: родитель %s или группа %s не найдены.",
            parent_id,
            schedule_id,
        )
        return
    mask = (
        ScheduleMask.objects.select_related("new_room", "new_teacher__user")
        .filter(schedule_id=schedule_id, target_date=session_date)
        .first()
    )
    if (mask is None) != (change == "removed"):
        logger.info(
            "Письмо об изменении занятия группы %s на %s устарело — пропуск.",
            schedule_id,
            target_date,
        )
        return
    has_trial = (
        lesson_seat_holders([schedule_id], on_date=session_date)
        .filter(student__parent_id=parent_id, type=EnrollmentType.TRIAL)
        .exists()
    )
    lesson = (
        f"Занятие «{schedule.activity.name}"
        + (f" — {schedule.group_name}" if schedule.group_name else "")
        + f"» {session_date:%d.%m.%Y} в {schedule.start_time:%H:%M}"
    )
    greeting = (
        f"Здравствуйте, {parent.full_name}!" if parent.full_name else "Здравствуйте!"
    )
    send_mail(
        subject="Изменение в расписании — «Улица Радости»",
        message=f"{greeting}\n\n{_change_text(lesson, schedule, mask, has_trial)}",
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[parent.email],
        fail_silently=False,
    )


def _change_text(
    lesson: str, schedule: Schedule, mask: ScheduleMask | None, has_trial: bool
) -> str:
    if mask is None:
        return (
            f"{lesson} состоится по обычному расписанию — прежнее сообщение "
            "об отмене или переносе больше не действует."
        )
    if mask.type == MaskType.CANCELLATION:
        text = f"{lesson} отменено."
        if has_trial:
            text += (
                "\nАдминистратор свяжется с вами, чтобы вернуть оплату "
                "пробного занятия."
            )
        return text
    landing = _effective_session(schedule, mask)
    text = (
        f"{lesson} переносится на "
        f"{DayOfWeek(landing.day_of_week).label.lower()} "
        f"{landing.date:%d.%m.%Y}, {landing.start_time:%H:%M}–"
        f"{landing.end_time:%H:%M}."
    )
    if mask.new_room is not None:
        text += f"\nКабинет: {mask.new_room.name}."
    teacher_name = _teacher_full_name(mask.new_teacher)
    if teacher_name is not None:
        text += f"\nПреподаватель: {teacher_name}."
    return text
