"""Comprehensive test suite for the Calendly-like scheduling app.

Used by the reward function to score the scheduling engine.
"""
from datetime import date, datetime, time, timedelta

from django.contrib.auth.models import User
from django.test import TestCase

import pytz

from .engine import SchedulingEngine
from .models import Availability, AvailabilityOverride, Booking, EventType


class EventTypeModelTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('host', 'host@test.com', 'pass')
        self.event = EventType.objects.create(
            host=self.user,
            name='30 Min Meeting',
            slug='30-min',
            duration_minutes=30,
            buffer_before_minutes=5,
            buffer_after_minutes=10,
        )

    def test_create_event_type(self):
        self.assertEqual(self.event.name, '30 Min Meeting')
        self.assertEqual(self.event.duration_minutes, 30)
        self.assertTrue(self.event.is_active)

    def test_total_block_minutes(self):
        self.assertEqual(self.event.total_block_minutes, 45)

    def test_event_type_str(self):
        self.assertEqual(str(self.event), '30 Min Meeting (30min)')

    def test_unique_slug_per_host(self):
        with self.assertRaises(Exception):
            EventType.objects.create(
                host=self.user, name='Dup', slug='30-min',
                duration_minutes=30,
            )


class AvailabilityModelTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('host', 'host@test.com', 'pass')
        self.avail = Availability.objects.create(
            host=self.user,
            day_of_week=0,
            start_time=time(9, 0),
            end_time=time(17, 0),
            timezone='US/Eastern',
        )

    def test_create_availability(self):
        self.assertEqual(self.avail.day_of_week, 0)
        self.assertEqual(self.avail.start_time, time(9, 0))

    def test_availability_str(self):
        self.assertIn('Monday', str(self.avail))


class ConflictDetectionTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('host', 'host@test.com', 'pass')
        self.event = EventType.objects.create(
            host=self.user, name='Meeting', slug='meeting',
            duration_minutes=30, buffer_before_minutes=5,
            buffer_after_minutes=5,
        )
        self.start = pytz.UTC.localize(datetime(2026, 3, 2, 10, 0))
        self.end = self.start + timedelta(minutes=30)

        Booking.objects.create(
            event_type=self.event, host=self.user,
            guest_name='Alice', guest_email='alice@test.com',
            start_time=self.start, end_time=self.end,
            buffer_start=self.start - timedelta(minutes=5),
            buffer_end=self.end + timedelta(minutes=5),
        )

    def test_exact_overlap_detected(self):
        self.assertTrue(SchedulingEngine.has_conflict(
            self.user.id, self.start, self.end
        ))

    def test_partial_overlap_detected(self):
        new_start = self.start + timedelta(minutes=15)
        new_end = new_start + timedelta(minutes=30)
        self.assertTrue(SchedulingEngine.has_conflict(
            self.user.id, new_start, new_end
        ))

    def test_no_overlap_after_buffer(self):
        new_start = self.end + timedelta(minutes=10)
        new_end = new_start + timedelta(minutes=30)
        self.assertFalse(SchedulingEngine.has_conflict(
            self.user.id, new_start, new_end
        ))

    def test_overlap_within_buffer(self):
        new_start = self.end + timedelta(minutes=2)
        new_end = new_start + timedelta(minutes=30)
        self.assertTrue(SchedulingEngine.has_conflict(
            self.user.id, new_start, new_end, buffer_before=5
        ))

    def test_no_conflict_with_cancelled(self):
        Booking.objects.filter(host=self.user).update(status='cancelled')
        self.assertFalse(SchedulingEngine.has_conflict(
            self.user.id, self.start, self.end
        ))


