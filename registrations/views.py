from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import F, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from .forms import CSVUploadForm, RegistrationForm, SessionForm, SwapForm
from .models import ImportJob, Lab, LabSession, Participant, Session
from .services import (
    build_export_dataframe,
    count_csv_rows,
    process_import_chunk,
    register_participant_auto,
    register_participant_with_lab_override,
    swap_registration,
)


@login_required
def dashboard(request):
    total = Participant.objects.count()
    registered = Participant.objects.filter(registered_at__isnull=False).count()
    pending = total - registered
    current_session = Session.objects.order_by("created_at").last()
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
def register_search(request):
    query = request.GET.get("q", "").strip()
    results = []
    if query:
        results = (
            Participant.objects.filter(
                Q(sl_number__icontains=query) | Q(candidate_full_name__icontains=query)
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
def register_detail(request, participant_id):
    participant = get_object_or_404(Participant, pk=participant_id)
    labs = Lab.objects.order_by("sort_order")

    if participant.registered_at:
        return render(
            request,
            "registrations/register_detail.html",
            {"participant": participant, "already_registered": True, "labs": labs},
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
                if result["status"] == "needs_session":
                    return redirect(
                        f"{reverse('registrations:create_session')}?next={request.path}"
                    )
                if result["status"] == "lab_full":
                    full_lab_session = result["lab_session"]
                    session = result["session"]
                    swap_participants = Participant.objects.filter(
                        lab=full_lab_session.lab, session=session
                    ).order_by("candidate_full_name")
                    available_lab_ids = (
                        LabSession.objects.filter(
                            session=session, assigned_count__lt=F("capacity")
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
                if result["status"] == "already_registered":
                    messages.info(request, "Participant is already registered.")
                elif result["status"] == "ok":
                    messages.success(request, "Participant registered.")
                    return redirect(
                        "registrations:register_detail", participant_id=participant.id
                    )
            else:
                result = register_participant_auto(
                    participant.id, request.user, present_absent
                )
                if result["status"] == "needs_session":
                    return redirect(
                        f"{reverse('registrations:create_session')}?next={request.path}"
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
    available_lab_ids = (
        LabSession.objects.filter(session=session, assigned_count__lt=F("capacity"))
        .exclude(lab=full_lab_session.lab)
        .values_list("lab_id", flat=True)
    )
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
            return redirect(
                f"{reverse('registrations:create_session')}?next={request.path}"
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
def export_csv(request):
    df = build_export_dataframe()
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = "attachment; filename=registrations_export.csv"
    df.to_csv(response, index=False)
    return response
