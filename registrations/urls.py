from django.urls import path

from . import views

app_name = "registrations"

urlpatterns = [
    path("favicon.ico", views.favicon, name="favicon"),
    path("", views.dashboard, name="dashboard"),
    path("import/", views.import_csv, name="import_csv"),
    path("import/<int:job_id>/", views.import_progress, name="import_progress"),
    path("register/", views.register_search, name="register_search"),
    path("register/<int:participant_id>/", views.register_detail, name="register_detail"),
    path("lab/", views.lab_dashboard, name="lab_dashboard"),
    path("lab/start/", views.start_lab_session, name="start_lab_session"),
    path(
        "lab/reject/<int:participant_id>/",
        views.reject_lab_registration_view,
        name="reject_lab_registration",
    ),
    path("session/new/", views.create_session, name="create_session"),
    path("swap/", views.swap_registration_view, name="swap_registration"),
    path("export/", views.export_csv, name="export_csv"),
]
