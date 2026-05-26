import csv
import io
import time

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from .models import AuditLog, ImportJob, Lab, LabSession, Participant, Session

REQUIRED_COLUMNS = ["SL no", "Sl #", "Zone", "Candidate Full Name"]


def count_csv_rows(csv_text):
    lines = csv_text.splitlines()
    return max(len(lines) - 1, 0)


def read_next_chunk(csv_text, start_row, chunk_size):
    csv_io = io.StringIO(csv_text)
    reader = csv.DictReader(csv_io)
    rows = []
    for index, row in enumerate(reader):
        if index < start_row:
            continue
        rows.append({key: value or "" for key, value in row.items() if key is not None})
        if len(rows) >= chunk_size:
            break
    return reader.fieldnames or [], rows


def process_import_chunk(job, chunk_size=500, time_budget_seconds=7):
    start = time.monotonic()
    processed_total = 0
    job.status = ImportJob.Status.PROCESSING
    job.last_error = ""
    job.save(update_fields=["status", "last_error"])

    while time.monotonic() - start < time_budget_seconds:
        fieldnames, rows = read_next_chunk(job.csv_text, job.next_row, chunk_size)
        if not rows:
            job.status = ImportJob.Status.COMPLETE
            job.save(update_fields=["status"])
            return processed_total, True

        missing = [col for col in REQUIRED_COLUMNS if col not in fieldnames]
        if missing:
            job.status = ImportJob.Status.FAILED
            job.last_error = f"Missing columns: {', '.join(missing)}"
            job.save(update_fields=["status", "last_error"])
            return 0, False

        participants = []
        for row in rows:
            participants.append(
                Participant(
                    regular_sl_number=row.get("SL no", ""),
                    sl_number=row.get("Sl #", ""),
                    zone=row.get("Zone", ""),
                    candidate_full_name=row.get("Candidate Full Name", ""),
                )
            )

        Participant.objects.bulk_create(participants, batch_size=500)
        processed = len(participants)
        if processed == 0:
            job.status = ImportJob.Status.COMPLETE
            job.save(update_fields=["status"])
            return processed_total, True

        job.next_row += processed
        job.processed_count += processed
        job.save(update_fields=["next_row", "processed_count"])
        processed_total += processed

        if processed < chunk_size:
            job.status = ImportJob.Status.COMPLETE
            job.save(update_fields=["status"])
            return processed_total, True

    return processed_total, False


def get_open_session_for_update():
    return (
        Session.objects.select_for_update()
        .filter(assigned_count__lt=F("capacity"))
        .order_by("created_at")
        .last()
    )


def get_next_lab_session_for_update(session):
    return (
        LabSession.objects.select_for_update()
        .filter(session=session, assigned_count__lt=F("capacity"))
        .select_related("lab")
        .order_by("lab__sort_order")
        .first()
    )


def register_participant_auto(participant_id, user, present_absent):
    with transaction.atomic():
        participant = Participant.objects.select_for_update().get(pk=participant_id)
        if participant.registered_at:
            return {"status": "already_registered", "participant": participant}

        session = get_open_session_for_update()
        if not session:
            return {"status": "needs_session"}

        lab_session = get_next_lab_session_for_update(session)
        if not lab_session:
            return {"status": "no_lab"}

        lab_session.assigned_count += 1
        lab_session.save(update_fields=["assigned_count"])
        session.assigned_count += 1
        session.save(update_fields=["assigned_count"])

        _assign_participant(participant, user, present_absent, session, lab_session.lab)
        AuditLog.objects.create(
            participant=participant, performed_by=user, action=AuditLog.Action.REGISTERED
        )
        return {"status": "ok", "participant": participant, "session": session}


def register_participant_with_lab_override(participant_id, lab_id, user, present_absent):
    with transaction.atomic():
        participant = Participant.objects.select_for_update().get(pk=participant_id)
        if participant.registered_at:
            return {"status": "already_registered", "participant": participant}

        session = get_open_session_for_update()
        if not session:
            return {"status": "needs_session"}

        try:
            lab_session = LabSession.objects.select_for_update().get(
                session=session, lab_id=lab_id
            )
        except LabSession.DoesNotExist:
            return {"status": "invalid_lab"}

        if lab_session.assigned_count >= lab_session.capacity:
            return {"status": "lab_full", "session": session, "lab_session": lab_session}

        lab_session.assigned_count += 1
        lab_session.save(update_fields=["assigned_count"])
        session.assigned_count += 1
        session.save(update_fields=["assigned_count"])

        _assign_participant(participant, user, present_absent, session, lab_session.lab)
        AuditLog.objects.create(
            participant=participant, performed_by=user, action=AuditLog.Action.REGISTERED
        )
        return {"status": "ok", "participant": participant, "session": session}


def swap_registration(participant_id, user, present_absent, full_lab_session_id, swap_participant_id, target_lab_id):
    with transaction.atomic():
        participant = Participant.objects.select_for_update().get(pk=participant_id)
        if participant.registered_at:
            return {"status": "already_registered", "participant": participant}

        session = get_open_session_for_update()
        if not session:
            return {"status": "needs_session"}

        try:
            full_lab_session = LabSession.objects.select_for_update().get(
                pk=full_lab_session_id, session=session
            )
        except LabSession.DoesNotExist:
            return {"status": "invalid_lab"}

        try:
            target_lab_session = LabSession.objects.select_for_update().get(
                session=session, lab_id=target_lab_id
            )
        except LabSession.DoesNotExist:
            return {"status": "invalid_target_lab"}

        if target_lab_session.assigned_count >= target_lab_session.capacity:
            return {"status": "target_lab_full"}

        try:
            swap_participant = Participant.objects.select_for_update().get(
                pk=swap_participant_id, lab=full_lab_session.lab, session=session
            )
        except Participant.DoesNotExist:
            return {"status": "invalid_swap_participant"}

        swap_participant.lab = target_lab_session.lab
        swap_participant.save(update_fields=["lab"])

        target_lab_session.assigned_count += 1
        target_lab_session.save(update_fields=["assigned_count"])

        session.assigned_count += 1
        session.save(update_fields=["assigned_count"])

        _assign_participant(participant, user, present_absent, session, full_lab_session.lab)
        AuditLog.objects.create(
            participant=participant, performed_by=user, action=AuditLog.Action.SWAP
        )
        return {"status": "ok", "participant": participant, "session": session}


