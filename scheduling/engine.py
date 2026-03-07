"""
Scheduling engine: conflict detection, buffer enforcement,
timezone handling, and availability masking.
"""
from datetime import datetime, timedelta, time, date
from typing import List, Optional, Tuple

from django.db.models import Q
from django.utils import timezone

import pytz

from .models import Availability, AvailabilityOverride, Booking, EventType


class SchedulingEngine:
    """Core scheduling logic for the Calendly-like app."""

    # ── Conflict Detection ──────────────────────────────────────────

    @staticmethod
    def has_conflict(
        host_id: int,
        start: datetime,
        end: datetime,
        buffer_before: int = 0,
        buffer_after: int = 0,
        exclude_booking_id=None,
    ) -> bool:
        """Check if a proposed time slot conflicts with existing bookings.

        Considers buffer times for both existing and proposed bookings.
        Returns True if there IS a conflict.
        """
        proposed_buffer_start = start - timedelta(minutes=buffer_before)
        proposed_buffer_end = end + timedelta(minutes=buffer_after)

        conflicts = Booking.objects.filter(
            host_id=host_id,
            status='confirmed',
        ).exclude(
            id=exclude_booking_id,
        ).filter(
            # Overlap: existing buffer_start < proposed buffer_end
            # AND existing buffer_end > proposed buffer_start
            buffer_start__lt=proposed_buffer_end,
            buffer_end__gt=proposed_buffer_start,
        )
        return conflicts.exists()

    @staticmethod
    def get_conflicts(
        host_id: int,
        start: datetime,
        end: datetime,
    ) -> List[Booking]:
        """Return all bookings that conflict with the proposed window."""
        return list(
            Booking.objects.filter(
                host_id=host_id,
                status='confirmed',
                buffer_start__lt=end,
                buffer_end__gt=start,
            ).order_by('start_time')
        )

    # ── Buffer Time Enforcement ─────────────────────────────────────

    @staticmethod
    def enforce_buffer(
        event_type: EventType,
        start: datetime,
    ) -> Tuple[datetime, datetime, datetime, datetime]:
        """Calculate meeting and buffer boundaries.

        Returns: (buffer_start, meeting_start, meeting_end, buffer_end)
        """
        buffer_start = start - timedelta(
            minutes=event_type.buffer_before_minutes
        )
        meeting_start = start
        meeting_end = start + timedelta(minutes=event_type.duration_minutes)
        buffer_end = meeting_end + timedelta(
            minutes=event_type.buffer_after_minutes
        )
        return buffer_start, meeting_start, meeting_end, buffer_end

    # ── Timezone Handling ───────────────────────────────────────────

    @staticmethod
    def convert_timezone(
        dt: datetime,
        source_tz: str,
        target_tz: str,
    ) -> datetime:
        """Convert a datetime from source timezone to target timezone.

        Handles DST transitions correctly.
        """
        src = pytz.timezone(source_tz)
        tgt = pytz.timezone(target_tz)

        if dt.tzinfo is None:
            dt = src.localize(dt)
        else:
            dt = dt.astimezone(src)

        return dt.astimezone(tgt)

    @staticmethod
    def normalize_to_utc(dt: datetime, source_tz: str) -> datetime:
        """Convert any datetime to UTC for storage."""
        src = pytz.timezone(source_tz)
        if dt.tzinfo is None:
            dt = src.localize(dt)
        return dt.astimezone(pytz.UTC)

    @staticmethod
    def display_in_timezone(dt: datetime, target_tz: str) -> datetime:
        """Convert a UTC datetime to a display timezone."""
        tgt = pytz.timezone(target_tz)
        if dt.tzinfo is None:
            dt = pytz.UTC.localize(dt)
        return dt.astimezone(tgt)

    # ── Availability Masking ────────────────────────────────────────

    @classmethod
    def get_available_slots(
        cls,
        host_id: int,
        event_type: EventType,
        target_date: date,
        guest_timezone: str = 'UTC',
        slot_interval_minutes: int = 15,
    ) -> List[dict]:
        """Get available time slots for a given date.

        Returns slots in the guest's timezone with private details masked.
        Only shows open/unavailable status, never the reason.
        """
        host_tz_name = cls._get_host_timezone(host_id, target_date)
        host_tz = pytz.timezone(host_tz_name)
        guest_tz = pytz.timezone(guest_timezone)

        # Check for date-specific override
        override = AvailabilityOverride.objects.filter(
            host_id=host_id,
            date=target_date,
        ).first()

        if override and not override.is_available:
            return []

        # Get availability windows for this day
        windows = cls._get_day_windows(
            host_id, target_date, override, host_tz
        )

        if not windows:
            return []

        # Generate candidate slots
        slots = []
        duration = timedelta(minutes=event_type.duration_minutes)
        interval = timedelta(minutes=slot_interval_minutes)

        for window_start, window_end in windows:
            current = window_start
            while current + duration <= window_end:
                buf_start, meet_start, meet_end, buf_end = cls.enforce_buffer(
                    event_type, current
                )

                # Check window boundaries with buffers
                if buf_start >= window_start and buf_end <= window_end:
                    # Check for conflicts
                    if not cls.has_conflict(
                        host_id,
                        meet_start,
                        meet_end,
                        event_type.buffer_before_minutes,
                        event_type.buffer_after_minutes,
                    ):
                        # Check max bookings per day
                        if cls._check_daily_limit(
                            host_id, event_type, target_date, host_tz
                        ):
                            # Convert to guest timezone for display
                            guest_start = meet_start.astimezone(guest_tz)
                            guest_end = meet_end.astimezone(guest_tz)

                            slots.append({
                                'start_time': guest_start.isoformat(),
                                'end_time': guest_end.isoformat(),
                                'start_time_utc': meet_start.astimezone(
                                    pytz.UTC
                                ).isoformat(),
                                'end_time_utc': meet_end.astimezone(
                                    pytz.UTC
                                ).isoformat(),
                                'available': True,
                            })

                current += interval

        return slots

    @classmethod
    def get_masked_availability(
        cls,
        host_id: int,
        event_type: EventType,
        start_date: date,
        end_date: date,
        guest_timezone: str = 'UTC',
    ) -> dict:
        """Get availability for a date range with private details masked.

        Guests see only open slots -- never why a slot is unavailable.
        """
        result = {}
        current_date = start_date

        while current_date <= end_date:
            day_slots = cls.get_available_slots(
                host_id, event_type, current_date, guest_timezone
            )
            result[current_date.isoformat()] = {
                'date': current_date.isoformat(),
                'slots': day_slots,
                'has_availability': len(day_slots) > 0,
            }
            current_date += timedelta(days=1)

        return result

    # ── Booking Creation ────────────────────────────────────────────

    @classmethod
    def create_booking(
        cls,
        event_type: EventType,
        start_time_utc: datetime,
        guest_name: str,
        guest_email: str,
        guest_timezone: str = 'UTC',
        notes: str = '',
    ) -> Booking:
        """Create a booking with full validation.

        Raises ValueError on conflict or invalid time.
        """
        if start_time_utc.tzinfo is None:
            start_time_utc = pytz.UTC.localize(start_time_utc)
        else:
            start_time_utc = start_time_utc.astimezone(pytz.UTC)

        buf_start, meet_start, meet_end, buf_end = cls.enforce_buffer(
            event_type, start_time_utc
        )

        # Validate: no conflicts
        if cls.has_conflict(
            event_type.host_id,
            meet_start,
            meet_end,
            event_type.buffer_before_minutes,
            event_type.buffer_after_minutes,
        ):
            raise ValueError('Time slot conflicts with an existing booking.')

        # Validate: within availability
        host_tz_name = cls._get_host_timezone(
            event_type.host_id,
            meet_start.date(),
        )
        host_tz = pytz.timezone(host_tz_name)
        local_start = meet_start.astimezone(host_tz)

        if not cls._is_within_availability(
            event_type.host_id, local_start, meet_end.astimezone(host_tz)
        ):
            raise ValueError('Time slot is outside host availability.')

        booking = Booking(
            event_type=event_type,
            host=event_type.host,
            guest_name=guest_name,
            guest_email=guest_email,
            guest_timezone=guest_timezone,
            start_time=meet_start,
            end_time=meet_end,
            buffer_start=buf_start,
            buffer_end=buf_end,
            notes=notes,
        )
        booking.save()
        return booking

    # ── Private Helpers ─────────────────────────────────────────────

    @staticmethod
    def _get_host_timezone(host_id: int, for_date: date) -> str:
        """Determine the host's timezone for a given date."""
        override = AvailabilityOverride.objects.filter(
            host_id=host_id, date=for_date
        ).first()
        if override and override.timezone:
            return override.timezone

        avail = Availability.objects.filter(host_id=host_id).first()
        if avail:
            return avail.timezone
        return 'UTC'

    @staticmethod
    def _get_day_windows(
        host_id: int,
        target_date: date,
        override: Optional[AvailabilityOverride],
        host_tz,
    ) -> List[Tuple[datetime, datetime]]:
        """Get availability windows for a specific date."""
        windows = []

        if override and override.is_available:
            if override.start_time and override.end_time:
                start_dt = host_tz.localize(
                    datetime.combine(target_date, override.start_time)
                )
                end_dt = host_tz.localize(
                    datetime.combine(target_date, override.end_time)
                )
                windows.append((start_dt, end_dt))
            return windows

        day_of_week = target_date.weekday()
        availabilities = Availability.objects.filter(
            host_id=host_id, day_of_week=day_of_week
        )

        for avail in availabilities:
            start_dt = host_tz.localize(
                datetime.combine(target_date, avail.start_time)
            )
            end_dt = host_tz.localize(
                datetime.combine(target_date, avail.end_time)
            )
            windows.append((start_dt, end_dt))

        return windows

    @staticmethod
    def _check_daily_limit(
        host_id: int,
        event_type: EventType,
        target_date: date,
        host_tz,
    ) -> bool:
        """Check if daily booking limit has been reached."""
        if event_type.max_bookings_per_day == 0:
            return True

        day_start = host_tz.localize(
            datetime.combine(target_date, time.min)
        )
        day_end = host_tz.localize(
            datetime.combine(target_date, time.max)
        )

        count = Booking.objects.filter(
            host_id=host_id,
            event_type=event_type,
            status='confirmed',
            start_time__gte=day_start,
            start_time__lt=day_end,
        ).count()

        return count < event_type.max_bookings_per_day

    @staticmethod
    def _is_within_availability(
        host_id: int,
        local_start: datetime,
        local_end: datetime,
    ) -> bool:
        """Check if a time falls within the host's availability."""
        target_date = local_start.date()

        override = AvailabilityOverride.objects.filter(
            host_id=host_id, date=target_date
        ).first()

        if override:
            if not override.is_available:
                return False
            if override.start_time and override.end_time:
                return (
                    local_start.time() >= override.start_time
                    and local_end.time() <= override.end_time
                )

        day_of_week = target_date.weekday()
        availabilities = Availability.objects.filter(
            host_id=host_id, day_of_week=day_of_week
        )

        for avail in availabilities:
            if (
                local_start.time() >= avail.start_time
                and local_end.time() <= avail.end_time
            ):
                return True

        return False
