from __future__ import annotations

import datetime
from typing import TypedDict

from django.conf import settings
from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import transaction
from django.utils import timezone

from apps.billing.models import (
    Enrollment,
    EnrollmentStatus,
    Subscription,
    SubscriptionPlan,
    SubscriptionSlot,
    SubscriptionStatus,
)
from apps.catalog.models import Activity
from apps.content.models import GalleryImage
from apps.events.models import Event, EventRegistration, RegistrationStatus
from apps.journal.models import Lesson
from apps.public_forms.models import (
    CallbackRequest,
    CallTimeWindow,
    FeedbackRequest,
)
from apps.schedule.models import Room, Schedule, TimeSlot
from apps.users.models import Parent, Student, TeacherProfile

DEMO_ADMIN_EMAIL = "admin@demo.ru"
DEMO_ADMIN_PASSWORD = "admin123"  # noqa: S105 — демо-стенд, не секрет
DEMO_PARENT_EMAIL = "parent@demo.ru"
TEACHER_GROUP_NAME = "Учителя"


class _TeacherSeed(TypedDict):
    email: str
    full_name: str
    middle_name: str
    position: str
    quote: str


class _ActivitySeed(TypedDict):
    name: str
    slug: str
    price: int
    short: str
    tags: list[str]


_TEACHERS: list[_TeacherSeed] = [
    {
        "email": "e.smirnova@demo.ru",
        "full_name": "Смирнова Елена",
        "middle_name": "Викторовна",
        "position": "Педагог ментальной арифметики",
        "quote": "Счёт в уме — это гимнастика для мозга.",
    },
    {
        "email": "d.orlov@demo.ru",
        "full_name": "Орлов Дмитрий",
        "middle_name": "Сергеевич",
        "position": "Преподаватель робототехники",
        "quote": "Сначала ломаем, потом чиним — так и учимся.",
    },
    {
        "email": "a.kim@demo.ru",
        "full_name": "Ким Анна",
        "middle_name": "Александровна",
        "position": "Преподаватель английского",
        "quote": "Язык — это игра, в которую играют каждый день.",
    },
]

_ACTIVITIES: list[_ActivitySeed] = [
    {
        "name": "Ментальная арифметика",
        "slug": "mentalnaya-arifmetika",
        "price": 120_000,
        "short": "Устный счёт, память и концентрация для детей 6–12 лет.",
        "tags": ["математика", "логика"],
    },
    {
        "name": "Робототехника",
        "slug": "robototehnika",
        "price": 150_000,
        "short": "Конструируем и программируем роботов на LEGO и Arduino.",
        "tags": ["инженерия", "программирование"],
    },
    {
        "name": "Английский для детей",
        "slug": "angliyskiy-dlya-detey",
        "price": 110_000,
        "short": "Разговорный английский в игровой форме, группы по возрасту.",
        "tags": ["языки"],
    },
    {
        "name": "Шахматы",
        "slug": "shahmaty",
        "price": 100_000,
        "short": "От первых ходов до турниров, тренер с разрядом.",
        "tags": ["логика", "турниры"],
    },
]

# (день недели, начало, конец) — все слоты разные, коллизии
# преподавателей и кабинетов исключены по построению
_SLOT_GRID: list[tuple[int, datetime.time, datetime.time]] = [
    (0, datetime.time(16, 0), datetime.time(17, 0)),
    (0, datetime.time(17, 30), datetime.time(18, 30)),
    (1, datetime.time(16, 0), datetime.time(17, 0)),
    (2, datetime.time(17, 0), datetime.time(18, 0)),
    (3, datetime.time(16, 30), datetime.time(17, 30)),
    (4, datetime.time(18, 0), datetime.time(19, 0)),
    (5, datetime.time(10, 0), datetime.time(11, 0)),
    (5, datetime.time(11, 30), datetime.time(12, 30)),
]

# Тарифная сетка из project-context.md §6, цены в копейках
_PLANS: list[tuple[str, int, int, bool]] = [
    ("4 занятия (1 слот)", 1, 400_000, False),
    ("8 занятий (2 слота)", 2, 700_000, False),
    ("12 занятий (3 слота)", 3, 1_000_000, False),
    ("16 занятий (4 слота)", 4, 1_100_000, False),
    ("20 занятий (5 слотов)", 5, 1_200_000, False),
    ("Безлимит", 6, 1_500_000, True),
]


