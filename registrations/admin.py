from django import forms
from django.contrib import admin

from .models import AuditLog, ImportJob, Lab, LabCoordinator, LabSession, Participant, Session
from .services import count_csv_rows, process_import_chunk


class ImportJobAdminForm(forms.ModelForm):
    csv_file = forms.FileField(required=False)

    class Meta:
        model = ImportJob
        fields = ["csv_file"]

    def save(self, commit=True):
        job = super().save(commit=False)
        upload = self.cleaned_data.get("csv_file")
        if upload:
            job.csv_text = upload.read().decode("utf-8-sig")
            job.original_filename = upload.name
            job.status = ImportJob.Status.PENDING
            job.next_row = 0
            job.processed_count = 0
            job.total_rows = count_csv_rows(job.csv_text)
            job.last_error = ""
        if commit:
            job.save()
        return job


@admin.action(description="Process next CSV chunk")
def process_next_chunk(modeladmin, request, queryset):
    for job in queryset:
        process_import_chunk(job)


@admin.register(ImportJob)
class ImportJobAdmin(admin.ModelAdmin):
    form = ImportJobAdminForm
    list_display = ("id", "original_filename", "status", "processed_count", "total_rows")
    list_filter = ("status",)
    actions = [process_next_chunk]
    readonly_fields = (
        "status",
        "processed_count",
        "total_rows",
        "next_row",
        "last_error",
        "created_at",
        "created_by",
    )
    fieldsets = (
        ("Upload", {"fields": ("csv_file",)}),
        (
            "Status",
            {
                "fields": (
                    "status",
                    "processed_count",
                    "total_rows",
                    "next_row",
                    "last_error",
                    "created_at",
                    "created_by",
                )
            },
        ),
    )


@admin.register(Lab)
class LabAdmin(admin.ModelAdmin):
    list_display = ("name", "sort_order")
    search_fields = ("name",)
    ordering = ("sort_order",)


@admin.register(Session)
class SessionAdmin(admin.ModelAdmin):
    list_display = ("label", "start_time", "capacity", "assigned_count")
    ordering = ("created_at",)


@admin.register(LabSession)
class LabSessionAdmin(admin.ModelAdmin):
    list_display = ("lab", "session", "assigned_count", "capacity")
    list_filter = ("session",)
    ordering = ("session", "lab__sort_order")


@admin.register(LabCoordinator)
class LabCoordinatorAdmin(admin.ModelAdmin):
    list_display = ("user", "lab", "created_at")
    list_filter = ("lab",)
    search_fields = ("user__username", "user__first_name", "user__last_name", "user__email")
    autocomplete_fields = ("user", "lab")


@admin.register(Participant)
class ParticipantAdmin(admin.ModelAdmin):
    list_display = (
        "regular_sl_number",
        "sl_number",
        "candidate_full_name",
        "zone",
        "lab",
        "session",
    )
    search_fields = ("regular_sl_number", "sl_number", "candidate_full_name")
    list_filter = ("session", "lab")


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("participant", "action", "performed_by", "created_at")
    list_filter = ("action", "performed_by")
    search_fields = ("participant__sl_number", "participant__candidate_full_name")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
