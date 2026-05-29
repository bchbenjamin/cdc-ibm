import csv
from functools import wraps

from django.core.exceptions import ObjectDoesNotExist, PermissionDenied
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.decorators import permission_required
from django.db.models import F, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from .forms import CSVUploadForm, LabChangeForm, RegistrationForm, SessionForm, SwapForm
from .models import ImportJob, Lab, LabSession, Participant, Session
from .services import (
    build_export_rows,
    change_registered_lab,
    count_csv_rows,
    get_current_session,
    get_open_session,
    mark_lab_session_started,
    process_import_chunk,
    reject_lab_registration,
    register_participant_auto,
    register_participant_with_lab_override,
    swap_registered_lab,
    swap_registration,
    undo_registration,
)


def _has_registration_access(user):
    return user.is_active and (
        user.is_staff or user.has_perm("registrations.register_participant")
    )


def _has_lab_change_access(user):
    return user.is_active and (
        user.is_staff or user.has_perm("registrations.change_participant_lab")
    )


def _has_undo_registration_access(user):
    return user.is_active and (
        user.is_staff or user.has_perm("registrations.undo_registration")
    )


def _lab_coordinator_lab(user):
    if not user.is_authenticated:
        return None
    try:
        return user.labcoordinator.lab
    except ObjectDoesNotExist:
        return None


def _has_lab_coordinator_access(user):
    return user.is_active and _lab_coordinator_lab(user) is not None


def _has_portal_access(user):
    return _has_registration_access(user) or _has_lab_coordinator_access(user)


def registration_access_required(view_func):
    @wraps(view_func)
    def wrapped(request, *args, **kwargs):
        if not _has_registration_access(request.user):
            raise PermissionDenied
        return view_func(request, *args, **kwargs)

    return wrapped


def portal_access_required(view_func):
    @wraps(view_func)
    def wrapped(request, *args, **kwargs):
        if not _has_portal_access(request.user):
            raise PermissionDenied
        return view_func(request, *args, **kwargs)

    return wrapped


def lab_coordinator_access_required(view_func):
    @wraps(view_func)
    def wrapped(request, *args, **kwargs):
        if not _has_lab_coordinator_access(request.user):
            raise PermissionDenied
        return view_func(request, *args, **kwargs)

    return wrapped


def favicon(request):
    return redirect(
        "https://fonts.gstatic.com/s/i/short-term/release/materialsymbolsrounded/event/default/24px.svg"
    )


@login_required
@portal_access_required
def dashboard(request):
    if _has_lab_coordinator_access(request.user) and not _has_registration_access(request.user):
        return redirect("registrations:lab_dashboard")

    total = Participant.objects.count()
    registered = Participant.objects.filter(registered_at__isnull=False).count()
    pending = total - registered
    current_session = get_open_session()
    lab_status = []
    if current_session:
        lab_status = (
            LabSession.objects.filter(session=current_session)
            .select_related("lab")
            .order_by("lab__sort_order")
        )

    context = {
        "total": total,
        "registered": registered,
        "pending": pending,
        "current_session": current_session,
        "lab_status": lab_status,
    }
    return render(request, "registrations/dashboard.html", context)


@login_required
@permission_required("registrations.add_importjob", raise_exception=True)
def import_csv(request):
    if request.method == "POST":
        form = CSVUploadForm(request.POST, request.FILES)
        if form.is_valid():
            csv_file = form.cleaned_data["csv_file"]
            csv_text = csv_file.read().decode("utf-8-sig")
            job = ImportJob.objects.create(
                csv_text=csv_text,
                original_filename=csv_file.name,
                created_by=request.user,
                total_rows=count_csv_rows(csv_text),
            )
            processed, done = process_import_chunk(job)
            if done:
                messages.success(request, "Import completed.")
            else:
                messages.info(
                    request,
                    f"Imported {processed} rows. Continue to process the next chunk.",
                )
            return redirect("registrations:import_progress", job_id=job.id)
    else:
        form = CSVUploadForm()
    return render(request, "registrations/import.html", {"form": form})


@login_required
@permission_required("registrations.add_importjob", raise_exception=True)
def import_progress(request, job_id):
    job = get_object_or_404(ImportJob, pk=job_id)
    if request.method == "POST":
        processed, done = process_import_chunk(job)
        if done:
            messages.success(request, "Import completed.")
        else:
            messages.info(
                request,
                f"Imported {processed} rows. Continue to process the next chunk.",
            )
        return redirect("registrations:import_progress", job_id=job.id)

    return render(request, "registrations/import_progress.html", {"job": job})