class Command(BaseCommand):
    help = "Наполняет базу демо-данными для локальной разработки и интеграции фронта."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--no-admin",
            action="store_true",
            help="Не создавать суперпользователя admin@demo.ru",
        )

    @transaction.atomic
    def handle(self, *args: object, **options: object) -> None:
        # ПОЧЕМУ: сиды содержат фиксированный пароль админа — на проде
        # это дыра, а не удобство
        if getattr(settings, "DEBUG", False) is False:
            raise CommandError(
                "seed_demo доступна только при DEBUG=True (локальная разработка)."
            )

        teachers = self._seed_teachers()
        activities = self._seed_activities()
        rooms = self._seed_rooms()
        groups = self._seed_schedule(activities, teachers, rooms)
        self._seed_plans()
        self._seed_events()
        self._seed_gallery()
        self._seed_form_requests()
        parent = self._seed_family(groups)
        if not options.get("no_admin"):
            self._seed_admin()

        self.stdout.write(self.style.SUCCESS("Демо-данные загружены."))
        self.stdout.write(f"Админка: {DEMO_ADMIN_EMAIL} / {DEMO_ADMIN_PASSWORD}")
        self.stdout.write(
            f"Демо-родитель: {DEMO_PARENT_EMAIL} — вход по OTP, код появится "
            "в логе backend (консольный email-бэкенд при DEBUG)."
        )
        self.stdout.write(f"Групп в расписании: {len(groups)}, родитель id={parent.pk}")

    def _seed_teachers(self) -> list[TeacherProfile]:
        group, _ = Group.objects.get_or_create(name=TEACHER_GROUP_NAME)
        profiles: list[TeacherProfile] = []
        for i, seed in enumerate(_TEACHERS):
            user = Parent.objects.filter(email=seed["email"]).first()
            if user is None:
                user = Parent.objects.create_user(
                    email=seed["email"], full_name=seed["full_name"], is_staff=True
                )
            user.groups.add(group)
            profile, _ = TeacherProfile.objects.get_or_create(
                user=user,
                defaults={
                    "middle_name": seed["middle_name"],
                    "position": seed["position"],
                    "quote": seed["quote"],
                    "photo_url": f"https://i.pravatar.cc/300?img={i + 11}",
                    "bio": f"{seed['position']}, стаж работы с детьми более 5 лет.",
                },
            )
            profiles.append(profile)
        return profiles

    def _seed_activities(self) -> list[Activity]:
        activities: list[Activity] = []
        for i, seed in enumerate(_ACTIVITIES):
            activity, _ = Activity.objects.get_or_create(
                slug=seed["slug"],
                defaults={
                    "name": seed["name"],
                    "category": "CLUB",
                    "price": seed["price"],
                    "short_description": seed["short"],
                    "description": (
                        f"{seed['short']} Занятия проходят раз в неделю в мини-группах "
                        "до 8 человек. Первое занятие — знакомство с педагогом."
                    ),
                    "cover_image": f"https://picsum.photos/seed/yra-{i}/800/600",
                    "features": ["Мини-группы", "Опытные педагоги"],
                    "tags": seed["tags"],
                    "is_active": True,
                },
            )
            activities.append(activity)
        return activities

    def _seed_rooms(self) -> list[Room]:
        return [
            Room.objects.get_or_create(name=name)[0]
            for name in ("Жёлтый кабинет", "Синий кабинет", "Зал для намаза")
        ]

    def _seed_schedule(
        self,
        activities: list[Activity],
        teachers: list[TeacherProfile],
        rooms: list[Room],
    ) -> list[Schedule]:
        groups: list[Schedule] = []
        for i, (day, start, end) in enumerate(_SLOT_GRID):
            slot, _ = TimeSlot.objects.get_or_create(
                day_of_week=day, start_time=start, end_time=end
            )
            activity = activities[i % len(activities)]
            schedule, _ = Schedule.objects.get_or_create(
                activity=activity,
                time_slot=slot,
                defaults={
                    "teacher": teachers[i % len(teachers)],
                    "room": rooms[i % len(rooms)],
                    "group_name": f"{activity.name} — группа {i // len(activities) + 1}",
                    "max_capacity": 8,
                    "age_min": 6 + (i % 3) * 2,
                    "age_max": 9 + (i % 3) * 2,
                    "is_active": True,
                },
            )
            groups.append(schedule)
        return groups

    def _seed_plans(self) -> None:
        for name, slots_count, price, is_unlimited in _PLANS:
            SubscriptionPlan.objects.get_or_create(
                name=name,
                defaults={
                    "slots_count": slots_count,
                    "price": price,
                    "base_session_price": round(price / (slots_count * 4)),
                    "is_unlimited": is_unlimited,
                    "is_active": True,
                },
            )

    def _seed_events(self) -> None:
        now = timezone.now()
        seeds = [
            ("Семейная игротека", 0, 14, 30),
            ("Мастер-класс по мышлению", 80_000, 7, 12),
        ]
        for i, (title, price, days_ahead, capacity) in enumerate(seeds):
            event, created = Event.objects.get_or_create(
                title=title,
                defaults={
                    "description": f"{title} в нашем центре. Количество мест ограничено.",
                    "cover_image": f"https://picsum.photos/seed/yra-event-{i}/800/600",
                    "start_datetime": now + datetime.timedelta(days=days_ahead),
                    "duration_minutes": 90,
                    "price": price,
                    "capacity": capacity,
                    "is_published": True,
                },
            )
            if created:
                EventRegistration.objects.create(
                    event=event,
                    child_name="Соня",
                    parent_name="Мария",
                    phone="+79990001122",
                    email="maria@example.com",
                    attendees_count=2,
                    source="instagram",
                    status=RegistrationStatus.CONFIRMED,
                )
                Event.objects.filter(pk=event.pk).update(seats_taken=2)

    def _seed_gallery(self) -> None:
        for i in range(6):
            GalleryImage.objects.get_or_create(
                image_url=f"https://picsum.photos/seed/yra-gallery-{i}/900/600",
                defaults={"order": i, "is_published": True},
            )

    def _seed_form_requests(self) -> None:
        if not CallbackRequest.objects.exists():
            CallbackRequest.objects.create(
                name="Ольга",
                phone="+79993334455",
                preferred_time_window=CallTimeWindow.EVENING,
            )
        if not FeedbackRequest.objects.exists():
            FeedbackRequest.objects.create(
                name="Ирина",
                email="irina@example.com",
                message="Подскажите, есть ли места в группу робототехники по субботам?",
            )

    def _seed_family(self, groups: list[Schedule]) -> Parent:
        parent = Parent.objects.filter(email=DEMO_PARENT_EMAIL).first()
        if parent is None:
            parent = Parent.objects.create_user(
                email=DEMO_PARENT_EMAIL, full_name="Правый лев"
            )
            parent.phone = "+79991234567"
            parent.save(update_fields=["phone"])

        today = timezone.localdate()
        children = [
            Student.objects.get_or_create(
                parent=parent,
                full_name=name,
                dob=dob,
                defaults={"school_grade": grade},
            )[0]
            for name, dob, grade in (
                ("Синяев Мирон", datetime.date(2017, 5, 12), "2"),
                ("Имажап Очур-Бады", datetime.date(2019, 9, 3), ""),
            )
        ]

        if not Subscription.objects.filter(parent=parent).exists():
            plan = SubscriptionPlan.objects.get(slots_count=2, is_unlimited=False)
            subscription = Subscription.objects.create(
                parent=parent,
                plan=plan,
                status=SubscriptionStatus.ACTIVE,
                purchase_price=plan.price,
                base_session_price=plan.base_session_price,
                start_date=today,
                expires_at=timezone.now() + datetime.timedelta(days=30),
            )
            for schedule in groups[:2]:
                Enrollment.objects.create(
                    student=children[0],
                    subscription=subscription,
                    schedule=schedule,
                    status=EnrollmentStatus.ENROLLED,
                )
                SubscriptionSlot.objects.create(
                    subscription=subscription,
                    slot_id=schedule.pk,
                    granted_tokens=4,
                    remaining_tokens=4,
                )
            # Занятие на сегодня, чтобы журнал в админке не был пустым
            Lesson.objects.get_or_create(
                schedule=groups[0], date=today, defaults={"topic": "Вводное занятие"}
            )
        return parent

    def _seed_admin(self) -> None:
        if not Parent.objects.filter(email=DEMO_ADMIN_EMAIL).exists():
            Parent.objects.create_superuser(
                email=DEMO_ADMIN_EMAIL,
                full_name="Администратор",
                password=DEMO_ADMIN_PASSWORD,
            )
