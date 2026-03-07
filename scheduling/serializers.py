from rest_framework import serializers

from .models import Availability, AvailabilityOverride, Booking, EventType


class EventTypeSerializer(serializers.ModelSerializer):
    total_block_minutes = serializers.ReadOnlyField()

    class Meta:
        model = EventType
        fields = [
            'id', 'name', 'slug', 'description', 'duration_minutes',
            'buffer_before_minutes', 'buffer_after_minutes',
            'max_bookings_per_day', 'is_active', 'total_block_minutes',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class AvailabilitySerializer(serializers.ModelSerializer):
    class Meta:
        model = Availability
        fields = [
            'id', 'day_of_week', 'start_time', 'end_time', 'timezone',
        ]
        read_only_fields = ['id']


class AvailabilityOverrideSerializer(serializers.ModelSerializer):
    class Meta:
        model = AvailabilityOverride
        fields = [
            'id', 'date', 'is_available', 'start_time', 'end_time',
            'timezone',
        ]
        read_only_fields = ['id']


class BookingCreateSerializer(serializers.Serializer):
    event_type_id = serializers.UUIDField()
    start_time_utc = serializers.DateTimeField()
    guest_name = serializers.CharField(max_length=200)
    guest_email = serializers.EmailField()
    guest_timezone = serializers.CharField(max_length=50, default='UTC')
    notes = serializers.CharField(required=False, default='')


class BookingSerializer(serializers.ModelSerializer):
    event_type_name = serializers.CharField(
        source='event_type.name', read_only=True
    )

    class Meta:
        model = Booking
        fields = [
            'id', 'event_type', 'event_type_name', 'guest_name',
            'guest_email', 'guest_timezone', 'start_time', 'end_time',
            'buffer_start', 'buffer_end', 'status', 'notes',
            'created_at', 'cancelled_at',
        ]
        read_only_fields = [
            'id', 'buffer_start', 'buffer_end', 'created_at',
            'cancelled_at',
        ]


class AvailableSlotsQuerySerializer(serializers.Serializer):
    event_type_id = serializers.UUIDField()
    date = serializers.DateField()
    guest_timezone = serializers.CharField(max_length=50, default='UTC')
