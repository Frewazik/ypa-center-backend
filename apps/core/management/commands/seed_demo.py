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
from apps.schedule.models import Schedule, TimeSlot
from apps.users.models import Parent, ReferralSource, Student, TeacherProfile

DEMO_ADMIN_EMAIL = "admin@demo.ru"
DEMO_ADMIN_PASSWORD = "admin123"  # noqa: S105 — демо-стенд, не секрет
DEMO_PARENT_EMAIL = "parent@demo.ru"
TEACHER_GROUP_NAME = "Учителя"

# Источник всех фактов ниже — docs/project-context.md §3 «Каталог центра сейчас»
_THINKING_SLUG = "kruzhok-myshleniya"
_ENGLISH_SLUG = "angliyskiy-yazyk"
_MORDVINOV_EMAIL = "y.mordvinov@demo.ru"
_MAKUKHA_EMAIL = "n.makukha@demo.ru"

# ПОЧЕМУ: выдуманный каталог прошлых версий сида остался в локальных базах;
# удалить его нельзя (демо-подписки ссылаются через PROTECT) — выключаем
_RETIRED_ACTIVITY_SLUGS: tuple[str, ...] = (
    "mentalnaya-arifmetika",
    "robototehnika",
    "angliyskiy-dlya-detey",
    "shahmaty",
)
_RETIRED_EVENT_TITLES: tuple[str, ...] = (
    "Семейная игротека",
    "Мастер-класс по мышлению",
)
_RETIRED_GALLERY_PREFIX = "https://picsum.photos/"

_UNSPLASH_PARAMS = "?w=1200&h=800&fit=crop&fm=jpg&q=80"


def _unsplash(photo_id: str) -> str:
    return f"https://images.unsplash.com/photo-{photo_id}{_UNSPLASH_PARAMS}"


class _TeacherSeed(TypedDict):
    email: str
    full_name: str
    middle_name: str
    position: str
    bio: str


class _ActivitySeed(TypedDict):
    name: str
    slug: str
    price: int
    short: str
    description: str
    cover_image: str
    features: list[str]
    tags: list[str]


class _GroupSeed(TypedDict):
    activity_slug: str
    teacher_email: str
    group_name: str
    day: int
    start: datetime.time
    end: datetime.time
    age_min: int | None
    age_max: int | None


_TEACHERS: list[_TeacherSeed] = [
    {
        "email": _MAKUKHA_EMAIL,
        "full_name": "Макуха Надежда",
        "middle_name": "Геннадьевна",
        "position": "Основатель центра, преподаватель английского языка",
        "bio": (
            "Основатель «Улицы Радости» — центр работает с сентября 2022 года. "
            "Преподаёт английский язык, в том числе в СУНЦ НГУ, есть сертификаты "
            "CAE и TKT. Также Монтессори-педагог начальной школы. В центре ведёт "
            "все группы английского — от нулевого класса до помощи с домашними "
            "заданиями."
        ),
    },
    {
        "email": _MORDVINOV_EMAIL,
        "full_name": "Мордвинов Яков",
        "middle_name": "Леонидович",
        "position": "Педагог Кружка Мышления",
        "bio": (
            "Кандидат физико-математических наук. Развивает мышление у детей "
            "больше 20 лет. В «Улице Радости» ведёт Кружок Мышления — основное "
            "направление центра — для учеников 1–9 классов."
        ),
    },
]

