from django.urls import path
from django_bpaml_strava.views import index_page, athlete_page, view_activities, view_athlete_activity_chart
from django_bpaml_strava.views import fetch_and_view_activities, save_activity
from django_bpaml_strava.views import delete_activity
from django_bpaml_strava.views import fetch_and_save_activities, fetch_and_save_activities_all
from django_bpaml_strava.views import fetch_and_save_parkruns_all, fetch_and_save_parkruns
from django_bpaml_strava.views import delete_activities
from django_bpaml_strava.views import delete_activities_all
from django_bpaml_strava.views import member
from django_bpaml_strava.views import volunteer
from django_bpaml_strava.views import view_athlete_activity_map
from django_bpaml_strava.views import calculate_deviation, calculate_deviations
from django_bpaml_strava.views import athletes_scores

urlpatterns = [
  path('', index_page, name='index'),
  path('athlete/<str:strava_id>/', athlete_page, name='athlete'),
  path('view-activities/athlete/<int:strava_id>', view_activities, name='view-activities'),
  path('view-chart/athlete/<int:strava_id>', view_athlete_activity_chart, name='view-athlete-chart'),
  path('view-map/athlete/<int:strava_id>/activity/<int:activity_id>', view_athlete_activity_map, name='view-activity-map'),
  path('view-unsaved-activities-available-on-strava/athlete/<int:strava_id>', fetch_and_view_activities, name='view-unsaved-activities-available-on-strava'),
  path('save-activity/athlete/<int:strava_id>/activity/<int:activity_id>', save_activity, name='save-activity'),
  path('delete-activity/athlete/<int:strava_id>/activity/<int:activity_id>', delete_activity, name='delete-activity'),
  path('save-activities/athlete/<int:strava_id>', fetch_and_save_activities, name='save-activities'),
  path('save-activities-all', fetch_and_save_activities_all, name='save-activities-all'),
  path('save-parkruns/athlete/<int:strava_id>', fetch_and_save_parkruns, name='save-parkruns'),
  path('save-parkruns-all', fetch_and_save_parkruns_all, name='save-parkruns-all'),
  path('delete-activities/athlete/<int:strava_id>', delete_activities, name='delete-activities'),
  path('delete-activities-all', delete_activities_all, name='delete-activities-all'),
  path('member-edit/', member, name='member-edit'),
  path('volunteer/<int:strava_id>', volunteer, name='volunteer'),
  path('calculate-deviation/<int:strava_id>', calculate_deviation, name='calculate-deviation'),
  path('calculate-deviations-all', calculate_deviations, name='calculate-deviations-all'),
  path('athletes-scores', athletes_scores, name='athletes-scores'),
]
