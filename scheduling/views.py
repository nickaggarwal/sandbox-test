from datetime import timedelta

from django.contrib.auth.models import User
from rest_framework import generics, status, views
from rest_framework.response import Response

from .engine import SchedulingEngine
from .models import Availability, AvailabilityOverride, Booking, EventType
from .serializers import (
    AvailabilitySerializer,
    AvailabilityOverrideSerializer,
    AvailableSlotsQuerySerializer,
    BookingCreateSerializer,
    BookingSerializer,
    EventTypeSerializer,
)


class EventTypeListCreateView(generics.ListCreateAPIView):
    serializer_class = EventTypeSerializer

    def get_queryset(self):
        return EventType.objects.filter(is_active=True)

    def perform_create(self, serializer):
        serializer.save(host=self.request.user)


class EventTypeDetailView(generics.RetrieveUpdateDestroyAPIView):
    serializer_class = EventTypeSerializer
    queryset = EventType.objects.all()
    lookup_field = 'slug'


class AvailabilityListCreateView(generics.ListCreateAPIView):
    serializer_class = AvailabilitySerializer

    def get_queryset(self):
        return Availability.objects.filter(host=self.request.user)

    def perform_create(self, serializer):
        serializer.save(host=self.request.user)


class AvailabilityOverrideListCreateView(generics.ListCreateAPIView):
    serializer_class = AvailabilityOverrideSerializer

    def get_queryset(self):
        return AvailabilityOverride.objects.filter(host=self.request.user)

    def perform_create(self, serializer):
        serializer.save(host=self.request.user)


class AvailableSlotsView(views.APIView):
    """Public endpoint: get available slots for an event type on a date."""

    def get(self, request):
        serializer = AvailableSlotsQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)

        try:
            event_type = EventType.objects.get(
                id=serializer.validated_data['event_type_id'],
                is_active=True,
            )
        except EventType.DoesNotExist:
            return Response(
                {'error': 'Event type not found'},
                status=status.HTTP_404_NOT_FOUND,
            )

        slots = SchedulingEngine.get_available_slots(
            host_id=event_type.host_id,
            event_type=event_type,
            target_date=serializer.validated_data['date'],
            guest_timezone=serializer.validated_data.get(
                'guest_timezone', 'UTC'
            ),
        )

        return Response({
            'event_type': EventTypeSerializer(event_type).data,
            'date': serializer.validated_data['date'].isoformat(),
            'slots': slots,
        })


class BookingCreateView(views.APIView):
    """Create a new booking."""

    def post(self, request):
        serializer = BookingCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            event_type = EventType.objects.get(
                id=serializer.validated_data['event_type_id'],
                is_active=True,
            )
        except EventType.DoesNotExist:
            return Response(
                {'error': 'Event type not found'},
                status=status.HTTP_404_NOT_FOUND,
            )

        try:
            booking = SchedulingEngine.create_booking(
                event_type=event_type,
                start_time_utc=serializer.validated_data['start_time_utc'],
                guest_name=serializer.validated_data['guest_name'],
                guest_email=serializer.validated_data['guest_email'],
                guest_timezone=serializer.validated_data.get(
                    'guest_timezone', 'UTC'
                ),
                notes=serializer.validated_data.get('notes', ''),
            )
        except ValueError as e:
            return Response(
                {'error': str(e)},
                status=status.HTTP_409_CONFLICT,
            )

        return Response(
            BookingSerializer(booking).data,
            status=status.HTTP_201_CREATED,
        )


class BookingDetailView(generics.RetrieveAPIView):
    serializer_class = BookingSerializer
    queryset = Booking.objects.all()


class BookingCancelView(views.APIView):
    """Cancel an existing booking."""

    def post(self, request, pk):
        try:
            booking = Booking.objects.get(pk=pk, status='confirmed')
        except Booking.DoesNotExist:
            return Response(
                {'error': 'Booking not found or already cancelled'},
                status=status.HTTP_404_NOT_FOUND,
            )

        booking.cancel()
        return Response(BookingSerializer(booking).data)


class HostBookingsView(generics.ListAPIView):
    """List bookings for the authenticated host."""
    serializer_class = BookingSerializer

    def get_queryset(self):
        return Booking.objects.filter(
            host=self.request.user, status='confirmed'
        )
