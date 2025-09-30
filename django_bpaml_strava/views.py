from allauth.socialaccount.models import SocialAccount
from django.shortcuts import render, get_object_or_404
from django.db.models import Prefetch
from django.contrib.auth.decorators import login_required
from django_bpaml_strava.models import Activity, User


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


def social_account_with_sorted_activities(strava_id):
    social_account = (SocialAccount.objects.filter(uid=strava_id, provider='strava')
                      .select_related("user")
                      .prefetch_related(
                          Prefetch(
                            "user__activity_set",
                            queryset=Activity.objects.order_by("start_time")
                          )
                        )
                      .first())
    return social_account


@login_required
def view_activities(request, strava_id):
    # Get the stored, and possibly refreshed, access token for this athlete
    social_account = social_account_with_sorted_activities(strava_id)
    context = {'athlete': social_account}
    return render(request, 'django_bpaml_strava/athlete.html', context)
