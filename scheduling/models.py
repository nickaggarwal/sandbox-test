import uuid
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone

import pytz


class EventType(models.Model):
    """A type of event a host can offer (e.g., '30-min meeting')."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    host = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='event_types',
    )
    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=200)
    description = models.TextField(blank=True, default='')
    duration_minutes = models.PositiveIntegerField(default=30)
    buffer_before_minutes = models.PositiveIntegerField(default=0)
    buffer_after_minutes = models.PositiveIntegerField(default=0)
    max_bookings_per_day = models.PositiveIntegerField(
        default=0, help_text='0 = unlimited'
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('host', 'slug')
        ordering = ['name']

    def __str__(self):
        return f"{self.name} ({self.duration_minutes}min)"

    @property
    def total_block_minutes(self):
        return (
            self.buffer_before_minutes
            + self.duration_minutes
            + self.buffer_after_minutes
        )


class Availability(models.Model):
    """Recurring weekly availability window for a host."""
    DAYS_OF_WEEK = [
        (0, 'Monday'),
        (1, 'Tuesday'),
        (2, 'Wednesday'),
        (3, 'Thursday'),
        (4, 'Friday'),
        (5, 'Saturday'),
        (6, 'Sunday'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    host = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='availabilities',
    )
    day_of_week = models.IntegerField(choices=DAYS_OF_WEEK)
    start_time = models.TimeField()
    end_time = models.TimeField()
    timezone = models.CharField(max_length=50, default='UTC')

    class Meta:
        verbose_name_plural = 'availabilities'
        ordering = ['day_of_week', 'start_time']

    def __str__(self):
        day = self.get_day_of_week_display()
        return f"{day} {self.start_time}-{self.end_time} ({self.timezone})"


class AvailabilityOverride(models.Model):
    """Date-specific availability override (e.g., day off, extended hours)."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    host = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='availability_overrides',
    )
    date = models.DateField()
    is_available = models.BooleanField(default=False)
    start_time = models.TimeField(null=True, blank=True)
    end_time = models.TimeField(null=True, blank=True)
    timezone = models.CharField(max_length=50, default='UTC')

    class Meta:
        unique_together = ('host', 'date')

    def __str__(self):
        status = 'Available' if self.is_available else 'Unavailable'
        return f"{self.date} - {status}"


class Booking(models.Model):
    """A confirmed booking/appointment."""
    STATUS_CHOICES = [
        ('confirmed', 'Confirmed'),
        ('cancelled', 'Cancelled'),
        ('completed', 'Completed'),
        ('no_show', 'No Show'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    event_type = models.ForeignKey(
        EventType,
        on_delete=models.CASCADE,
        related_name='bookings',
    )
    host = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='hosted_bookings',
    )
    guest_name = models.CharField(max_length=200)
    guest_email = models.EmailField()
    guest_timezone = models.CharField(max_length=50, default='UTC')
    start_time = models.DateTimeField()
    end_time = models.DateTimeField()
    buffer_start = models.DateTimeField(
        help_text='Start time including pre-buffer'
    )
    buffer_end = models.DateTimeField(
        help_text='End time including post-buffer'
    )
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default='confirmed'
    )
    notes = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['start_time']

    def __str__(self):
        return f"{self.guest_name} - {self.event_type.name} @ {self.start_time}"

    def save(self, *args, **kwargs):
        if not self.end_time:
            self.end_time = self.start_time + timedelta(
                minutes=self.event_type.duration_minutes
            )
        if not self.buffer_start:
            self.buffer_start = self.start_time - timedelta(
                minutes=self.event_type.buffer_before_minutes
            )
        if not self.buffer_end:
            self.buffer_end = self.end_time + timedelta(
                minutes=self.event_type.buffer_after_minutes
            )
        super().save(*args, **kwargs)

    def cancel(self):
        self.status = 'cancelled'
        self.cancelled_at = timezone.now()
        self.save()
