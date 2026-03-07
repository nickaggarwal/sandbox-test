from django.urls import path

from . import views

app_name = 'scheduling'

urlpatterns = [
    path(
        'event-types/',
        views.EventTypeListCreateView.as_view(),
        name='event-type-list',
    ),
    path(
        'event-types/<slug:slug>/',
        views.EventTypeDetailView.as_view(),
        name='event-type-detail',
    ),
    path(
        'availability/',
        views.AvailabilityListCreateView.as_view(),
        name='availability-list',
    ),
    path(
        'availability-overrides/',
        views.AvailabilityOverrideListCreateView.as_view(),
        name='availability-override-list',
    ),
    path(
        'available-slots/',
        views.AvailableSlotsView.as_view(),
        name='available-slots',
    ),
    path(
        'bookings/',
        views.BookingCreateView.as_view(),
        name='booking-create',
    ),
    path(
        'bookings/<uuid:pk>/',
        views.BookingDetailView.as_view(),
        name='booking-detail',
    ),
    path(
        'bookings/<uuid:pk>/cancel/',
        views.BookingCancelView.as_view(),
        name='booking-cancel',
    ),
    path(
        'my-bookings/',
        views.HostBookingsView.as_view(),
        name='host-bookings',
    ),
]
