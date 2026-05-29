from datetime import time as time_of_day
import random
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.contrib.admin.sites import AdminSite
from django.http import HttpResponse
from django.test import RequestFactory
from django.test import TestCase
from django.urls import reverse
from faker import Faker

from registrations.admin import LabSessionAdmin
from registrations.models import AuditLog, Lab, LabCoordinator, LabSession, Participant, Session
from registrations.services import (
    mark_lab_session_started,
    reject_lab_registration,
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

    def setUp(self):
        AuditLog.objects.all().delete()
        Participant.objects.all().delete()
        LabCoordinator.objects.all().delete()
        Session.objects.all().delete()

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
        return [participant.id for participant in participants]

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

    def test_lab_coordinator_can_reject_only_own_lab_registration(self):
        Session.objects.create(label="Session 1", start_time=time_of_day(9, 0))
        own_lab = Lab.objects.get(name="A1")
        other_lab = Lab.objects.get(name="A2")
        coordinator = get_user_model().objects.create_user(
            username="lab-coordinator",
            password="test-pass",
        )
        LabCoordinator.objects.create(user=coordinator, lab=own_lab)

        own_id, other_id = self._create_participants(2, prefix="LC")
        own_result = register_participant_with_lab_override(
            own_id, own_lab.id, self.user, "present"
        )
        other_result = register_participant_with_lab_override(
            other_id, other_lab.id, self.user, "present"
        )
        self.assertEqual(own_result["status"], "ok")
        self.assertEqual(other_result["status"], "ok")

        rejected = reject_lab_registration(own_id, own_lab, coordinator)
        self.assertEqual(rejected["status"], "ok")
        self.assertEqual(
            AuditLog.objects.filter(
                participant_id=own_id,
                performed_by=coordinator,
                action=AuditLog.Action.REJECTED,
            ).count(),
            1,
        )
        own_lab_session = LabSession.objects.get(session=own_result["session"], lab=own_lab)
        self.assertEqual(own_lab_session.assigned_count, 0)

        wrong_lab = reject_lab_registration(other_id, own_lab, coordinator)
        self.assertEqual(wrong_lab["status"], "wrong_lab")
        self.assertEqual(
            Participant.objects.get(pk=other_id).registered_at,
            other_result["participant"].registered_at,
        )

    def test_lab_coordinator_dashboard_is_scoped_to_assigned_lab(self):
        Session.objects.create(label="Session 1", start_time=time_of_day(9, 0))
        own_lab = Lab.objects.get(name="A1")
        other_lab = Lab.objects.get(name="A2")
        coordinator = get_user_model().objects.create_user(
            username="scoped-coordinator",
            password="test-pass",
        )
        LabCoordinator.objects.create(user=coordinator, lab=own_lab)
        own_id, other_id = self._create_participants(2, prefix="LCS")
        register_participant_with_lab_override(own_id, own_lab.id, self.user, "present")
        register_participant_with_lab_override(other_id, other_lab.id, self.user, "present")

        self.client.force_login(coordinator)
        captured_context = {}

        def capture_render(request, template_name, context):
            captured_context.update(context)
            return HttpResponse("ok")

        with patch("registrations.views.render", side_effect=capture_render):
            response = self.client.get(reverse("registrations:lab_dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured_context["lab"], own_lab)
        self.assertEqual(
            list(captured_context["participants"].values_list("id", flat=True)),
            [own_id],
        )

    def test_lab_coordinator_cannot_accept_or_register_participants(self):
        participant_id = self._create_participants(1, prefix="NOREG")[0]
        coordinator = get_user_model().objects.create_user(
            username="view-only-coordinator",
            password="test-pass",
        )
        LabCoordinator.objects.create(user=coordinator, lab=Lab.objects.get(name="A1"))

        self.client.force_login(coordinator)
        response = self.client.post(
            reverse("registrations:register_detail", args=[participant_id]),
            {"present_absent": "present"},
        )

        self.assertEqual(response.status_code, 403)
        self.assertIsNone(Participant.objects.get(pk=participant_id).registered_at)

    def test_admin_can_edit_individual_lab_session_capacity(self):
        session = Session.objects.create(label="Session 1", start_time=time_of_day(9, 0))
        lab_session = LabSession.objects.get(session=session, lab__name="A1")
        admin_user = get_user_model().objects.create_user(
            username="capacity-admin",
            password="test-pass",
            is_staff=True,
        )
        permission = Permission.objects.get(codename="change_labsession")
        admin_user.user_permissions.add(permission)

        request = RequestFactory().post("/")
        request.user = admin_user
        model_admin = LabSessionAdmin(LabSession, AdminSite())
        form_class = model_admin.get_form(request, lab_session)
        self.assertIn("capacity", form_class.base_fields)

        form = form_class(
            data={
                "lab": lab_session.lab_id,
                "session": lab_session.session_id,
                "assigned_count": lab_session.assigned_count,
                "capacity": 33,
            },
            instance=lab_session,
        )
        self.assertTrue(form.is_valid(), form.errors)
        form.save()

        lab_session.refresh_from_db()
        self.assertEqual(lab_session.capacity, 33)
