from django.urls import path
from django_bpaml_strava.views import index_page

urlpatterns = [
  path('', index_page, name='index'),
]
