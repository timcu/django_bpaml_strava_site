from django.urls import path
from django_bpaml_strava.views import index_page, athlete_page, view_activities
from django_bpaml_strava.views import fetch_and_view_activities, save_activity
from django_bpaml_strava.views import delete_activity
from django_bpaml_strava.views import fetch_and_save_activities, fetch_and_save_parkruns
from django_bpaml_strava.views import delete_activities
from django_bpaml_strava.views import delete_activities_admin
from django_bpaml_strava.views import member
from django_bpaml_strava.views import volunteer

urlpatterns = [
  path('', index_page, name='index'),
  path('athlete/<str:strava_id>/', athlete_page, name='athlete'),
  path('view-activities/athlete/<int:strava_id>', view_activities, name='view-activities'),
  path('view-unsaved-activities-available-on-strava/athlete/<int:strava_id>', fetch_and_view_activities, name='view-unsaved-activities-available-on-strava'),
  path('save-activity/athlete/<int:strava_id>/activity/<int:activity_id>', save_activity, name='save-activity'),
  path('delete-activity/athlete/<int:strava_id>/activity/<int:activity_id>', delete_activity, name='delete-activity'),
  path('save-activities/athlete/<int:strava_id>', fetch_and_save_activities, name='save-activities'),
  path('save-parkruns/athlete/<int:strava_id>', fetch_and_save_parkruns, name='save-parkruns'),
  path('delete-activities/athlete/<int:strava_id>', delete_activities, name='delete-activities'),
  path('delete-activities-all', delete_activities_admin, name='delete-activities-all'),
  path('member-edit/', member, name='member-edit'),
  path('volunteer/<int:strava_id>', volunteer, name='volunteer'),
]
