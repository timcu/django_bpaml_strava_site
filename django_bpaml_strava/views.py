from allauth.socialaccount.models import SocialAccount
from django.shortcuts import render, get_object_or_404
from django_bpaml_strava.models import User

def index_page(request):
    """Find all athletes """
    list_social_accounts = SocialAccount.objects.filter(provider='strava').select_related('user')
    context = {'athletes': list_social_accounts}
    return render(request, 'django_bpaml_strava/athletes.html', context)

def athlete_page(request, strava_id):
    """Find just the one athlete with the supplied strava id"""
    a = get_object_or_404(SocialAccount, provider='strava', uid=strava_id)
    context = {'athlete': a}
    return render(request, 'django_bpaml_strava/athlete.html', context)
