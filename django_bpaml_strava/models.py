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
    parkrun_id = models.IntegerField(null=True, blank=True)

    def __str__(self):
        return f"{self.email}<{self.first_name} {self.last_name}>"

# Need to define Activity second because it refers to User
class Activity(models.Model):
    athlete = models.ForeignKey(User, on_delete=models.CASCADE)
    activity_id = models.IntegerField(null=True)
    date = models.DateField("date of activity")
    start_time = models.DateTimeField("start date and time in UTC")
    start_time_local = models.DateTimeField("start date and time in local timezone", null=True, blank=True)
    timezone = models.CharField(max_length=200, default="UTC")
    title = models.CharField(max_length=200)
    location = models.CharField(max_length=200)  # parkrun location
    description = models.CharField(max_length=4000)
    parkrun_duration = models.DurationField(default=None, null=True, blank=True)
    strava_duration = models.DurationField(default=None, null=True, blank=True)
    distance = models.FloatField(default=0)  # metres
    polyline = models.CharField(max_length=4000)

    def __str__(self):
        return self.title
