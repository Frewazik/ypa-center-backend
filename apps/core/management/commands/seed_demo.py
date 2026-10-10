from __future__ import annotations

import datetime
import importlib
from typing import TypedDict

from django.conf import settings
from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import transaction
from django.utils import timezone

from apps.billing.models import (
    Attendance,
    AttendanceCommentTag,
    AttendanceStatus,
    Enrollment,
    EnrollmentType,
    EnrollmentStatus,
    Subscription,
    SubscriptionPlan,
    SubscriptionSlot,
    SubscriptionStatus,
    Transaction,
    TransactionStatus,
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
# ПОЧЕМУ пароль у педагогов: админка пускает только по паролю, а журнал
# учителя живёт в админке — без него роль «Учителя» на демо не проверить
DEMO_TEACHER_PASSWORD = "teacher123"  # noqa: S105 — демо-стенд, не секрет
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


class _ChildSeed(TypedDict):
    full_name: str
    age: int
    grade: str
    groups: list[_GroupKey]
    trial: _GroupKey | None


class _FamilySeed(TypedDict):
    email: str
    full_name: str
    phone: str
    referral: ReferralSource
    children: list[_ChildSeed]


def _child(
    full_name: str,
    age: int,
    grade: str,
    groups: list[_GroupKey],
    trial: _GroupKey | None = None,
) -> _ChildSeed:
    return {
        "full_name": full_name,
        "age": age,
        "grade": grade,
        "groups": groups,
        "trial": trial,
    }


def _thinking(day: int, hour: int) -> _GroupKey:
    return (_THINKING_SLUG, day, datetime.time(hour, 0))


_ENGLISH_GRADE_0: _GroupKey = (_ENGLISH_SLUG, _SATURDAY, datetime.time(13, 30))
_ENGLISH_GRADE_1: _GroupKey = (_ENGLISH_SLUG, 0, datetime.time(13, 0))
_ENGLISH_GRADE_2: _GroupKey = (_ENGLISH_SLUG, 1, datetime.time(17, 0))
_ENGLISH_GRADES_3_5_TUE: _GroupKey = (_ENGLISH_SLUG, 1, datetime.time(16, 0))
_ENGLISH_GRADES_3_5_FRI: _GroupKey = (_ENGLISH_SLUG, 4, datetime.time(16, 0))
_ENGLISH_HOMEWORK: _GroupKey = (_ENGLISH_SLUG, 4, datetime.time(17, 0))
_THINKING_SATURDAY: _GroupKey = (_THINKING_SLUG, _SATURDAY, datetime.time(11, 0))

# ПОЧЕМУ выдуманные семьи: это клиенты-заглушки для показа админки, а не
# данные центра. Группы ребёнка подобраны по классу и без накладок по времени;
# число групп ребёнка = число слотов его тарифа
_FAMILIES: list[_FamilySeed] = [
    {
        "email": DEMO_PARENT_EMAIL,
        "full_name": "Синяева Елена",
        "phone": "+79991234567",
        "referral": ReferralSource.FRIENDS,
        "children": [
            _child("Синяев Мирон", 8, "2", [_thinking(0, 16), _ENGLISH_GRADE_2]),
            _child("Имажап Очур-Бады", 6, "", [_ENGLISH_GRADE_0]),
        ],
    },
    {
        "email": "a.kuznetsova@demo.ru",
        "full_name": "Кузнецова Анна",
        "phone": "+79992000001",
        "referral": ReferralSource.SOCIAL,
        "children": [
            _child(
                "Кузнецов Артём", 10, "4", [_thinking(2, 17), _ENGLISH_GRADES_3_5_FRI]
            ),
            _child("Кузнецова Полина", 7, "1", [_ENGLISH_GRADE_1]),
        ],
    },
    {
        "email": "s.petrov@demo.ru",
        "full_name": "Петров Сергей",
        "phone": "+79992000002",
        "referral": ReferralSource.MAPS,
        "children": [
            _child(
                "Петрова Алиса", 9, "3", [_thinking(3, 16), _ENGLISH_GRADES_3_5_TUE]
            ),
        ],
    },
    {
        "email": "o.smirnova@demo.ru",
        "full_name": "Смирнова Ольга",
        "phone": "+79992000003",
        "referral": ReferralSource.SEARCH,
        "children": [
            _child("Смирнов Глеб", 12, "6", [_thinking(0, 17), _thinking(3, 17)]),
        ],
    },
    {
        "email": "m.ivanova@demo.ru",
        "full_name": "Иванова Марина",
        "phone": "+79992000004",
        "referral": ReferralSource.SCHOOL,
        "children": [
            _child("Иванов Лев", 8, "2", [_ENGLISH_GRADE_2, _THINKING_SATURDAY]),
            _child("Иванова Ева", 6, "0", [_ENGLISH_GRADE_0]),
        ],
    },
    {
        "email": "a.fedorov@demo.ru",
        "full_name": "Фёдоров Алексей",
        "phone": "+79992000005",
        "referral": ReferralSource.SIGN,
        "children": [
            _child(
                "Фёдорова Ксения",
                11,
                "5",
                [_thinking(2, 15), _ENGLISH_GRADES_3_5_FRI, _ENGLISH_HOMEWORK],
            ),
        ],
    },
    {
        "email": "d.morozova@demo.ru",
        "full_name": "Морозова Дарья",
        "phone": "+79992000006",
        "referral": ReferralSource.FRIENDS,
        "children": [_child("Морозов Тимур", 7, "1", [_thinking(1, 15)])],
    },
    {
        "email": "t.volkova@demo.ru",
        "full_name": "Волкова Татьяна",
        "phone": "+79992000007",
        "referral": ReferralSource.SOCIAL,
        "children": [
            _child("Волков Даниил", 14, "8", [_thinking(4, 17), _thinking(0, 16)]),
            _child("Волкова Мила", 9, "3", [_ENGLISH_GRADES_3_5_FRI]),
        ],
    },
    {
        "email": "e.novikova@demo.ru",
        "full_name": "Новикова Екатерина",
        "phone": "+79992000008",
        "referral": ReferralSource.MAPS,
        "children": [
            # Только пришли: пробное на Кружок Мышления и ребёнок без записей
            _child("Новиков Матвей", 10, "4", [], trial=_thinking(2, 16)),
            _child("Новикова Вера", 8, "2", []),
        ],
    },
    {
        "email": "n.sokolova@demo.ru",
        "full_name": "Соколова Наталья",
        "phone": "+79992000009",
        "referral": ReferralSource.SEARCH,
        "children": [
            _child("Соколов Егор", 9, "3", [_thinking(0, 15), _ENGLISH_GRADES_3_5_TUE]),
            _child("Соколова Варя", 7, "1", [_ENGLISH_GRADE_1]),
        ],
    },
    {
        "email": "i.lebedev@demo.ru",
        "full_name": "Лебедев Игорь",
        "phone": "+79992000010",
        "referral": ReferralSource.FRIENDS,
        "children": [_child("Лебедева София", 13, "7", [_thinking(2, 17)])],
    },
    {
        "email": "y.kozlova@demo.ru",
        "full_name": "Козлова Юлия",
        "phone": "+79992000011",
        "referral": ReferralSource.SOCIAL,
        "children": [
            _child("Козлов Макар", 8, "2", [_ENGLISH_GRADE_2, _thinking(3, 15)]),
        ],
    },
    {
        "email": "r.orlova@demo.ru",
        "full_name": "Орлова Регина",
        "phone": "+79992000012",
        "referral": ReferralSource.SCHOOL,
        "children": [
            _child(
                "Орлов Никита", 11, "5", [_ENGLISH_GRADES_3_5_FRI, _thinking(4, 15)]
            ),
            _child("Орлова Даша", 6, "0", [_ENGLISH_GRADE_0]),
        ],
    },
    {
        "email": "v.popov@demo.ru",
        "full_name": "Попов Виктор",
        "phone": "+79992000013",
        "referral": ReferralSource.MAPS,
        "children": [
            _child(
                "Попов Кирилл",
                15,
                "9",
                [_thinking(1, 17), _thinking(3, 17), _THINKING_SATURDAY],
            ),
        ],
    },
    {
        "email": "a.zaitseva@demo.ru",
        "full_name": "Зайцева Алёна",
        "phone": "+79992000014",
        "referral": ReferralSource.SIGN,
        "children": [
            _child("Зайцева Ника", 10, "4", [_ENGLISH_GRADES_3_5_TUE]),
            # Пробное на английский для младшего брата
            _child("Зайцев Федя", 8, "2", [], trial=_ENGLISH_GRADE_2),
        ],
    },
    {
        "email": "k.belova@demo.ru",
        "full_name": "Белова Кристина",
        "phone": "+79992000015",
        "referral": ReferralSource.FRIENDS,
        "children": [_child("Белов Артемий", 12, "6", [_thinking(2, 16)])],
    },
]

# ПОЧЕМУ разные дни: покупки разнесены по последнему месяцу — график
# платежей и журнал занятий выглядят как живые. Дни берутся по кругу
_PURCHASE_DAYS_AGO: tuple[int, ...] = (
    27, 9, 22, 15, 4, 18, 12, 25, 7, 20, 14, 29, 11,
    24, 6, 17, 21, 8, 26, 13, 19, 10, 23, 16, 28,
)  # fmt: skip
_TRIAL_DAYS_AGO: tuple[int, ...] = (2, 5, 1)
_TOKENS_PER_SLOT = 4
_THINKING_TOPICS: tuple[str, ...] = (
    "Логические задачи на взвешивание",
    "Задачи на переливание",
    "Графы: кто с кем знаком",
    "Разбор заданий конкурса «Кенгуру»",
)
_ENGLISH_TOPICS: tuple[str, ...] = (
    "Present Simple: рассказываем о себе",
    "Семья и друзья: новая лексика",
    "Читаем короткий рассказ",
    "Can и can not: что я умею",
)
_ATTENDANCE_COMMENTS: tuple[tuple[str, AttendanceCommentTag], ...] = (
    ("Решал задачи с интересом, помогал соседу.", AttendanceCommentTag.POSITIVE),
    ("Хорошо работал, к концу занятия устал.", AttendanceCommentTag.NEUTRAL),
    ("Не сделал домашнее задание, обсудили с ребёнком.", AttendanceCommentTag.NEGATIVE),
    ("Отлично отвечал устно, словарь растёт.", AttendanceCommentTag.POSITIVE),
    ("Отвлекался, пересадили ближе к доске.", AttendanceCommentTag.NEGATIVE),
    ("Первым решил задачу со звёздочкой.", AttendanceCommentTag.POSITIVE),
)


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
        parent = self._seed_families(groups)
        if not options.get("no_admin"):
            self._seed_admin()

        self.stdout.write(self.style.SUCCESS("Демо-данные загружены."))
        self.stdout.write(f"Админка: {DEMO_ADMIN_EMAIL} / {DEMO_ADMIN_PASSWORD}")
        teacher_emails = ", ".join(seed["email"] for seed in _TEACHERS)
        self.stdout.write(
            f"Педагоги (журнал): {teacher_emails} / {DEMO_TEACHER_PASSWORD}"
        )
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
        # ПОЧЕМУ: права группе выдаёт миграция journal/0002, но manage.py flush
        # их стирает — без этого учитель видит «недостаточно полномочий»
        for app_label, model, actions in _teacher_group_permissions():
            group.permissions.add(
                *Permission.objects.filter(
                    content_type__app_label=app_label,
                    codename__in=[f"{action}_{model}" for action in actions],
                )
            )
        profiles: dict[str, TeacherProfile] = {}
        for seed in _TEACHERS:
            user = Parent.objects.filter(email=seed["email"]).first()
            if user is None:
                user = Parent.objects.create_user(
                    email=seed["email"], full_name=seed["full_name"], is_staff=True
                )
            user.groups.add(group)
            if not user.has_usable_password():
                user.set_password(DEMO_TEACHER_PASSWORD)
                user.save(update_fields=["password"])
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

    def _seed_families(self, groups: dict[_GroupKey, Schedule]) -> Parent:
        today = timezone.localdate()
        demo_parent: Parent | None = None
        purchases = 0
        trials = 0
        for family in _FAMILIES:
            parent = self._seed_parent(family)
            if demo_parent is None:
                demo_parent = parent
            # ПОЧЕМУ проверка по родителю: дети и абонементы создаются один раз,
            # повторный прогон не плодит покупки и отметки
            has_purchases = Enrollment.objects.filter(student__parent=parent).exists()
            for child in family["children"]:
                student, _ = Student.objects.get_or_create(
                    parent=parent,
                    full_name=child["full_name"],
                    defaults={
                        "dob": _birth_date(today, child["age"]),
                        "school_grade": child["grade"],
                    },
                )
                if has_purchases:
                    continue
                if child["groups"]:
                    days_ago = _PURCHASE_DAYS_AGO[purchases % len(_PURCHASE_DAYS_AGO)]
                    purchases += 1
                    self._seed_subscription(
                        parent,
                        student,
                        [groups[key] for key in child["groups"]],
                        days_ago,
                    )
                if child["trial"] is not None:
                    days_ago = _TRIAL_DAYS_AGO[trials % len(_TRIAL_DAYS_AGO)]
                    trials += 1
                    self._seed_trial(parent, student, groups[child["trial"]], days_ago)
        if demo_parent is None:
            raise CommandError("В _FAMILIES нет ни одной семьи.")
        return demo_parent

    def _seed_parent(self, family: _FamilySeed) -> Parent:
        parent = Parent.objects.filter(email=family["email"]).first()
        if parent is not None:
            return parent
        parent = Parent.objects.create_user(
            email=family["email"], full_name=family["full_name"]
        )
        parent.phone = family["phone"]
        # Анкета заполнена — иначе демо-ЛК отвечает 403 PROFILE_INCOMPLETE
        parent.referral_source = family["referral"]
        # Демо-данные: согласие проставлено напрямую, без журнала — на
        # проде оно появляется только из анкеты (apps.users.consent)
        parent.pd_consent_at = timezone.now()
        parent.save(update_fields=["phone", "referral_source", "pd_consent_at"])
        return parent

    def _seed_subscription(
        self,
        parent: Parent,
        student: Student,
        schedules: list[Schedule],
        days_ago: int,
    ) -> None:
        bought_at = timezone.now() - datetime.timedelta(days=days_ago)
        start_date = timezone.localtime(bought_at).date()
        plan = SubscriptionPlan.objects.get(
            slots_count=len(schedules), is_unlimited=False
        )
        subscription = Subscription.objects.create(
            parent=parent,
            plan=plan,
            status=SubscriptionStatus.ACTIVE,
            purchase_price=plan.price,
            base_session_price=plan.base_session_price,
            start_date=start_date,
            expires_at=bought_at + datetime.timedelta(days=30),
        )
        tx = Transaction.objects.create(
            parent=parent,
            subscription=subscription,
            amount=plan.price,
            received_amount=plan.price,
            status=TransactionStatus.SUCCEEDED,
            selected_slot_ids=[schedule.pk for schedule in schedules],
        )
        # ПОЧЕМУ update: created_at — auto_now_add, а на демо покупки должны
        # быть разнесены по месяцу, иначе график платежей — один столбик
        Subscription.objects.filter(pk=subscription.pk).update(created_at=bought_at)
        Transaction.objects.filter(pk=tx.pk).update(created_at=bought_at)
        for schedule in schedules:
            enrollment = Enrollment.objects.create(
                student=student,
                subscription=subscription,
                schedule=schedule,
                status=EnrollmentStatus.ENROLLED,
            )
            Enrollment.objects.filter(pk=enrollment.pk).update(created_at=bought_at)
            debited = self._seed_lessons(enrollment, schedule, start_date)
            SubscriptionSlot.objects.create(
                subscription=subscription,
                slot_id=schedule.pk,
                granted_tokens=_TOKENS_PER_SLOT,
                remaining_tokens=_TOKENS_PER_SLOT - debited,
            )

    def _seed_lessons(
        self, enrollment: Enrollment, schedule: Schedule, since: datetime.date
    ) -> int:
        topics = (
            _THINKING_TOPICS
            if schedule.activity.slug == _THINKING_SLUG
            else _ENGLISH_TOPICS
        )
        dates = _weekly_dates(
            since,
            timezone.localdate(),
            schedule.time_slot.day_of_week,
            limit=_TOKENS_PER_SLOT,
        )
        debited = 0
        for week, lesson_date in enumerate(dates):
            Lesson.objects.get_or_create(
                schedule=schedule,
                date=lesson_date,
                defaults={"topic": topics[lesson_date.toordinal() // 7 % len(topics)]},
            )
            # ПОЧЕМУ каждое пятое — пропуск: в журнале видны оба исхода,
            # а у пропуска по уважительной фишка не списывается
            attended = (enrollment.pk + week) % 5 != 0
            comment, tag = _ATTENDANCE_COMMENTS[
                (enrollment.pk + week) % len(_ATTENDANCE_COMMENTS)
            ]
            Attendance.objects.create(
                enrollment=enrollment,
                date=lesson_date,
                status=AttendanceStatus.ATTENDED
                if attended
                else AttendanceStatus.ABSENT_OK,
                token_debited=attended,
                comment=comment if attended else "",
                comment_tag=tag if attended else AttendanceCommentTag.NEUTRAL,
            )
            debited += attended
        return debited

    def _seed_trial(
        self,
        parent: Parent,
        student: Student,
        schedule: Schedule,
        days_ago: int,
    ) -> None:
        activity = schedule.activity
        enrollment = Enrollment.objects.create(
            student=student,
            schedule=schedule,
            type=EnrollmentType.TRIAL,
            trial_date=_next_weekday_after(
                timezone.localdate(), schedule.time_slot.day_of_week
            ),
            activity=activity,
            status=EnrollmentStatus.ENROLLED,
        )
        tx = Transaction.objects.create(
            parent=parent,
            enrollment=enrollment,
            amount=activity.price,
            received_amount=activity.price,
            status=TransactionStatus.SUCCEEDED,
            selected_slot_ids=[schedule.pk],
        )
        Transaction.objects.filter(pk=tx.pk).update(
            created_at=timezone.now() - datetime.timedelta(days=days_ago)
        )

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


def _birth_date(today: datetime.date, age: int) -> datetime.date:
    # ПОЧЕМУ от сегодняшней даты: фиксированная дата рождения со временем
    # вывела бы ребёнка из возраста его группы
    return datetime.date(today.year - age, today.month, 1) - datetime.timedelta(days=60)


def _weekly_dates(
    since: datetime.date, today: datetime.date, weekday: int, *, limit: int
) -> list[datetime.date]:
    """Прошедшие даты занятий группы с `since` до вчера, не больше `limit` последних."""
    day = today - datetime.timedelta(days=1)
    dates: list[datetime.date] = []
    while day >= since and len(dates) < limit:
        if day.weekday() == weekday:
            dates.append(day)
        day -= datetime.timedelta(days=1)
    return sorted(dates)


def _next_weekday_after(today: datetime.date, weekday: int) -> datetime.date:
    day = today + datetime.timedelta(days=1)
    while day.weekday() != weekday:
        day += datetime.timedelta(days=1)
    return day


def _teacher_group_permissions() -> list[tuple[str, str, tuple[str, ...]]]:
    # ПОЧЕМУ импорт из миграции: список прав группы живёт в одном месте;
    # имя модуля начинается с цифры, поэтому обычный import не подходит
    migration = importlib.import_module("apps.journal.migrations.0002_teachers_group")
    permissions: list[tuple[str, str, tuple[str, ...]]] = migration.GROUP_PERMISSIONS
    return permissions
