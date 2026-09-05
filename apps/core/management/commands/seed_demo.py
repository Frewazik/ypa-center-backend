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

_ROOM_NAMES: tuple[str, ...] = ("Жёлтый кабинет", "Синий кабинет", "Зелёный кабинет")


class _TeacherSeed(TypedDict):
    email: str
    full_name: str
    middle_name: str
    position: str
    quote: str
    bio: str


class _ActivitySeed(TypedDict):
    name: str
    slug: str
    price: int
    short: str
    description: str
    tags: list[str]
    features: list[str]


# ПОЧЕМУ: индекс в списке = индекс в _ACTIVITIES и в _ACTIVITY_SLOTS —
# один преподаватель ведёт один кружок, без модульной раскидки как раньше
_TEACHERS: list[_TeacherSeed] = [
    {
        "email": "e.smirnova@demo.ru",
        "full_name": "Смирнова Елена",
        "middle_name": "Викторовна",
        "position": "Педагог ментальной арифметики",
        "quote": "Счёт в уме — это гимнастика для мозга.",
        "bio": (
            "Преподаёт ментальную арифметику больше 7 лет, обучалась методике соробана "
            "в сертифицированной школе. Считает, что главный результат курса — не "
            "скорость счёта, а умение ребёнка удерживать внимание на задаче до конца, "
            "не отвлекаясь. Следит, чтобы каждый продвигался в своём темпе, без "
            "сравнения с другими детьми в группе."
        ),
    },
    {
        "email": "d.orlov@demo.ru",
        "full_name": "Орлов Дмитрий",
        "middle_name": "Сергеевич",
        "position": "Преподаватель робототехники",
        "quote": "Сначала ломаем, потом чиним — так и учимся.",
        "bio": (
            "Инженер по образованию, до преподавания несколько лет работал на "
            "производстве автоматизированных систем. В работе с детьми делает ставку "
            "на живой эксперимент: если конструкция развалилась — это повод "
            "разобраться, почему, а не повод расстроиться. Ведёт занятия так, чтобы "
            "к концу блока у каждого ребёнка был собственный работающий проект."
        ),
    },
    {
        "email": "a.kim@demo.ru",
        "full_name": "Ким Анна",
        "middle_name": "Александровна",
        "position": "Преподаватель английского",
        "quote": "Язык — это игра, в которую играют каждый день.",
        "bio": (
            "Преподаёт английский детям больше 6 лет, работала также с "
            "билингвальными группами. Убеждена, что страх ошибиться — главный тормоз "
            "в изучении языка, поэтому на занятиях нет оценок за произношение — только "
            "поддержка и повтор. Регулярно обновляет программу под интересы "
            "конкретной группы — от мультфильмов до настольных игр на английском."
        ),
    },
    {
        "email": "i.volkov@demo.ru",
        "full_name": "Волков Игорь",
        "middle_name": "Петрович",
        "position": "Тренер по шахматам",
        "quote": "Шахматы учат проигрывать достойно — это половина успеха в жизни.",
        "bio": (
            "Кандидат в мастера спорта по шахматам, судья второй категории, "
            "тренирует детей больше 10 лет. Считает, что шахматы — не про "
            "запоминание дебютов, а про привычку думать на несколько ходов вперёд "
            "и спокойно разбирать свои ошибки после партии. Дважды в год вывозит "
            "учеников на городские турниры среди детских клубов."
        ),
    },
]

