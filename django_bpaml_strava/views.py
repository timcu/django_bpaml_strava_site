import datetime
import logging
import requests
import zoneinfo
from allauth.socialaccount.models import SocialAccount
from bs4 import BeautifulSoup
from django.shortcuts import render, get_object_or_404, redirect
from django.db.models import Prefetch
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseRedirect
from django.urls import reverse

from django_bpaml_strava.models import Activity
from django_bpaml_strava.strava_token import fetch_strava_token
from django_bpaml_strava.version import version

logger = logging.getLogger(__name__)
BASE_TZ = zoneinfo.ZoneInfo("Australia/Brisbane")

def index_page(request):
    """Find all athletes """
    list_social_accounts = SocialAccount.objects.filter(provider='strava').select_related('user')
    for sa in list_social_accounts:
        if sa.user == request.user or request.user.is_staff:
            sa.is_authenticated = request.user.is_authenticated
        else:
            sa.is_authenticated = False
    context = {'athletes': list_social_accounts}
    return render(request, 'django_bpaml_strava/athletes.html', context)


def athlete_page(request, strava_id):
    """Find just the one athlete with the supplied strava id"""
    a = social_account_with_sorted_activities(strava_id=strava_id)
    context = {'athlete': a}
    return render(request, 'django_bpaml_strava/athlete.html', context)


