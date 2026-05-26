from datetime import time

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.db.models import F
from django.db.models.signals import post_delete, post_migrate, post_save
from django.dispatch import receiver

from .models import Lab, LabSession, Participant, Session

LAB_NAMES = ["A1", "A2", "A3", "A4", "A5", "A11", "A12", "A13", "A14", "A15"]
DEFAULT_SESSION_LABEL = "Session 1"
DEFAULT_SESSION_TIME = time(9, 0)
REGISTRATION_STAFF_GROUP = "Registration staff"


@receiver(post_migrate)
def seed_defaults(sender, **kwargs):
    if sender.name != "registrations":
        return

    for order, name in enumerate(LAB_NAMES, start=1):
        Lab.objects.get_or_create(name=name, defaults={"sort_order": order})

    if not Session.objects.exists():
        Session.objects.create(label=DEFAULT_SESSION_LABEL, start_time=DEFAULT_SESSION_TIME)

    User = get_user_model()
    if not User.objects.filter(username="admin").exists():
        User.objects.create_superuser("admin", password="admin")

    participant_type = ContentType.objects.get_for_model(Participant)
    session_type = ContentType.objects.get_for_model(Session)
    group, _ = Group.objects.get_or_create(name=REGISTRATION_STAFF_GROUP)
    permissions = Permission.objects.filter(
        content_type=participant_type,
        codename__in=["register_participant", "change_participant_lab"],
    ) | Permission.objects.filter(
        content_type=session_type,
        codename="add_session",
    )
    group.permissions.add(*permissions)


@receiver(post_save, sender=Session)
def ensure_lab_sessions(sender, instance, **kwargs):
    labs = Lab.objects.all()
    for lab in labs:
        LabSession.objects.get_or_create(
            lab=lab,
            session=instance,
            defaults={"capacity": 20, "assigned_count": 0},
        )


@receiver(post_delete, sender=Participant)
def decrement_counts_on_delete(sender, instance, **kwargs):
    if not instance.registered_at or not instance.session_id or not instance.lab_id:
        return
    LabSession.objects.filter(
        session_id=instance.session_id,
        lab_id=instance.lab_id,
        assigned_count__gt=0,
    ).update(assigned_count=F("assigned_count") - 1)
    Session.objects.filter(
        id=instance.session_id,
        assigned_count__gt=0,
    ).update(assigned_count=F("assigned_count") - 1)
