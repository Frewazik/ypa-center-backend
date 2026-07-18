# ПОЧЕМУ: инвалидация живёт в BFF-слое, владеющем кэшем, и висит на
# сигналах моделей — правка из админки, shell или сервиса видна на витрине
# мгновенно, TTL остаётся страховкой. QuerySet.update() сигналы обходит —
# такие правки дождутся TTL.

from __future__ import annotations

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver
from django.urls import reverse

from apps.billing.models import SubscriptionPlan
from apps.catalog.models import Activity
from apps.content.models import GalleryImage
from apps.core.caching import invalidate_payload_cache
from apps.events.models import Event
from apps.schedule.models import Schedule
from apps.users.models import TeacherProfile


def _invalidate_activity_pages(activity_id: int) -> None:
    invalidate_payload_cache("activities", reverse("public_api:activities-list"))
    invalidate_payload_cache(
        "activities_popular", reverse("public_api:activities-popular")
    )
    invalidate_payload_cache(
        "activity_detail",
        reverse("public_api:activity-detail", kwargs={"pk": activity_id}),
    )


@receiver(post_save, sender=Activity)
@receiver(post_delete, sender=Activity)
def on_activity_change(
    sender: type[Activity], instance: Activity, **kwargs: object
) -> None:
    _invalidate_activity_pages(instance.pk)


@receiver(post_save, sender=Schedule)
@receiver(post_delete, sender=Schedule)
def on_schedule_change(
    sender: type[Schedule], instance: Schedule, **kwargs: object
) -> None:
    _invalidate_activity_pages(instance.activity_id)
    invalidate_payload_cache("teachers", reverse("public_api:teachers-list"))


@receiver(post_save, sender=TeacherProfile)
@receiver(post_delete, sender=TeacherProfile)
def on_teacher_change(
    sender: type[TeacherProfile], instance: TeacherProfile, **kwargs: object
) -> None:
    invalidate_payload_cache("teachers", reverse("public_api:teachers-list"))
    invalidate_payload_cache("activities", reverse("public_api:activities-list"))


@receiver(post_save, sender=GalleryImage)
@receiver(post_delete, sender=GalleryImage)
def on_gallery_change(
    sender: type[GalleryImage], instance: GalleryImage, **kwargs: object
) -> None:
    invalidate_payload_cache("gallery", reverse("public_api:gallery-list"))


@receiver(post_save, sender=Event)
@receiver(post_delete, sender=Event)
def on_event_change(sender: type[Event], instance: Event, **kwargs: object) -> None:
    invalidate_payload_cache("events", reverse("public_api:events-list"))


@receiver(post_save, sender=SubscriptionPlan)
@receiver(post_delete, sender=SubscriptionPlan)
def on_plan_change(
    sender: type[SubscriptionPlan], instance: SubscriptionPlan, **kwargs: object
) -> None:
    invalidate_payload_cache("plans", reverse("public_api:plans-list"))
