from __future__ import annotations

from apps.billing.ports import EventBookingContacts, EventBookingSummary
from apps.events import services
from apps.events.models import EventRegistration


class DjangoEventBookingPort:
    # ПОЧЕМУ: billing меняет брони событий только через этот адаптер —
    # счётчик мест и порядок локов «событие → бронь» остаются в events

    def confirm_paid(self, registration_id: int) -> bool:
        return services.confirm_paid_registration(registration_id)

    def release_unpaid(self, registration_id: int, *, notify: bool) -> bool:
        return services.release_unpaid_registration(registration_id, notify=notify)

    def cancel_paid(self, registration_id: int) -> bool:
        return services.cancel_paid_registration(registration_id)

    def get_contacts(self, registration_id: int) -> EventBookingContacts:
        registration = EventRegistration.objects.get(pk=registration_id)
        return EventBookingContacts(
            parent_name=registration.parent_name,
            email=registration.email,
            phone=str(registration.phone),
        )

    def get_summary(self, registration_id: int) -> EventBookingSummary:
        registration = EventRegistration.objects.select_related("event").get(
            pk=registration_id
        )
        return EventBookingSummary(
            event_id=registration.event_id,
            title=registration.event.title,
            starts_at=registration.event.start_datetime,
            attendees_count=registration.attendees_count,
        )