# ПОЧЕМУ price 1 200 ₽: Activity.price — цена пробного занятия (schedule/ports.py),
# у центра пробное и разовое занятие стоят 1 200 ₽
_ACTIVITIES: list[_ActivitySeed] = [
    {
        "name": "Кружок Мышления",
        "slug": _THINKING_SLUG,
        "price": 120_000,
        "short": "Основное направление центра: развиваем мышление у школьников 1–9 классов.",
        "description": (
            "Кружок Мышления — основное направление «Улицы Радости». Его ведёт "
            "Яков Леонидович Мордвинов, кандидат физико-математических наук, "
            "который развивает мышление у детей больше 20 лет. На занятиях — "
            "интегрированные методики и широкий спектр заданий, а ещё подготовка "
            "к конкурсу «Кенгуру». Группа одна для учеников 1–9 классов: занятие "
            "длится 60 минут, слоты есть каждый будний день и в субботу."
        ),
        "cover_image": _unsplash("1583938001302-501eb68d92cf"),
        "features": [
            "Занятия строятся на интегрированных методиках и разнообразных заданиях",
            "Готовим детей к участию в конкурсе «Кенгуру»",
            "Ведёт кандидат физико-математических наук с опытом больше 20 лет",
            "Занятия проходят каждый будний день и в субботу",
        ],
        "tags": ["Развитие мышления", "Конкурс «Кенгуру»", "1–9 классы"],
    },
    {
        "name": "Английский язык",
        "slug": _ENGLISH_SLUG,
        "price": 120_000,
        "short": "Английский для 0–5 классов и помощь с домашними заданиями.",
        "description": (
            "Английский в «Улице Радости» ведёт основатель центра Надежда "
            "Геннадьевна Макуха — преподаватель английского с сертификатами CAE "
            "и TKT. На занятиях развиваем все навыки: грамматику и лексику, "
            "чтение, аудирование, говорение и письмо. С младшими занимаемся в "
            "игровой форме. Группы собраны по классам — от нулевого до пятого, а "
            "по пятницам есть отдельная группа помощи с домашними заданиями."
        ),
        "cover_image": _unsplash("1725398925420-11515f2cbb95"),
        "features": [
            "Развиваем грамматику, лексику, чтение, аудирование, говорение и письмо",
            "Для младших учеников занятия проходят в игровой форме",
            "Ведёт преподаватель английского с сертификатами CAE и TKT",
            "Отдельная группа помогает с домашними заданиями по английскому",
        ],
        "tags": ["Грамматика и лексика", "Аудирование", "Говорение", "Чтение и письмо"],
    },
]


def _group(
    activity_slug: str,
    teacher_email: str,
    group_name: str,
    day: int,
    start: datetime.time,
    end: datetime.time,
    ages: tuple[int | None, int | None],
) -> _GroupSeed:
    return {
        "activity_slug": activity_slug,
        "teacher_email": teacher_email,
        "group_name": group_name,
        "day": day,
        "start": start,
        "end": end,
        "age_min": ages[0],
        "age_max": ages[1],
    }


# ПОЧЕМУ возраст так: N-й класс — N+6…N+7 лет (решение заказчика 2026-10-04).
# У «Помощи с ДЗ» классов в материалах центра нет — без ограничения
_THINKING_AGES = (7, 16)
_SATURDAY = 5

# ПОЧЕМУ без кабинетов: у центра одно пространство (§3), а Room с ограничением
# на пересечение запретил бы одновременные занятия Вт/Пт 16:00 и 17:00
_GROUPS: list[_GroupSeed] = [
    *(
        _group(
            _THINKING_SLUG,
            _MORDVINOV_EMAIL,
            "1–9 классы",
            day,
            datetime.time(hour, 0),
            datetime.time(hour + 1, 0),
            _THINKING_AGES,
        )
        for day in range(5)
        for hour in (15, 16, 17)
    ),
    _group(
        _THINKING_SLUG,
        _MORDVINOV_EMAIL,
        "1–9 классы",
        _SATURDAY,
        datetime.time(11, 0),
        datetime.time(12, 0),
        _THINKING_AGES,
    ),
    _group(
        _ENGLISH_SLUG,
        _MAKUKHA_EMAIL,
        "0 класс",
        _SATURDAY,
        datetime.time(13, 30),
        datetime.time(14, 30),
        (6, 7),
    ),
    _group(
        _ENGLISH_SLUG,
        _MAKUKHA_EMAIL,
        "1 класс",
        0,
        datetime.time(13, 0),
        datetime.time(14, 0),
        (7, 8),
    ),
    _group(
        _ENGLISH_SLUG,
        _MAKUKHA_EMAIL,
        "2 класс",
        1,
        datetime.time(17, 0),
        datetime.time(18, 0),
        (8, 9),
    ),
    *(
        _group(
            _ENGLISH_SLUG,
            _MAKUKHA_EMAIL,
            "3–5 классы",
            day,
            datetime.time(16, 0),
            datetime.time(16, 55),
            (9, 12),
        )
        for day in (1, 4)
    ),
    _group(
        _ENGLISH_SLUG,
        _MAKUKHA_EMAIL,
        "Помощь с ДЗ",
        4,
        datetime.time(17, 0),
        datetime.time(18, 0),
        (None, None),
    ),
]

# ПОЧЕМУ 6: вместимости групп в материалах центра нет, 6 — умолчание модели
_GROUP_CAPACITY = 6

_BOARD_GAMES_TITLE = "Настольные игры"
_BOARD_GAMES_DESCRIPTION = (
    "Каждую среду с 17:00 до 19:00 играем в настольные игры на русском и "
    "английском языках. Участие бесплатное, вход свободный. Это основной способ "
    "познакомиться с «Улицей Радости»."
)
_BOARD_GAMES_COVER = _unsplash("1729343587051-223b013daec0")
_BOARD_GAMES_START = datetime.time(17, 0)
_BOARD_GAMES_COUNT = 4
_BOARD_GAMES_CAPACITY = 10
_WEDNESDAY = 2