def change_registered_lab(participant_id, target_lab_id, user):
    with transaction.atomic():
        participant = (
            Participant.objects.select_for_update(of=("self",))
            .select_related("lab", "session")
            .get(pk=participant_id)
        )
        if not participant.registered_at or not participant.session_id or not participant.lab_id:
            return {"status": "not_registered", "participant": participant}
        if participant.lab_id == target_lab_id:
            return {"status": "same_lab", "participant": participant}

        try:
            current_lab_session = LabSession.objects.select_for_update().get(
                session=participant.session, lab=participant.lab
            )
            target_lab_session = LabSession.objects.select_for_update().get(
                session=participant.session, lab_id=target_lab_id
            )
        except LabSession.DoesNotExist:
            return {"status": "invalid_lab", "participant": participant}

        if target_lab_session.assigned_count >= target_lab_session.capacity:
            return {
                "status": "target_lab_full",
                "participant": participant,
                "target_lab_session": target_lab_session,
            }

        current_lab_session.assigned_count = max(current_lab_session.assigned_count - 1, 0)
        current_lab_session.save(update_fields=["assigned_count"])
        target_lab_session.assigned_count += 1
        target_lab_session.save(update_fields=["assigned_count"])

        participant.lab = target_lab_session.lab
        participant.registered_by = user
        participant.save(update_fields=["lab", "registered_by"])
        AuditLog.objects.create(
            participant=participant, performed_by=user, action=AuditLog.Action.SWAP
        )
        return {"status": "ok", "participant": participant}


def swap_registered_lab(participant_id, target_lab_id, swap_participant_id, user):
    with transaction.atomic():
        participant = (
            Participant.objects.select_for_update(of=("self",))
            .select_related("lab", "session")
            .get(pk=participant_id)
        )
        if not participant.registered_at or not participant.session_id or not participant.lab_id:
            return {"status": "not_registered", "participant": participant}
        if participant.lab_id == target_lab_id:
            return {"status": "same_lab", "participant": participant}

        try:
            swap_participant = Participant.objects.select_for_update().get(
                pk=swap_participant_id,
                session=participant.session,
                lab_id=target_lab_id,
                registered_at__isnull=False,
            )
        except Participant.DoesNotExist:
            return {"status": "invalid_swap_participant", "participant": participant}

        original_lab = participant.lab
        participant.lab = swap_participant.lab
        participant.registered_by = user
        participant.save(update_fields=["lab", "registered_by"])

        swap_participant.lab = original_lab
        swap_participant.save(update_fields=["lab"])

        AuditLog.objects.create(
            participant=participant, performed_by=user, action=AuditLog.Action.SWAP
        )
        AuditLog.objects.create(
            participant=swap_participant, performed_by=user, action=AuditLog.Action.SWAP
        )
        return {
            "status": "ok",
            "participant": participant,
            "swap_participant": swap_participant,
        }


def undo_registration(participant_id, user):
    with transaction.atomic():
        participant = (
            Participant.objects.select_for_update(of=("self",))
            .select_related("lab", "session")
            .get(pk=participant_id)
        )
        if not participant.registered_at:
            return {"status": "not_registered", "participant": participant}

        if participant.lab_id and participant.session_id:
            LabSession.objects.filter(
                session_id=participant.session_id,
                lab_id=participant.lab_id,
                assigned_count__gt=0,
            ).update(assigned_count=F("assigned_count") - 1)
            Session.objects.filter(
                id=participant.session_id,
                assigned_count__gt=0,
            ).update(assigned_count=F("assigned_count") - 1)

        participant.present_absent = ""
        participant.lab = None
        participant.session = None
        participant.registered_by = None
        participant.registered_at = None
        participant.save(
            update_fields=[
                "present_absent",
                "lab",
                "session",
                "registered_by",
                "registered_at",
            ]
        )
        AuditLog.objects.create(
            participant=participant,
            performed_by=user,
            action=AuditLog.Action.UNREGISTERED,
        )
        return {"status": "ok", "participant": participant}


def build_export_rows():
    rows = []
    participants = Participant.objects.select_related("lab", "session").order_by("id")
    for index, participant in enumerate(participants, start=1):
        rows.append(
            {
                "SL no": index,
                "Regular SL no": participant.regular_sl_number,
                "Sl #": participant.sl_number,
                "Zone": participant.zone,
                "Candidate Full Name": participant.candidate_full_name,
                "present/absent": participant.present_absent,
                "lab alloted": participant.lab.name if participant.lab else "",
                "session alloted": participant.session.label if participant.session else "",
                "batch timing": participant.session.start_time if participant.session else "",
                "signature": "",
            }
        )
    return rows


def _assign_participant(participant, user, present_absent, session, lab):
    participant.present_absent = present_absent
    participant.lab = lab
    participant.session = session
    participant.registered_by = user
    participant.registered_at = timezone.now()
    participant.save(
        update_fields=[
            "present_absent",
            "lab",
            "session",
            "registered_by",
            "registered_at",
        ]
    )
