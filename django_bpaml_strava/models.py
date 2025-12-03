from django.db import models
from django.contrib.auth.models import AbstractUser

class User(AbstractUser):
    """
    Extends Django User class and provides extra field for parkrun_id
    Strava ID also required but will be handled by allauth[socialaccount]
    """
    # add extra fields to AbstractUser to make User
    # null=True means field can be empty in database
    # blank=True means field can be empty in django forms
    parkrun_id = models.BigIntegerField(null=True, blank=True)

    def __str__(self):
        return f"{self.email}<{self.first_name} {self.last_name}>"

# Need to define Activity second because it refers to User
class Activity(models.Model):
    athlete = models.ForeignKey(User, on_delete=models.CASCADE)
    activity_id = models.BigIntegerField(null=True)
    date = models.DateField("date of activity")
    start_time = models.DateTimeField("start date and time in UTC", null=True)
    start_time_local = models.DateTimeField("start date and time in local timezone", null=True, blank=True)
    timezone = models.CharField(max_length=200, default="UTC")
    title = models.CharField(max_length=200)
    location = models.CharField("Parkrun location", max_length=200)
    description = models.CharField(max_length=4000)
    parkrun_duration = models.DurationField(default=None, null=True, blank=True)
    strava_duration = models.DurationField(default=None, null=True, blank=True)
    distance = models.FloatField(default=0, null=True, blank=True)  # metres
    polyline = models.CharField(max_length=4000)
    volunteer_event = models.BigIntegerField("Event number volunteered at that parkrun location", null=True, blank=True)

    def __str__(self):
        return self.title