@login_required
@registration_access_required
def register_search(request):
    query = request.GET.get("q", "").strip()
    results = []
    if query:
        results = (
            Participant.objects.filter(
                Q(sl_number__icontains=query)
                | Q(regular_sl_number__icontains=query)
                | Q(candidate_full_name__icontains=query)
            )
            .order_by("sl_number")
            .all()[:50]
        )
    return render(
        request,
        "registrations/register_search.html",
        {"query": query, "results": results},
    )


@login_required
@registration_access_required
def register_detail(request, participant_id):
    participant = get_object_or_404(Participant, pk=participant_id)
    labs = Lab.objects.order_by("sort_order")

    if participant.registered_at:
        if request.method == "POST":
            action = request.POST.get("action")
            if action == "change_lab":
                if not _has_lab_change_access(request.user):
                    raise PermissionDenied
                form = LabChangeForm(request.POST, current_lab=participant.lab)
                if form.is_valid():
                    target_lab = form.cleaned_data["target_lab"]
                    result = change_registered_lab(participant.id, target_lab.id, request.user)
                    if result["status"] == "ok":
                        messages.success(request, "Lab allocation updated.")
                        return redirect(
                            "registrations:register_detail", participant_id=participant.id
                        )
                    if result["status"] == "target_lab_started":
                        messages.error(
                            request,
                            "The target lab has already started for this session.",
                        )
                        return redirect(
                            "registrations:register_detail", participant_id=participant.id
                        )
                    if result["status"] == "target_lab_full":
                        target_lab_session = result["target_lab_session"]
                        swap_participants = Participant.objects.filter(
                            lab=target_lab_session.lab,
                            session=participant.session,
                            registered_at__isnull=False,
                        ).order_by("candidate_full_name")
                        swap_form = SwapForm(
                            swap_participant_queryset=swap_participants,
                            target_lab_queryset=Lab.objects.filter(pk=target_lab.id),
                        )
                        return render(
                            request,
                            "registrations/registered_swap.html",
                            {
                                "participant": participant,
                                "target_lab": target_lab,
                                "swap_form": swap_form,
                            },
                        )
                    messages.error(request, "Unable to update lab allocation.")
            elif action == "swap_registered_lab":
                if not _has_lab_change_access(request.user):
                    raise PermissionDenied
                target_lab = get_object_or_404(Lab, pk=request.POST.get("target_lab_id"))
                swap_participants = Participant.objects.filter(
                    lab=target_lab,
                    session=participant.session,
                    registered_at__isnull=False,
                ).order_by("candidate_full_name")
                swap_form = SwapForm(
                    request.POST,
                    swap_participant_queryset=swap_participants,
                    target_lab_queryset=Lab.objects.filter(pk=target_lab.id),
                )
                if swap_form.is_valid():
                    swap_participant = swap_form.cleaned_data["swap_participant"]
                    result = swap_registered_lab(
                        participant.id,
                        target_lab.id,
                        swap_participant.id,
                        request.user,
                    )
                    if result["status"] == "ok":
                        messages.success(request, "Lab allocation swapped.")
                        return redirect(
                            "registrations:register_detail", participant_id=participant.id
                        )
                    if result["status"] == "target_lab_started":
                        messages.error(
                            request,
                            "The target lab has already started for this session.",
                        )
                        return redirect(
                            "registrations:register_detail", participant_id=participant.id
                        )
                messages.error(request, "Unable to complete lab swap.")
            elif action == "undo_registration":
                if not _has_undo_registration_access(request.user):
                    raise PermissionDenied
                result = undo_registration(participant.id, request.user)
                if result["status"] == "ok":
                    messages.success(request, "Registration undone.")
                    return redirect(
                        "registrations:register_detail", participant_id=participant.id
                    )
                messages.error(request, "Participant is not currently registered.")

        return render(
            request,
            "registrations/register_detail.html",
            {
                "participant": participant,
                "already_registered": True,
                "labs": labs,
                "lab_change_form": LabChangeForm(current_lab=participant.lab),
                "can_change_lab": _has_lab_change_access(request.user),
                "can_undo_registration": _has_undo_registration_access(request.user),
            },
        )

    if request.method == "POST":
        form = RegistrationForm(request.POST)
        if form.is_valid():
            present_absent = form.cleaned_data["present_absent"]
            lab_override = form.cleaned_data["lab_override"]
            if lab_override:
                result = register_participant_with_lab_override(
                    participant.id, lab_override.id, request.user, present_absent
                )
                if result["status"] == "lab_full":
                    full_lab_session = result["lab_session"]
                    session = result["session"]
                    swap_participants = Participant.objects.filter(
                        lab=full_lab_session.lab, session=session
                    ).order_by("candidate_full_name")
                    available_lab_ids = (
                        LabSession.objects.filter(
                            session=session,
                            started_at__isnull=True,
                            assigned_count__lt=F("capacity"),
                        )
                        .exclude(lab=full_lab_session.lab)
                        .values_list("lab_id", flat=True)
                    )
                    target_labs = Lab.objects.filter(id__in=available_lab_ids).order_by(
                        "sort_order"
                    )
                    swap_form = SwapForm(
                        swap_participant_queryset=swap_participants,
                        target_lab_queryset=target_labs,
                    )
                    return render(
                        request,
                        "registrations/swap.html",
                        {
                            "participant": participant,
                            "full_lab_session": full_lab_session,
                            "swap_form": swap_form,
                            "present_absent": present_absent,
                            "available_labs": target_labs,
                        },
                    )
                if result["status"] == "lab_started":
                    messages.error(
                        request,
                        "That lab has already started for the current session.",
                    )
                elif result["status"] == "already_registered":
                    messages.info(request, "Participant is already registered.")
                elif result["status"] == "ok":
                    messages.success(request, "Participant registered.")
                    return redirect(
                        "registrations:register_detail", participant_id=participant.id
                    )
                else:
                    messages.error(request, "No available lab seats.")
            else:
                result = register_participant_auto(
                    participant.id, request.user, present_absent
                )
                if result["status"] == "already_registered":
                    messages.info(request, "Participant is already registered.")
                elif result["status"] == "ok":
                    messages.success(request, "Participant registered.")
                    return redirect(
                        "registrations:register_detail", participant_id=participant.id
                    )
                else:
                    messages.error(request, "No available lab seats.")
    else:
        form = RegistrationForm()

    return render(
        request,
        "registrations/register_detail.html",
        {"participant": participant, "form": form, "labs": labs},
    )


