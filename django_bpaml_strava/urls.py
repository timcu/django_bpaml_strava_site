from django.urls import path
from django_bpaml_strava.views import index_page, athlete_page, view_activities
urlpatterns = [
  path('', index_page, name='index'),
  path('athlete/<str:strava_id>/', athlete_page, name='athlete'),
  path('view-activities/athlete/<int:strava_id>', view_activities, name='view-activities'),
]