# Демо-«аншлаг» на ближайшей игре: в сумме ровно _BOARD_GAMES_CAPACITY мест
_BOARD_GAMES_GUESTS: tuple[tuple[str, str, str, int], ...] = (
    ("Соня", "Мария", "+79990001122", 2),
    ("Тимофей", "Анна", "+79990001123", 2),
    ("Вера", "Ольга", "+79990001124", 3),
    ("Лёва", "Дмитрий", "+79990001125", 1),
    ("Миша", "Елена", "+79990001126", 2),
)

_GALLERY_PHOTOS: tuple[str, ...] = (
    "1489850846882-35ef10a4b480",
    "1587390874738-7a3888f8ba9b",
    "1588072432836-e10032774350",
    "1617117206620-b01f2919ff86",
    "1613950190144-4f2a84c75e8c",
    "1529390079861-591de354faf5",
)

# Тарифная сетка из project-context.md §6, цены в копейках
_PLANS: list[tuple[str, int, int, bool]] = [
    ("4 занятия (1 слот)", 1, 400_000, False),
    ("8 занятий (2 слота)", 2, 700_000, False),
    ("12 занятий (3 слота)", 3, 1_000_000, False),
    ("16 занятий (4 слота)", 4, 1_100_000, False),
    ("20 занятий (5 слотов)", 5, 1_200_000, False),
    ("Безлимит", 6, 1_500_000, True),
]
# ПОЧЕМУ: скидка тарифа действует только на цену покупки; при истечении
# посещения считаются без скидки — по базовой цене занятия, одной для всех
# тарифов (решение бизнеса 2026-10-04)
_BASE_SESSION_PRICE = 120_000