_ACTIVITIES: list[_ActivitySeed] = [
    {
        "name": "Ментальная арифметика",
        "slug": "mentalnaya-arifmetika",
        "price": 120_000,
        "short": "Устный счёт на соробане, память и концентрация для детей 6–12 лет.",
        "description": (
            "Дети считают на соробане — японских счётах, а затем учатся представлять "
            "его в уме и считать без косточек. Это не про быстрый счёт ради счёта: "
            "тренируются одновременно оба полушария, развивается память, внимание и "
            "усидчивость. Группы маленькие — до 8 человек, педагог успевает разобрать "
            "ошибку каждого. Раз в два месяца — контрольный срез, чтобы родители "
            "видели реальный прогресс, а не просто посещаемость."
        ),
        "features": ["математика", "логика", "память", "концентрация", "устный счёт"],
        "tags": [
            "Соробан и мысленный счёт",
            "Мини-группы до 8 человек",
            "Контрольные срезы раз в 2 месяца",
            "Домашние задания с разбором ошибок",
        ],
    },
    {
        "name": "Робототехника",
        "slug": "robototehnika",
        "price": 150_000,
        "short": "Конструируем и программируем роботов на LEGO и Arduino.",
        "description": (
            "От первых механизмов на LEGO до программирования Arduino — дети проходят "
            "путь от конструктора до работающего устройства своими руками. Каждый блок "
            "занятий заканчивается собственным проектом: миксером, роботом-манипулятором "
            "или светофором с датчиком. Учим не просто повторять инструкцию, а "
            "объяснять, почему деталь стоит именно тут. Раз в семестр — внутренний "
            "конкурс проектов, где дети защищают свою работу перед родителями."
        ),
        "features": [
            "инженерия",
            "программирование",
            "LEGO",
            "Arduino",
            "конструирование",
        ],
        "tags": [
            "Проектное обучение — свой робот на каждый блок",
            "LEGO и Arduino в одной программе",
            "Конкурс проектов раз в семестр",
            "Свой набор деталей на ребёнка",
        ],
    },
    {
        "name": "Английский для детей",
        "slug": "angliyskiy-dlya-detey",
        "price": 110_000,
        "short": "Разговорный английский в игровой форме, группы по возрасту.",
        "description": (
            "Английский без зубрёжки правил: дети играют, поют, разыгрывают сценки — "
            "и незаметно для себя начинают говорить. Группы собраны строго по возрасту, "
            "поэтому темп и лексика подобраны под конкретный этап развития речи. "
            "Педагог использует международные учебники Cambridge и следит за прогрессом "
            "по чётким уровням — от starter до elementary. Родители получают короткий "
            "отчёт по итогам каждого месяца: что ребёнок уже умеет сказать сам."
        ),
        "features": [
            "языки",
            "английский",
            "разговорная практика",
            "международные программы",
        ],
        "tags": [
            "Группы строго по возрасту",
            "Учебники Cambridge",
            "Ежемесячный отчёт для родителей",
            "Игровой формат без письменных тестов",
        ],
    },
    {
        "name": "Шахматы",
        "slug": "shahmaty",
        "price": 100_000,
        "short": "От первых ходов до турниров, тренер с разрядом.",
        "description": (
            "Начинаем с базовых правил и техники безопасности фигур, дальше — "
            "дебютные схемы, простые эндшпили и первые турнирные партии внутри клуба. "
            "Тренер — кандидат в мастера спорта, ведёт занятия так, чтобы ребёнок "
            "учился думать на несколько ходов вперёд, а не запоминать готовые "
            "комбинации. Дважды в год — открытый турнир с награждением, куда можно "
            "позвать родителей поболеть."
        ),
        "features": ["логика", "стратегия", "шахматы", "турниры"],
        "tags": [
            "Тренер — КМС по шахматам",
            "Турнир внутри клуба дважды в год",
            "От новичка до разрядной подготовки",
            "Разбор партий после каждой игры",
        ],
    },
]

