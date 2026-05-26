from django.conf import settings
from django.db import models


class Lab(models.Model):
    name = models.CharField(max_length=4, unique=True)
    sort_order = models.PositiveSmallIntegerField()

    def __str__(self):
        return self.name


class Session(models.Model):
    label = models.CharField(max_length=100)
    start_time = models.TimeField()
    capacity = models.PositiveIntegerField(default=200)
    assigned_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.label} ({self.start_time})"


class LabSession(models.Model):
    lab = models.ForeignKey(Lab, on_delete=models.CASCADE)
    session = models.ForeignKey(Session, on_delete=models.CASCADE)
    capacity = models.PositiveIntegerField(default=20)
    assigned_count = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = ("lab", "session")

    def __str__(self):
        return f"{self.lab.name} - {self.session.label}"


class Participant(models.Model):
    PRESENT = "present"
    ABSENT = "absent"
    PRESENT_ABSENT_CHOICES = [
        (PRESENT, "Present"),
        (ABSENT, "Absent"),
    ]

    sl_number = models.CharField(max_length=50, db_index=True)
    zone = models.CharField(max_length=100, blank=True)
    candidate_full_name = models.TextField()
    present_absent = models.CharField(max_length=10, choices=PRESENT_ABSENT_CHOICES, blank=True)
    lab = models.ForeignKey(Lab, null=True, blank=True, on_delete=models.SET_NULL)
    session = models.ForeignKey(Session, null=True, blank=True, on_delete=models.SET_NULL)
    signature = models.CharField(max_length=255, blank=True, default="")
    registered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )
    registered_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.sl_number} - {self.candidate_full_name}"

    class Meta:
        permissions = [
            ("register_participant", "Can register participants"),
            ("change_participant_lab", "Can change participant lab allocation"),
        ]


class AuditLog(models.Model):
    class Action(models.TextChoices):
        REGISTERED = "registered", "Registered"
        SWAP = "swap", "Swap"

    participant = models.ForeignKey(Participant, on_delete=models.CASCADE)
    performed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    action = models.CharField(max_length=20, choices=Action.choices, default=Action.REGISTERED)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.action} - {self.participant.sl_number}"


class ImportJob(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PROCESSING = "processing", "Processing"
        COMPLETE = "complete", "Complete"
        FAILED = "failed", "Failed"

    csv_text = models.TextField()
    original_filename = models.CharField(max_length=255, blank=True)
    next_row = models.PositiveIntegerField(default=0)
    processed_count = models.PositiveIntegerField(default=0)
    total_rows = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    last_error = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Import {self.id} - {self.status}"