_GroupKey = tuple[str, int, datetime.time]


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

        self._retire_old_demo()
        teachers = self._seed_teachers()
        activities = self._seed_activities()
        groups = self._seed_schedule(activities, teachers)
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

    def _retire_old_demo(self) -> None:
        # ПОЧЕМУ save(), а не QuerySet.update(): сигналы public_api сбрасывают
        # кэш витрины только на save — иначе старые карточки живут до TTL
        for schedule in Schedule.objects.filter(
            activity__slug__in=_RETIRED_ACTIVITY_SLUGS, is_active=True
        ):
            schedule.is_active = False
            schedule.save(update_fields=["is_active", "updated_at"])
        for activity in Activity.objects.filter(
            slug__in=_RETIRED_ACTIVITY_SLUGS, is_active=True
        ):
            activity.is_active = False
            activity.save(update_fields=["is_active"])
        for event in Event.objects.filter(
            title__in=_RETIRED_EVENT_TITLES, is_published=True
        ):
            event.is_published = False
            event.save(update_fields=["is_published"])
        for image in GalleryImage.objects.filter(
            image_url__startswith=_RETIRED_GALLERY_PREFIX, is_published=True
        ):
            image.is_published = False
            image.save(update_fields=["is_published"])

    def _seed_teachers(self) -> dict[str, TeacherProfile]:
        group, _ = Group.objects.get_or_create(name=TEACHER_GROUP_NAME)
        profiles: dict[str, TeacherProfile] = {}
        for seed in _TEACHERS:
            user = Parent.objects.filter(email=seed["email"]).first()
            if user is None:
                user = Parent.objects.create_user(
                    email=seed["email"], full_name=seed["full_name"], is_staff=True
                )
            user.groups.add(group)
            # ПОЧЕМУ quote и photo_url пустые: это реальные люди — чужое
            # стоковое лицо и придуманная цитата от их имени недопустимы
            profile, _ = TeacherProfile.objects.update_or_create(
                user=user,
                defaults={
                    "middle_name": seed["middle_name"],
                    "position": seed["position"],
                    "quote": "",
                    "photo_url": "",
                    "bio": seed["bio"],
                },
            )
            profiles[seed["email"]] = profile
        return profiles

    def _seed_activities(self) -> dict[str, Activity]:
        activities: dict[str, Activity] = {}
        for seed in _ACTIVITIES:
            activity, _ = Activity.objects.update_or_create(
                slug=seed["slug"],
                defaults={
                    "name": seed["name"],
                    "category": "CLUB",
                    "price": seed["price"],
                    "short_description": seed["short"],
                    "description": seed["description"],
                    "cover_image": seed["cover_image"],
                    "features": seed["features"],
                    "tags": seed["tags"],
                    "is_active": True,
                },
            )
            activities[seed["slug"]] = activity
        return activities

    def _seed_schedule(
        self,
        activities: dict[str, Activity],
        teachers: dict[str, TeacherProfile],
    ) -> dict[_GroupKey, Schedule]:
        groups: dict[_GroupKey, Schedule] = {}
        for seed in _GROUPS:
            time_slot, _ = TimeSlot.objects.get_or_create(
                day_of_week=seed["day"], start_time=seed["start"], end_time=seed["end"]
            )
            schedule, _ = Schedule.objects.update_or_create(
                activity=activities[seed["activity_slug"]],
                time_slot=time_slot,
                defaults={
                    "teacher": teachers[seed["teacher_email"]],
                    "room": None,
                    "group_name": seed["group_name"],
                    "max_capacity": _GROUP_CAPACITY,
                    "age_min": seed["age_min"],
                    "age_max": seed["age_max"],
                    "is_active": True,
                },
            )
            groups[(seed["activity_slug"], seed["day"], seed["start"])] = schedule
        return groups

    def _seed_plans(self) -> None:
        for name, slots_count, price, is_unlimited in _PLANS:
            SubscriptionPlan.objects.get_or_create(
                name=name,
                defaults={
                    "slots_count": slots_count,
                    "price": price,
                    "base_session_price": _BASE_SESSION_PRICE,
                    "is_unlimited": is_unlimited,
                    "is_active": True,
                },
            )

    def _seed_events(self) -> None:
        starts = _upcoming_wednesdays(timezone.now(), _BOARD_GAMES_COUNT)
        for start in starts:
            Event.objects.update_or_create(
                title=_BOARD_GAMES_TITLE,
                start_datetime=start,
                defaults={
                    "description": _BOARD_GAMES_DESCRIPTION,
                    "cover_image": _BOARD_GAMES_COVER,
                    "duration_minutes": 120,
                    "price": 0,
                    "capacity": _BOARD_GAMES_CAPACITY,
                    "is_published": True,
                },
            )

        nearest = Event.objects.get(title=_BOARD_GAMES_TITLE, start_datetime=starts[0])
        if EventRegistration.objects.filter(event=nearest).exists():
            return
        for child_name, parent_name, phone, attendees in _BOARD_GAMES_GUESTS:
            EventRegistration.objects.create(
                event=nearest,
                child_name=child_name,
                parent_name=parent_name,
                phone=phone,
                attendees_count=attendees,
                amount=0,
                source="instagram",
                status=RegistrationStatus.CONFIRMED,
            )
        nearest.seats_taken = sum(guest[3] for guest in _BOARD_GAMES_GUESTS)
        nearest.save(update_fields=["seats_taken"])

    def _seed_gallery(self) -> None:
        for order, photo_id in enumerate(_GALLERY_PHOTOS):
            GalleryImage.objects.update_or_create(
                image_url=_unsplash(photo_id),
                defaults={"order": order, "is_published": True},
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
                message="Подскажите, есть ли места в группе английского для 2 класса?",
            )

    def _seed_family(self, groups: dict[_GroupKey, Schedule]) -> Parent:
        parent = Parent.objects.filter(email=DEMO_PARENT_EMAIL).first()
        if parent is None:
            parent = Parent.objects.create_user(
                email=DEMO_PARENT_EMAIL, full_name="Правый лев"
            )
            parent.phone = "+79991234567"
            # Анкета заполнена — иначе демо-ЛК отвечает 403 PROFILE_INCOMPLETE
            parent.referral_source = ReferralSource.FRIENDS
            # Демо-данные: согласие проставлено напрямую, без журнала — на
            # проде оно появляется только из анкеты (apps.users.consent)
            parent.pd_consent_at = timezone.now()
            parent.save(update_fields=["phone", "referral_source", "pd_consent_at"])

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
            # Мирон во 2 классе: Кружок Мышления Пн 16:00 + английский «2 класс»
            demo_groups = [
                groups[(_THINKING_SLUG, 0, datetime.time(16, 0))],
                groups[(_ENGLISH_SLUG, 1, datetime.time(17, 0))],
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


def _upcoming_wednesdays(now: datetime.datetime, count: int) -> list[datetime.datetime]:
    starts: list[datetime.datetime] = []
    day = timezone.localtime(now).date()
    while len(starts) < count:
        if day.weekday() == _WEDNESDAY:
            start = timezone.make_aware(
                datetime.datetime.combine(day, _BOARD_GAMES_START)
            )
            if start > now:
                starts.append(start)
        day += datetime.timedelta(days=1)
    return starts