class BufferTimeTest(TestCase):
    def test_buffer_enforcement(self):
        user = User.objects.create_user('host', 'host@test.com', 'pass')
        event = EventType.objects.create(
            host=user, name='Meeting', slug='meeting',
            duration_minutes=30,
            buffer_before_minutes=10,
            buffer_after_minutes=15,
        )
        start = pytz.UTC.localize(datetime(2026, 3, 2, 10, 0))

        buf_start, meet_start, meet_end, buf_end = (
            SchedulingEngine.enforce_buffer(event, start)
        )

        self.assertEqual(buf_start, start - timedelta(minutes=10))
        self.assertEqual(meet_start, start)
        self.assertEqual(meet_end, start + timedelta(minutes=30))
        self.assertEqual(buf_end, start + timedelta(minutes=45))

    def test_zero_buffer(self):
        user = User.objects.create_user('host', 'host@test.com', 'pass')
        event = EventType.objects.create(
            host=user, name='Quick', slug='quick',
            duration_minutes=15,
            buffer_before_minutes=0,
            buffer_after_minutes=0,
        )
        start = pytz.UTC.localize(datetime(2026, 3, 2, 10, 0))

        buf_start, meet_start, meet_end, buf_end = (
            SchedulingEngine.enforce_buffer(event, start)
        )

        self.assertEqual(buf_start, meet_start)
        self.assertEqual(buf_end, meet_end)


class TimezoneHandlingTest(TestCase):
    def test_utc_to_eastern(self):
        utc_dt = pytz.UTC.localize(datetime(2026, 3, 2, 15, 0))
        eastern = SchedulingEngine.convert_timezone(
            utc_dt, 'UTC', 'US/Eastern'
        )
        self.assertEqual(eastern.hour, 10)

    def test_eastern_to_pacific(self):
        eastern = pytz.timezone('US/Eastern')
        dt = eastern.localize(datetime(2026, 3, 2, 12, 0))
        pacific = SchedulingEngine.convert_timezone(
            dt, 'US/Eastern', 'US/Pacific'
        )
        self.assertEqual(pacific.hour, 9)

    def test_normalize_to_utc(self):
        eastern = pytz.timezone('US/Eastern')
        dt = datetime(2026, 3, 2, 10, 0)
        utc = SchedulingEngine.normalize_to_utc(dt, 'US/Eastern')
        self.assertEqual(utc.hour, 15)
        self.assertEqual(utc.tzinfo, pytz.UTC)

    def test_display_in_timezone(self):
        utc_dt = pytz.UTC.localize(datetime(2026, 3, 2, 18, 0))
        tokyo = SchedulingEngine.display_in_timezone(utc_dt, 'Asia/Tokyo')
        self.assertEqual(tokyo.day, 3)
        self.assertEqual(tokyo.hour, 3)

    def test_naive_datetime_handling(self):
        naive = datetime(2026, 3, 2, 10, 0)
        utc = SchedulingEngine.normalize_to_utc(naive, 'US/Eastern')
        self.assertIsNotNone(utc.tzinfo)

    def test_dst_transition(self):
        eastern = pytz.timezone('US/Eastern')
        winter = eastern.localize(datetime(2026, 1, 15, 12, 0))
        summer = eastern.localize(datetime(2026, 7, 15, 12, 0))

        winter_utc = winter.astimezone(pytz.UTC)
        summer_utc = summer.astimezone(pytz.UTC)

        self.assertEqual(winter_utc.hour, 17)
        self.assertEqual(summer_utc.hour, 16)


class AvailabilityMaskingTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('host', 'host@test.com', 'pass')
        self.event = EventType.objects.create(
            host=self.user, name='Meeting', slug='meeting',
            duration_minutes=30,
            buffer_before_minutes=0,
            buffer_after_minutes=0,
        )
        Availability.objects.create(
            host=self.user, day_of_week=0,
            start_time=time(9, 0), end_time=time(17, 0),
            timezone='UTC',
        )

    def test_slots_returned_for_available_day(self):
        target = date(2026, 3, 2)  # Monday
        slots = SchedulingEngine.get_available_slots(
            self.user.id, self.event, target
        )
        self.assertGreater(len(slots), 0)

    def test_no_slots_for_unavailable_day(self):
        target = date(2026, 3, 3)  # Tuesday, no availability set
        slots = SchedulingEngine.get_available_slots(
            self.user.id, self.event, target
        )
        self.assertEqual(len(slots), 0)

    def test_no_slots_on_override_day_off(self):
        target = date(2026, 3, 2)
        AvailabilityOverride.objects.create(
            host=self.user, date=target, is_available=False,
        )
        slots = SchedulingEngine.get_available_slots(
            self.user.id, self.event, target
        )
        self.assertEqual(len(slots), 0)

    def test_slots_hide_private_details(self):
        target = date(2026, 3, 2)
        slots = SchedulingEngine.get_available_slots(
            self.user.id, self.event, target
        )
        for slot in slots:
            self.assertNotIn('reason', slot)
            self.assertNotIn('host_calendar', slot)
            self.assertIn('available', slot)

    def test_slots_in_guest_timezone(self):
        target = date(2026, 3, 2)
        slots = SchedulingEngine.get_available_slots(
            self.user.id, self.event, target,
            guest_timezone='US/Eastern',
        )
        if slots:
            start = slots[0]['start_time']
            self.assertIn('-05:00', start)

    def test_booked_slot_removed(self):
        target = date(2026, 3, 2)
        start = pytz.UTC.localize(datetime(2026, 3, 2, 9, 0))
        end = start + timedelta(minutes=30)
        Booking.objects.create(
            event_type=self.event, host=self.user,
            guest_name='Guest', guest_email='guest@test.com',
            start_time=start, end_time=end,
            buffer_start=start, buffer_end=end,
        )
        slots = SchedulingEngine.get_available_slots(
            self.user.id, self.event, target
        )
        slot_starts = [s['start_time_utc'] for s in slots]
        self.assertNotIn(start.isoformat(), slot_starts)


class BookingCreationTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('host', 'host@test.com', 'pass')
        self.event = EventType.objects.create(
            host=self.user, name='Meeting', slug='meeting',
            duration_minutes=30,
            buffer_before_minutes=5,
            buffer_after_minutes=5,
        )
        Availability.objects.create(
            host=self.user, day_of_week=0,
            start_time=time(9, 0), end_time=time(17, 0),
            timezone='UTC',
        )

    def test_create_valid_booking(self):
        start = pytz.UTC.localize(datetime(2026, 3, 2, 10, 0))
        booking = SchedulingEngine.create_booking(
            self.event, start, 'Guest', 'guest@test.com',
        )
        self.assertEqual(booking.status, 'confirmed')
        self.assertEqual(booking.guest_name, 'Guest')

    def test_conflict_raises_error(self):
        start = pytz.UTC.localize(datetime(2026, 3, 2, 10, 0))
        SchedulingEngine.create_booking(
            self.event, start, 'Guest1', 'g1@test.com',
        )
        with self.assertRaises(ValueError):
            SchedulingEngine.create_booking(
                self.event, start, 'Guest2', 'g2@test.com',
            )

    def test_cancel_booking(self):
        start = pytz.UTC.localize(datetime(2026, 3, 2, 10, 0))
        booking = SchedulingEngine.create_booking(
            self.event, start, 'Guest', 'guest@test.com',
        )
        booking.cancel()
        self.assertEqual(booking.status, 'cancelled')
        self.assertIsNotNone(booking.cancelled_at)

    def test_booking_after_cancellation(self):
        start = pytz.UTC.localize(datetime(2026, 3, 2, 10, 0))
        b1 = SchedulingEngine.create_booking(
            self.event, start, 'Guest1', 'g1@test.com',
        )
        b1.cancel()
        b2 = SchedulingEngine.create_booking(
            self.event, start, 'Guest2', 'g2@test.com',
        )
        self.assertEqual(b2.status, 'confirmed')
