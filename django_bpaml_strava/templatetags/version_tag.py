from django import template
from django_bpaml_strava.version import version

register = template.Library()

@register.simple_tag
def app_version():
    return version