@login_required
@registration_access_required
def create_session(request):
    next_url = (
        request.POST.get("next_url")
        or request.GET.get("next")
        or reverse("registrations:dashboard")
    )
    session_count = Session.objects.count()
    initial = {"label": f"Session {session_count + 1}"}
    if session_count == 0:
        initial["start_time"] = "09:00"

    if request.method == "POST":
        form = SessionForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "Session created.")
            return redirect(next_url)
    else:
        form = SessionForm(initial=initial)

    return render(
        request,
        "registrations/session_prompt.html",
        {"form": form, "next_url": next_url},
    )


@login_required
@registration_access_required
def swap_registration_view(request):
    if request.method != "POST":
        return redirect("registrations:register_search")

    participant_id = request.POST.get("participant_id")
    full_lab_session_id = request.POST.get("full_lab_session_id")
    present_absent = request.POST.get("present_absent", "")

    full_lab_session = get_object_or_404(LabSession, pk=full_lab_session_id)
    session = full_lab_session.session

    swap_participants = Participant.objects.filter(
        lab=full_lab_session.lab, session=session
    ).order_by("candidate_full_name")
    available_lab_ids = LabSession.objects.filter(
        session=session,
        started_at__isnull=True,
        assigned_count__lt=F("capacity"),
    ).exclude(lab=full_lab_session.lab).values_list("lab_id", flat=True)
    target_labs = Lab.objects.filter(id__in=available_lab_ids).order_by("sort_order")

    form = SwapForm(
        request.POST,
        swap_participant_queryset=swap_participants,
        target_lab_queryset=target_labs,
    )
    if form.is_valid():
        swap_participant = form.cleaned_data["swap_participant"]
        target_lab = form.cleaned_data["target_lab"]
        result = swap_registration(
            participant_id,
            request.user,
            present_absent,
            full_lab_session.id,
            swap_participant.id,
            target_lab.id,
        )
        if result["status"] == "ok":
            messages.success(request, "Swap completed and participant registered.")
            return redirect("registrations:register_detail", participant_id=participant_id)
        if result["status"] == "needs_session":
            override_result = register_participant_with_lab_override(
                participant_id,
                target_lab.id,
                request.user,
                present_absent,
            )
            if override_result["status"] == "ok":
                messages.success(
                    request,
                    "New session created. Participant registered.",
                )
                return redirect(
                    "registrations:register_detail", participant_id=participant_id
                )
            if override_result["status"] in {"lab_full", "lab_started"}:
                messages.error(
                    request,
                    "Unable to allocate that lab in the new session.",
                )
                return redirect(
                    "registrations:register_detail", participant_id=participant_id
                )
        if result["status"] in {"lab_started", "target_lab_started"}:
            messages.error(
                request,
                "That lab has already started for the current session.",
            )
            return redirect(
                "registrations:register_detail", participant_id=participant_id
            )
        messages.error(request, "Unable to complete swap. Try again.")

    return render(
        request,
        "registrations/swap.html",
        {
            "participant": get_object_or_404(Participant, pk=participant_id),
            "full_lab_session": full_lab_session,
            "swap_form": form,
            "present_absent": present_absent,
            "available_labs": target_labs,
        },
    )


