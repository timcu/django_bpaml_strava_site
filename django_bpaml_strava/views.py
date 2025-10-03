import datetime
import logging
import requests
import zoneinfo
from allauth.socialaccount.models import SocialAccount
from django.shortcuts import render, get_object_or_404, redirect
from django.db.models import Prefetch
from django.contrib.auth.decorators import login_required
from django_bpaml_strava.models import Activity, User

from django_bpaml_strava.strava_token import fetch_strava_token

logger = logging.getLogger(__name__)
BASE_TZ = zoneinfo.ZoneInfo("Australia/Brisbane")

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


def fetch_activities_from_strava(strava_id):
    """Fetch all the strava activities for a user in first three months of current year without saving them"""
    social_token = fetch_strava_token(strava_id)

    year = datetime.datetime.now(BASE_TZ).year
    after = int(datetime.datetime(year, 1, 1, 0, 0, tzinfo=BASE_TZ).timestamp())
    before = int(datetime.datetime(year, 4, 1, 0, 0, tzinfo=BASE_TZ).timestamp())

    # Define the endpoint and headers
    url = 'https://www.strava.com/api/v3/athlete/activities'
    headers = {'Authorization': f'Bearer {social_token.token}'}

    # Define parameters for the request
    params = {
        'before': before,
        'after': after,
        'page': 1,
        'per_page': 200,
    }

    # Make the GET request with parameters
    response = requests.get(url, headers=headers, params=params)

    # Check if the request was successful
    if response.ok:
        activity_data = response.json()
        logger.info(f"Athlete activities for authorized user: {len(activity_data)}")
        return activity_data
    else:
        logger.warning(f"Error requesting activities from strava {response.status_code}: {response.text}")
        return None


@login_required
def fetch_and_view_activities(request, strava_id):
    """Fetch activities from strava, no filtering, and display asking user which ones should be saved

    Triggered by URL 'bpaml-strava/show-unsaved-activities-available-on-strava'
    """
    list_strava_activities = fetch_activities_from_strava(strava_id)
    if list_strava_activities is None:
        return index_page(request)
    social_account = social_account_with_sorted_activities(strava_id)
    # omit the ones already saved
    set_activity_id = set(a.activity_id for a in social_account.user.activity_set.all())
    logger.info(f"{set_activity_id=}")
    for i in range(len(list_strava_activities)-1, 0, -1):
        if list_strava_activities[i]['id'] in set_activity_id:
            del list_strava_activities[i]
    list_new_strava_activities = [d for d in list_strava_activities if d['id'] not in set_activity_id]
    # display the results
    context = {'athlete': social_account, 'list_strava_activities': list_new_strava_activities}
    return render(request, 'django_bpaml_strava/athlete.html', context)


def create_activity_from_strava(user: User, dct_activity):
    """
    Given the json data for a single activity from Strava (already converted to a dict)
    create an Activity record linked to the correct User and save in the database.
    """
    list_timezone = dct_activity['timezone'].split(' ')
    timezone = list_timezone[1]
    start_time = datetime.datetime.strptime(dct_activity["start_date"], "%Y-%m-%dT%H:%M:%SZ").astimezone(zoneinfo.ZoneInfo(timezone))
    start_time_local = datetime.datetime.strptime(dct_activity["start_date_local"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=zoneinfo.ZoneInfo(timezone))
    start_date = start_time.date()
    logger.info(f'{start_time:%d-%b-%Y %H:%M} {start_time_local:%d-%b-%Y %H:%M %z} {dct_activity["distance"] / 1000:6.1f}km {dct_activity["name"]}')
    activity = Activity.objects.create(
        athlete=user,
        activity_id=dct_activity["id"],
        date=start_date,
        start_time=start_time,
        start_time_local=start_time_local.replace(tzinfo=None),
        timezone=timezone,
        distance=dct_activity["distance"],
        title=dct_activity["name"],
        strava_duration=datetime.timedelta(seconds=dct_activity["elapsed_time"]),
        polyline=dct_activity["map"]["summary_polyline"],
    )
    activity.save()


@login_required
def save_activity(request, strava_id, activity_id):
    social_account = social_account_with_sorted_activities(strava_id)
    social_token = fetch_strava_token(strava_id)
    # Define the endpoint and headers to fetch a single activity
    url = f'https://www.strava.com/api/v3/activities/{activity_id}'
    headers = {'Authorization': f'Bearer {social_token.token}'}

    # Define parameters for the request. We don't need all efforts for this activity
    params = {
        'include_all_efforts': False,
    }

    # Make the GET request with parameters for single activity
    response = requests.get(url, headers=headers, params=params)

    # Check if the request was successful
    if response.ok:
        dct_activity = response.json()
        logger.info(f"Activity for authorized user: {dct_activity}")
        # add activity to user
        create_activity_from_strava(social_account.user, dct_activity)
    else:
        logger.warning(f"Error requesting activities from strava {response.status_code}: {response.text}")
    # requery db to include new activity
    return redirect("view-activities", strava_id=strava_id)


@login_required
def delete_activity(request, strava_id, activity_id):
    social_account = social_account_with_sorted_activities(strava_id)
    for a in social_account.user.activity_set.all():
        if a.activity_id == activity_id:
            a.delete()
            logger.info(f"Deleted activity {a.activity_id}")
            break
        logger.info(f"activity {a.activity_id} != {activity_id}")
    else:
        logger.error("Activity not found")
    return redirect('view-activities', strava_id=strava_id)

