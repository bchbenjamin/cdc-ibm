from django import forms

from .models import Lab, Participant, Session


class CSVUploadForm(forms.Form):
    csv_file = forms.FileField()


class RegistrationForm(forms.Form):
    present_absent = forms.ChoiceField(choices=Participant.PRESENT_ABSENT_CHOICES)
    lab_override = forms.ModelChoiceField(queryset=Lab.objects.none(), required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["lab_override"].queryset = Lab.objects.order_by("sort_order")


class LabChangeForm(forms.Form):
    target_lab = forms.ModelChoiceField(queryset=Lab.objects.none())

    def __init__(self, *args, current_lab=None, **kwargs):
        super().__init__(*args, **kwargs)
        queryset = Lab.objects.order_by("sort_order")
        if current_lab is not None:
            queryset = queryset.exclude(pk=current_lab.pk)
        self.fields["target_lab"].queryset = queryset


class SessionForm(forms.ModelForm):
    class Meta:
        model = Session
        fields = ["label", "start_time"]
        widgets = {
            "start_time": forms.TimeInput(attrs={"type": "time"}),
        }


class SwapForm(forms.Form):
    swap_participant = forms.ModelChoiceField(queryset=Participant.objects.none())
    target_lab = forms.ModelChoiceField(queryset=Lab.objects.none())

    def __init__(self, *args, swap_participant_queryset=None, target_lab_queryset=None, **kwargs):
        super().__init__(*args, **kwargs)
        if swap_participant_queryset is not None:
            self.fields["swap_participant"].queryset = swap_participant_queryset
        if target_lab_queryset is not None:
            self.fields["target_lab"].queryset = target_lab_queryset