@login_required
@permission_required("registrations.view_participant", raise_exception=True)
def export_csv(request):
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = "attachment; filename=registrations_export.csv"
    fieldnames = [
        "SL no",
        "Regular SL no",
        "Sl #",
        "Zone",
        "Candidate Full Name",
        "present/absent",
        "lab alloted",
        "session alloted",
        "batch timing",
        "signature",
    ]
    writer = csv.DictWriter(response, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(build_export_rows())
    return response


@login_required
@portal_access_required
def lab_dashboard(request):
    lab = _lab_coordinator_lab(request.user)
    if lab is None:
        if _has_registration_access(request.user):
            return redirect("registrations:dashboard")
        raise PermissionDenied

    current_session = get_current_session()
    current_lab_session = None
    if current_session:
        current_lab_session = (
            LabSession.objects.filter(session=current_session, lab=lab)
            .select_related("session")
            .first()
        )

    participants = (
        Participant.objects.filter(lab=lab, registered_at__isnull=False)
        .select_related("session")
        .order_by("session__start_time", "regular_sl_number", "sl_number")
    )
    sessions = (
        LabSession.objects.filter(lab=lab)
        .select_related("session")
        .order_by("session__start_time")
    )
    return render(
        request,
        "registrations/lab_dashboard.html",
        {
            "lab": lab,
            "participants": participants,
            "sessions": sessions,
            "current_session": current_session,
            "current_lab_session": current_lab_session,
        },
    )


@login_required
@lab_coordinator_access_required
def start_lab_session(request):
    if request.method != "POST":
        return redirect("registrations:lab_dashboard")

    lab = _lab_coordinator_lab(request.user)
    if lab is None:
        raise PermissionDenied

    session = get_current_session()
    if not session:
        messages.info(request, "No active session is available to start.")
        return redirect("registrations:lab_dashboard")

    result = mark_lab_session_started(lab, session, request.user)
    status = result["status"]
    if status == "ok":
        messages.success(
            request,
            f"{session.label} marked as started for lab {lab.name}.",
        )
    elif status == "already_started":
        messages.info(
            request,
            f"{session.label} is already marked as started for lab {lab.name}.",
        )
    elif status == "session_started":
        messages.info(request, f"{session.label} is already started for all labs.")
    elif status == "session_started_all":
        messages.success(
            request,
            f"Quorum reached. {session.label} started for all labs.",
        )
    else:
        messages.error(request, "Unable to mark the session as started.")

    return redirect("registrations:lab_dashboard")


@login_required
@lab_coordinator_access_required
def reject_lab_registration_view(request, participant_id):
    if request.method != "POST":
        return redirect("registrations:lab_dashboard")

    lab = _lab_coordinator_lab(request.user)
    if lab is None:
        raise PermissionDenied

    participant = get_object_or_404(Participant, pk=participant_id)
    if participant.lab_id != lab.id:
        raise PermissionDenied

    result = reject_lab_registration(participant.id, lab, request.user)
    if result["status"] == "ok":
        messages.success(request, "Registration rejected.")
    elif result["status"] == "not_registered":
        messages.info(request, "Participant is not currently registered.")
    else:
        messages.error(request, "Unable to reject that registration.")

    return redirect("registrations:lab_dashboard")