def social_account_with_sorted_activities(strava_id):
    social_account = (SocialAccount.objects.filter(uid=strava_id, provider='strava')
                      .select_related("user")
                      .prefetch_related(
                          Prefetch(
                            "user__activity_set",
                            queryset=Activity.objects.order_by("date")
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
        for a in activity_data:
            if 'start_date_local' in a:
                a['start_time_local'] = datetime.datetime.strptime(a["start_date_local"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=None)
                logger.debug(f"{a['start_date_local']=} on {a['start_time_local']=}")
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


def create_activity_from_strava(social_account: SocialAccount, dct_activity):
    """
    Given the json data for a single activity from Strava (already converted to a dict)
    create an Activity record linked to the correct User and save in the database.
    Checks first if an activity exists for that date and modifies it if so.
    """
    # timezone looks like '(GMT+10:00) Australia/Brisbane'
    list_timezone = dct_activity['timezone'].split(' ')
    timezone = list_timezone[1]
    start_time = datetime.datetime.strptime(dct_activity["start_date"], "%Y-%m-%dT%H:%M:%SZ").astimezone(zoneinfo.ZoneInfo(timezone))
    start_time_local = datetime.datetime.strptime(dct_activity["start_date_local"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=zoneinfo.ZoneInfo(timezone))
    start_date = start_time.date()
    logger.info(f'{start_time:%d-%b-%Y %H:%M} {start_time_local:%d-%b-%Y %H:%M %z} {dct_activity["distance"] / 1000:6.1f}km {dct_activity["name"]}')
    dct_activity_by_date = {a.date: a for a in social_account.user.activity_set.all()}
    if start_date in dct_activity_by_date:
        logger.info("updating existing activity")
        a = dct_activity_by_date[start_date]
        a.athlete=social_account.user
        a.activity_id=dct_activity["id"]
        a.start_time=start_time
        a.start_time_local=start_time_local.replace(tzinfo=None)
        a.timezone=timezone
        a.distance=dct_activity["distance"]
        a.title=dct_activity["name"]
        a.strava_duration=datetime.timedelta(seconds=dct_activity["elapsed_time"])
        a.polyline=dct_activity["map"]["summary_polyline"]
        a.device_name=dct_activity.get("device_name", "no device")
    else:
        logger.info("creating new activity")
        a = Activity.objects.create(
            athlete=social_account.user,
            activity_id=dct_activity["id"],
            date=start_date,
            start_time=start_time,
            start_time_local=start_time_local.replace(tzinfo=None),
            timezone=timezone,
            distance=dct_activity["distance"],
            title=dct_activity["name"],
            strava_duration=datetime.timedelta(seconds=dct_activity["elapsed_time"]),
            polyline=dct_activity["map"]["summary_polyline"],
            device_name=dct_activity.get("device_name", "no device"),
        )
    a.save()
    return a


@login_required
def save_activity(request, strava_id, activity_id):
    social_account: SocialAccount = social_account_with_sorted_activities(strava_id)
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
        create_activity_from_strava(social_account, dct_activity)
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


@login_required
def fetch_and_save_activities(request, strava_id):
    """Fetch activities from strava, filter out non-parkrun events and save the rest if nothing else
    already saved for that date"""
    lst_strava_activities = fetch_activities_from_strava(strava_id)
    if lst_strava_activities is None:
        return index_page(request)
    social_account = social_account_with_sorted_activities(strava_id)
    set_saturday = set(a.date for a in social_account.user.activity_set.all() if len(str(a.activity_id)) > 8)
    for dct_activity in lst_strava_activities:
        # timezone looks like '(GMT+10:00) Australia/Brisbane'
        list_timezone = dct_activity['timezone'].split(' ')
        timezone = list_timezone[1]
        start_time = datetime.datetime.strptime(
            dct_activity["start_date"],
            "%Y-%m-%dT%H:%M:%SZ").astimezone(zoneinfo.ZoneInfo(timezone))
        start_time_local = datetime.datetime.strptime(
            dct_activity["start_date_local"],
            "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=zoneinfo.ZoneInfo(timezone))
        start_date = start_time_local.date()
        latest_start_time = start_time.replace(hour=7, minute=10, second=0, microsecond=0)
        # Find last start on each Saturday before 7:10am local time that is between 4.7km and 5.3km
        if start_date not in set_saturday and start_time.weekday() == 5 and start_time < latest_start_time and 4700 < dct_activity["distance"] < 5300:
            create_activity_from_strava(social_account, dct_activity)
            set_saturday.add(start_date)
    return redirect('view-activities', strava_id=strava_id)


@login_required
def fetch_and_save_parkruns(request, strava_id):
    """Fetch all the parkrun results for a user in first three months of current year without saving them"""
    social_account = social_account_with_sorted_activities(strava_id)
    parkrun_id = social_account.user.parkrun_id
    url = f"https://www.parkrun.com.au/parkrunner/{parkrun_id}/all/"

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5',
        'Accept-Encoding': 'gzip, deflate, br',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1'
    }

    response = requests.get(url, headers=headers, timeout=10)
    try:
        # fetch html from parkrun or raise timeout exception if no response
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'lxml')
    except requests.exceptions.Timeout:
        messages.error(request, "The request timed out. Please try again later.")
        return redirect('view-activities', strava_id=strava_id)

    except requests.exceptions.HTTPError as e:
        if response.status_code == 404:
            messages.error(request, f"{response.status_code} The requested page was not found.")
        elif response.status_code == 403:
            messages.error(request, f"{response.status_code} Access to this resource is forbidden.")
        elif response.status_code >= 500:
            messages.error(request, f"{response.status_code} The server is experiencing issues. Please try again later.")
        else:
            messages.error(request, f"{response.status_code} An error occurred: {e}")
        return redirect('view-activities', strava_id=strava_id)

    except requests.exceptions.ConnectionError:
        messages.error(request, "Unable to connect to the server. Please check your internet connection.")
        return redirect('view-activities', strava_id=strava_id)

    except requests.exceptions.RequestException as e:
        messages.error(request, "An unexpected error occurred. Please try again.")
        return redirect('view-activities', strava_id=strava_id)

    year = datetime.datetime.now(BASE_TZ).year
    after = datetime.date(year, 1, 1)
    before = datetime.date(year, 4, 1)

    tables = soup.find_all("table")
    for table in tables:
        caption_text = table.find("caption").get_text()
        if caption_text and caption_text.strip() == "All  Results":
            t = table
            break
    else:
        messages.info(request, "No parkrun results found")
        return redirect('view-activities', strava_id=strava_id)

    trs = t.tbody.find_all("tr")
    messages.info(request, f"Found {len(trs)} parkrun results")
    logger.info(f"Found {len(trs)} parkrun results")
    dct_parkruns_by_date = {}
    for tr in trs:
        tds = tr.find_all("td")
        parkrun_date = datetime.datetime.strptime(tds[1].get_text(), "%d/%m/%Y").date()
        if after <= parkrun_date < before and parkrun_date.weekday() == 5:
            time_parts = tds[4].get_text().split(":")
            part_seconds = 1
            total_seconds = 0
            # total the seconds, minutes and, if exists, hours
            for t in time_parts[::-1]:
                total_seconds += int(t) * part_seconds
                part_seconds *= 60
            dct_parkrun = {"location": tds[0].get_text(),
                           "date": parkrun_date,
                           "parkrun_duration": datetime.timedelta(seconds=total_seconds)}
            dct_parkruns_by_date[parkrun_date] = dct_parkrun
            logger.info(f"Save dict {dct_parkrun=}")
        else:
            logger.debug(f"Reject Location: {tds[0].get_text()}, Date: {parkrun_date}, Time: {tds[4].get_text()} {before<=parkrun_date=} {after>parkrun_date=}")

    social_account = social_account_with_sorted_activities(strava_id)
    for a in social_account.user.activity_set.all():
        if a.date in dct_parkruns_by_date:
            dct_parkrun = dct_parkruns_by_date.pop(a.date)
            logger.info(f"Link {dct_parkrun['date']=} {dct_parkrun['location']} to {a.date=} ")
            a.location = dct_parkrun["location"]
            a.parkrun_duration = dct_parkrun["parkrun_duration"]
            a.save()
    for dct_parkrun in dct_parkruns_by_date.values():
        logger.info(f"Create {dct_parkrun['date']=} {dct_parkrun['location']}")
        a = Activity.objects.create(
            athlete=social_account.user,
            activity_id=int(f"{dct_parkrun['date']:%Y%m%d}"),
            date=dct_parkrun["date"],
            parkrun_duration=dct_parkrun["parkrun_duration"],
            location=dct_parkrun["location"],
            distance=5000,
        )
        a.save()
    return redirect('view-activities', strava_id=strava_id)


@login_required
def delete_activities(request, strava_id):
    social_account = social_account_with_sorted_activities(strava_id)
    social_account.user.activity_set.all().delete()
    return redirect('view-activities', strava_id=strava_id)


@login_required()
def member(request):
    """Update member details for currently logged-in user

    First name
    Last name
    Parkrun ID
    Goal Duration
    """
    if request.method == "POST":
        user = request.user
        user.first_name = request.POST.get("first-name", user.first_name)
        user.last_name = request.POST.get("last-name", user.last_name)
        user.parkrun_id = request.POST.get("parkrun-id") or None  # converts empty string to None
        if request.POST.get("goal-minutes") == "":
            user.goal_duration = None
        else:
            seconds = int(request.POST.get("goal-minutes")) * 60
            if request.POST.get("goal-seconds") != "":
                seconds += int(request.POST.get("goal-seconds"))
            user.goal_duration = datetime.timedelta(seconds=int(seconds))
        user.save()
        return HttpResponseRedirect(reverse("index"))
    else:
        if request.user.goal_duration is None:
            goal_minutes = 25
            goal_seconds = 0
        else:
            goal_minutes = int(request.user.goal_duration.total_seconds() // 60)
            goal_seconds = int(request.user.goal_duration.total_seconds() % 60)
        context = {"goal_minutes": goal_minutes, "goal_seconds": goal_seconds}
        return render(request, "django_bpaml_strava/member.html", context=context)
