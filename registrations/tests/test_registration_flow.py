from datetime import time as time_of_day
import random

from django.contrib.auth import get_user_model
from django.test import TestCase
from faker import Faker

from registrations.models import Lab, LabSession, Participant, Session
from registrations.services import (
    mark_lab_session_started,
    register_participant_auto,
    register_participant_with_lab_override,
    swap_registration,
    undo_registration,
)


class RegistrationFlowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        random.seed(1337)
        cls.faker = Faker()
        cls.faker.seed_instance(1337)
        cls.user = get_user_model().objects.create_user(
            username="registrar",
            password="test-pass",
        )
        cls.labs = []
        for order, name in enumerate(
            ["A1", "A2", "A3", "A4", "A5", "A11", "A12", "A13", "A14", "A15"],
            start=1,
        ):
            lab, _created = Lab.objects.get_or_create(
                name=name,
                defaults={"sort_order": order},
            )
            if not lab.sort_order:
                lab.sort_order = order
                lab.save(update_fields=["sort_order"])
            cls.labs.append(lab)

    def _create_participants(self, count, prefix="P", use_fake=False):
        participants = []
        for index in range(count):
            candidate_name = (
                self.faker.name() if use_fake else f"Candidate {index + 1}"
            )
            zone = (
                random.choice(["North", "South", "East", "West"])
                if use_fake
                else "Z1"
            )
            participants.append(
                Participant(
                    regular_sl_number=str(index + 1),
                    sl_number=f"{prefix}{index + 1}",
                    zone=zone,
                    candidate_full_name=candidate_name,
                )
            )
        Participant.objects.bulk_create(participants, batch_size=500)
        return list(Participant.objects.order_by("id").values_list("id", flat=True))

    def test_auto_assignment_fills_oldest_open_session(self):
        session = Session.objects.create(label="Session 1", start_time=time_of_day(9, 0))
        # Fill one seat in the first lab.
        participant_id = self._create_participants(1, prefix="S1")[0]
        result = register_participant_auto(participant_id, self.user, "present")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["session"].id, session.id)
        self.assertEqual(result["participant"].lab.name, "A1")

        # Create a newer session that is still open.
        newer_session = Session.objects.create(
            label="Session 2", start_time=time_of_day(10, 0)
        )
        participant_id = self._create_participants(1, prefix="S2")[-1]
        result = register_participant_auto(participant_id, self.user, "present")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["session"].id, session.id)
        self.assertNotEqual(result["session"].id, newer_session.id)

    def test_auto_creates_new_session_only_after_filling_open_seats(self):
        session = Session.objects.create(label="Session 1", start_time=time_of_day(9, 0))
        # Fill entire first session (10 labs * 20 seats).
        participant_ids = self._create_participants(200, prefix="F")
        for participant_id in participant_ids:
            result = register_participant_auto(participant_id, self.user, "present")
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["session"].id, session.id)

        extra_participant_id = self._create_participants(1, prefix="N")[0]
        result = register_participant_auto(extra_participant_id, self.user, "present")
        self.assertEqual(result["status"], "ok")
        self.assertNotEqual(result["session"].id, session.id)
        self.assertEqual(Session.objects.count(), 2)

    def test_undo_registration_frees_oldest_seat(self):
        session = Session.objects.create(label="Session 1", start_time=time_of_day(9, 0))
        first_id, second_id = self._create_participants(2, prefix="U")
        first_result = register_participant_auto(first_id, self.user, "present")
        self.assertEqual(first_result["status"], "ok")
        self.assertEqual(first_result["session"].id, session.id)
        self.assertEqual(first_result["participant"].lab.name, "A1")

        undo_result = undo_registration(first_result["participant"].id, self.user)
        self.assertEqual(undo_result["status"], "ok")

        second_result = register_participant_auto(second_id, self.user, "present")
        self.assertEqual(second_result["status"], "ok")
        self.assertEqual(second_result["session"].id, session.id)
        self.assertEqual(second_result["participant"].lab.name, "A1")

    def test_started_lab_closes_session_for_allocation(self):
        session = Session.objects.create(label="Session 1", start_time=time_of_day(9, 0))
        first_id = self._create_participants(1, prefix="ST")[0]
        first_result = register_participant_auto(first_id, self.user, "present")
        self.assertEqual(first_result["status"], "ok")
        self.assertEqual(first_result["session"].id, session.id)

        lab = Lab.objects.get(name="A1")
        start_result = mark_lab_session_started(lab, session, self.user)
        self.assertIn(start_result["status"], {"ok", "session_started_all"})

        second_id = self._create_participants(1, prefix="ST2")[-1]
        second_result = register_participant_auto(second_id, self.user, "present")
        self.assertEqual(second_result["status"], "ok")
        self.assertNotEqual(second_result["session"].id, session.id)

    def test_lab_override_rejects_started_lab(self):
        session = Session.objects.create(label="Session 1", start_time=time_of_day(9, 0))
        lab = Lab.objects.get(name="A1")
        mark_lab_session_started(lab, session, self.user)

        participant_id = self._create_participants(1, prefix="OL")[-1]
        result = register_participant_with_lab_override(
            participant_id, lab.id, self.user, "present"
        )
        self.assertEqual(result["status"], "lab_started")

    def test_capacity_counts_match_allocations(self):
        session = Session.objects.create(label="Session 1", start_time=time_of_day(9, 0))
        participant_ids = self._create_participants(50, prefix="C")
        for participant_id in participant_ids:
            result = register_participant_auto(participant_id, self.user, "present")
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["session"].id, session.id)

        session.refresh_from_db()
        self.assertEqual(session.assigned_count, 50)
        lab_session = LabSession.objects.get(session=session, lab__name="A1")
        self.assertEqual(lab_session.assigned_count, 20)

    def test_undo_registration_decrements_counts(self):
        session = Session.objects.create(label="Session 1", start_time=time_of_day(9, 0))
        participant_id = self._create_participants(1, prefix="DC")[0]
        result = register_participant_auto(participant_id, self.user, "present")
        self.assertEqual(result["status"], "ok")

        undo_result = undo_registration(result["participant"].id, self.user)
        self.assertEqual(undo_result["status"], "ok")

        session.refresh_from_db()
        self.assertEqual(session.assigned_count, 0)
        lab_session = LabSession.objects.get(session=session, lab__name="A1")
        self.assertEqual(lab_session.assigned_count, 0)

    def test_swap_registration_moves_participant(self):
        session = Session.objects.create(label="Session 1", start_time=time_of_day(9, 0))
        # Fill lab A1 to capacity.
        participant_ids = self._create_participants(20, prefix="SW")
        for participant_id in participant_ids:
            result = register_participant_auto(participant_id, self.user, "present")
            self.assertEqual(result["status"], "ok")

        full_lab_session = LabSession.objects.get(session=session, lab__name="A1")
        swap_participant = Participant.objects.filter(
            lab=full_lab_session.lab, session=session
        ).first()
        target_lab = Lab.objects.get(name="A2")

        incoming_id = self._create_participants(1, prefix="SWN")[0]
        result = swap_registration(
            incoming_id,
            self.user,
            "present",
            full_lab_session.id,
            swap_participant.id,
            target_lab.id,
        )
        self.assertEqual(result["status"], "ok")

        target_lab_session = LabSession.objects.get(session=session, lab=target_lab)
        self.assertEqual(target_lab_session.assigned_count, 1)
        self.assertEqual(
            Participant.objects.filter(session=session, lab=target_lab).count(),
            1,
        )
        self.assertEqual(
            Participant.objects.filter(session=session, lab=full_lab_session.lab).count(),
            20,
        )

    def test_quorum_starts_session_for_all_labs(self):
        session = Session.objects.create(label="Session 1", start_time=time_of_day(9, 0))
        for lab_name in ["A1", "A2", "A3", "A4"]:
            lab = Lab.objects.get(name=lab_name)
            mark_lab_session_started(lab, session, self.user)

        session.refresh_from_db()
        self.assertIsNotNone(session.started_at)
        self.assertEqual(
            LabSession.objects.filter(session=session, started_at__isnull=False).count(),
            10,
        )

    def test_override_rejects_full_lab(self):
        session = Session.objects.create(label="Session 1", start_time=time_of_day(9, 0))
        participant_ids = self._create_participants(20, prefix="OVF")
        for participant_id in participant_ids:
            register_participant_with_lab_override(
                participant_id, self.labs[0].id, self.user, "present"
            )

        extra_participant_id = self._create_participants(1, prefix="OVF2")[0]
        result = register_participant_with_lab_override(
            extra_participant_id, self.labs[0].id, self.user, "present"
        )
        self.assertEqual(result["status"], "lab_full")

    def test_bulk_registers_two_thousand_participants(self):
        Session.objects.create(label="Session 1", start_time=time_of_day(9, 0))
        participant_ids = self._create_participants(2000, prefix="B", use_fake=True)
        for participant_id in participant_ids:
            result = register_participant_auto(participant_id, self.user, "present")
            self.assertEqual(result["status"], "ok")

        self.assertEqual(Participant.objects.filter(registered_at__isnull=False).count(), 2000)
        self.assertGreaterEqual(Session.objects.count(), 10)
