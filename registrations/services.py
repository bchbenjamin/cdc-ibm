import io
import time

import pandas as pd
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
    skiprows = range(1, start_row + 1) if start_row > 0 else None
    reader = pd.read_csv(
        csv_io,
        dtype=str,
        keep_default_na=False,
        skiprows=skiprows,
        chunksize=chunk_size,
    )
    try:
        return next(reader)
    except StopIteration:
        return None


def process_import_chunk(job, chunk_size=500, time_budget_seconds=7):
    start = time.monotonic()
    processed_total = 0
    job.status = ImportJob.Status.PROCESSING
    job.last_error = ""
    job.save(update_fields=["status", "last_error"])

    while time.monotonic() - start < time_budget_seconds:
        df = read_next_chunk(job.csv_text, job.next_row, chunk_size)
        if df is None or df.empty:
            job.status = ImportJob.Status.COMPLETE
            job.save(update_fields=["status"])
            return processed_total, True

        missing = [col for col in REQUIRED_COLUMNS if col not in df.columns]
        if missing:
            job.status = ImportJob.Status.FAILED
            job.last_error = f"Missing columns: {', '.join(missing)}"
            job.save(update_fields=["status", "last_error"])
            return 0, False

        df = df.drop(columns=["SL no"], errors="ignore")
        rows = df.to_dict(orient="records")
        participants = []
        for row in rows:
            participants.append(
                Participant(
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


def build_export_dataframe():
    rows = []
    participants = Participant.objects.select_related("lab", "session").order_by("id")
    for index, participant in enumerate(participants, start=1):
        rows.append(
            {
                "SL no": index,
                "Sl #": participant.sl_number,
                "Zone": participant.zone,
                "Candidate Full Name": participant.candidate_full_name,
                "present/absent": participant.present_absent,
                "lab alloted": participant.lab.name if participant.lab else "",
                "session alloted": participant.session.label if participant.session else "",
                "signature": "",
            }
        )
    return pd.DataFrame(
        rows,
        columns=[
            "SL no",
            "Sl #",
            "Zone",
            "Candidate Full Name",
            "present/absent",
            "lab alloted",
            "session alloted",
            "signature",
        ],
    )


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