# Слоты на каждый кружок: (день недели, начало, конец, индекс кабинета в _ROOM_NAMES,
# возраст от, возраст до). 3 группы на кружок. Преподаватель у каждого кружка один —
# коллизий по преподавателю между новыми слотами быть не может по построению.
# ПОЧЕМУ дни/время именно такие: в базе с прошлых прогонов уже сидят 8 старых групп
# расписания (Смирнова: Пн16-17/Ср17-18/Сб10-11, Орлов: Пн17:30-18:30/Чт16:30-17:30/
# Сб11:30-12:30, Ким: Вт16-17/Пт18-19) — их нельзя тихо снести (Enrollment/DepositEntry
# демо-подписки на них ссылаются через PROTECT), поэтому новые слоты для тех же
# преподавателей намеренно поставлены на другие дни/часы, без пересечения по
# преподавателю и по кабинету — проверено вручную при составлении
_ACTIVITY_SLOTS: list[list[tuple[int, datetime.time, datetime.time, int, int, int]]] = [
    # Ментальная арифметика (Смирнова)
    [
        (1, datetime.time(16, 30), datetime.time(17, 30), 0, 6, 9),
        (3, datetime.time(16, 0), datetime.time(17, 0), 0, 8, 11),
        (5, datetime.time(12, 0), datetime.time(13, 0), 0, 6, 9),
    ],
    # Робототехника (Орлов)
    [
        (1, datetime.time(17, 30), datetime.time(18, 30), 1, 8, 11),
        (4, datetime.time(16, 0), datetime.time(17, 0), 0, 10, 13),
        (5, datetime.time(10, 0), datetime.time(11, 0), 1, 8, 11),
    ],
    # Английский для детей (Ким)
    [
        (0, datetime.time(18, 0), datetime.time(19, 0), 2, 6, 9),
        (2, datetime.time(16, 0), datetime.time(17, 0), 1, 10, 13),
        (5, datetime.time(13, 0), datetime.time(14, 0), 2, 6, 9),
    ],
    # Шахматы (Волков) — новый преподаватель, старых занятых слотов нет
    [
        (2, datetime.time(17, 30), datetime.time(18, 30), 2, 6, 9),
        (4, datetime.time(17, 0), datetime.time(18, 0), 2, 8, 11),
        (5, datetime.time(9, 0), datetime.time(10, 0), 0, 10, 13),
    ],
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
        parent = self._seed_family(activities, groups)
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
            # update_or_create — чтобы повторный прогон обновлял био/цитату
            # у уже существующих преподавателей, а не игнорировал правки
            profile, _ = TeacherProfile.objects.update_or_create(
                user=user,
                defaults={
                    "middle_name": seed["middle_name"],
                    "position": seed["position"],
                    "quote": seed["quote"],
                    "photo_url": f"https://i.pravatar.cc/300?img={i + 11}",
                    "bio": seed["bio"],
                },
            )
            profiles.append(profile)
        return profiles

    def _seed_activities(self) -> list[Activity]:
        activities: list[Activity] = []
        for i, seed in enumerate(_ACTIVITIES):
            activity, _ = Activity.objects.update_or_create(
                slug=seed["slug"],
                defaults={
                    "name": seed["name"],
                    "category": "CLUB",
                    "price": seed["price"],
                    "short_description": seed["short"],
                    "description": seed["description"],
                    "cover_image": f"https://picsum.photos/seed/yra-{i}/800/600",
                    "features": seed["features"],
                    "tags": seed["tags"],
                    "is_active": True,
                },
            )
            activities.append(activity)
        return activities

    def _seed_rooms(self) -> list[Room]:
        # Переименовываем мусорное название, если оно осталось от старых прогонов
        Room.objects.filter(name="Зал для намаза").update(name="Зелёный кабинет")
        return [Room.objects.get_or_create(name=name)[0] for name in _ROOM_NAMES]

    def _seed_schedule(
        self,
        activities: list[Activity],
        teachers: list[TeacherProfile],
        rooms: list[Room],
    ) -> list[Schedule]:
        groups: list[Schedule] = []
        for activity_idx, slots in enumerate(_ACTIVITY_SLOTS):
            activity = activities[activity_idx]
            teacher = teachers[activity_idx]
            for group_idx, (day, start, end, room_idx, age_min, age_max) in enumerate(
                slots
            ):
                time_slot, _ = TimeSlot.objects.get_or_create(
                    day_of_week=day, start_time=start, end_time=end
                )
                schedule, _ = Schedule.objects.update_or_create(
                    activity=activity,
                    time_slot=time_slot,
                    defaults={
                        "teacher": teacher,
                        "room": rooms[room_idx],
                        "group_name": f"{activity.name} — группа {group_idx + 1}",
                        "max_capacity": 8,
                        "age_min": age_min,
                        "age_max": age_max,
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

    def _seed_family(
        self, activities: list[Activity], groups: list[Schedule]
    ) -> Parent:
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
            # Берём по одной группе из двух разных кружков, а не первые попавшиеся —
            # так демо-абонемент показывает реальную комбинацию направлений
            demo_groups = [
                next(g for g in groups if g.activity_id == activities[0].pk),
                next(g for g in groups if g.activity_id == activities[1].pk),
            ]
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
            for schedule in demo_groups:
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
                schedule=demo_groups[0],
                date=today,
                defaults={"topic": "Вводное занятие"},
            )
        return parent

    def _seed_admin(self) -> None:
        if not Parent.objects.filter(email=DEMO_ADMIN_EMAIL).exists():
            Parent.objects.create_superuser(
                email=DEMO_ADMIN_EMAIL,
                full_name="Администратор",
                password=DEMO_ADMIN_PASSWORD,
            )
