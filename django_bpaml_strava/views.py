import datetime
import json
import logging
import re
import statistics
from math import isnan
from zoneinfo import ZoneInfoNotFoundError
from itertools import combinations

import requests
import zoneinfo
from allauth.socialaccount.models import SocialAccount
from bs4 import BeautifulSoup
from django.shortcuts import render, redirect
from django.db.models import Prefetch, Count, Q
from django.db.models.functions import Lower
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseRedirect, Http404
from django.urls import reverse
import folium
import polyline
import plotly.graph_objects as go
from requests import HTTPError
from scipy import stats
import numpy as np

from django_bpaml_strava.models import Activity
from django_bpaml_strava.strava_token import fetch_strava_token

logger = logging.getLogger(__name__)
BASE_TZ = zoneinfo.ZoneInfo("Australia/Brisbane")
# Headers required by parkrun to validate source
HEADERS_PARKRUN = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.5',
    'Accept-Encoding': 'gzip, deflate, br',
    'Connection': 'keep-alive',
    'Upgrade-Insecure-Requests': '1'
}


def list_active_strava_accounts():
    return SocialAccount.objects.filter(
        provider='strava'
    ).exclude(
        user__last_name="DON'T USE"
    ).select_related('user').prefetch_related('user__activity_set').annotate(
        volunteered=Count('user__activity', filter=Q(user__activity__volunteer_event__isnull=False))
    ).order_by(
        Lower('user__last_name'), Lower('user__first_name')
    )


def index_page(request):
    """Find all athletes """
    list_social_accounts = list_active_strava_accounts()
    for sa in list_social_accounts:
        if sa.user == request.user or request.user.is_staff:
            sa.is_authenticated = request.user.is_authenticated
        else:
            sa.is_authenticated = False
        fastest = None
        sa.num_strava = 0
        sa.num_parkrun = 0
        sa.latest_strava = None
        sa.latest_parkrun = None
        for a in sa.user.activity_set.all():
            if a.parkrun_duration:
                sa.num_parkrun += 1
                if sa.latest_parkrun is None or a.date > sa.latest_parkrun:
                    sa.latest_parkrun = a.date
                if fastest is None or a.parkrun_duration < fastest:
                   fastest = a.parkrun_duration
            if a.strava_duration:
                sa.num_strava += 1
                if sa.latest_strava is None or a.date > sa.latest_strava:
                    sa.latest_strava = a.date
                if fastest is None or a.strava_duration < fastest:
                   fastest = a.strava_duration
        sa.fastest = fastest
    context = {'athletes': list_social_accounts}
    return render(request, 'django_bpaml_strava/athletes.html', context)


@login_required
def calculate_deviations(request):
    """Fetch full activity for every strava activity for all active strava id so that splits and standard deviations can be calculated"""
    list_social_accounts = list_active_strava_accounts()
    for social_account in list_social_accounts:
        for activity in social_account.user.activity_set.all():
            if activity.strava_duration and activity.activity_id and activity.strava_json is None:
                save_activity_from_id(social_account.uid, activity.activity_id)
    return redirect("athletes-scores")


def athletes_scores(request):
    """Find all athletes scores and render sorted by place"""
    list_social_accounts = list_active_strava_accounts()
    for sa in list_social_accounts:
        sa.score = score_for_athlete(sa)
    list_social_accounts = sorted(list_social_accounts, key=lambda a: a.score.get('total', -1000), reverse=True)
    for i, sa in enumerate(list_social_accounts):
        sa.score['place'] = i + 1
    context = {'athletes': list_social_accounts}
    return render(request, 'django_bpaml_strava/athletes_scores.html', context)


def athlete_page(request, strava_id):
    """Find just the one athlete with the supplied strava id"""
    a = social_account_with_sorted_activities(strava_id=strava_id)
    context = {'athlete': a}
    return render(request, 'django_bpaml_strava/athlete.html', context)


def pace_std_dev(activity: Activity):
    if activity is None:
        return {"lst_pace": [], "std_deviation": 0, "html_deviation": "", "strava_activity": {}}
    lst_pace = []
    std_deviation = 0
    html_deviation = ""
    strava_activity = {}
    if hasattr(activity, "strava_json") and activity.strava_json is not None:
        strava_activity = json.loads(activity.strava_json)
        for i, split in enumerate(strava_activity.get('splits_metric', [])):
            speed = split.get('average_speed', 0)
            if speed and i < 5:
                lst_pace.append(1000 / speed)
        if len(lst_pace) > 1:
            std_deviation = statistics.stdev(lst_pace)
            lst_pace_formatted = [min_sec(p) for p in lst_pace]
            html_deviation = "<div>" + ", ".join(lst_pace_formatted) + f"<br>std dev {min_sec(std_deviation)}</div>"
    return {"lst_pace": lst_pace, "std_deviation": std_deviation, "html_deviation": html_deviation, "strava_activity": strava_activity}


def social_account_with_sorted_activities(strava_id: str):
    social_account = (SocialAccount.objects.filter(uid=strava_id, provider='strava')
                      .select_related("user")
                      .prefetch_related(
                          Prefetch(
                            "user__activity_set",
                            queryset=Activity.objects.order_by("date")
                          )
                        )
                      .first())
    for a in social_account.user.activity_set.all():
        if a.volunteer_event is not None:
            location = re.sub(r'[^a-z]', '', a.location.lower())
            a.volunteer_url = f"https://www.parkrun.com.au/{location}/results/{a.volunteer_event}/"
        dct = pace_std_dev(a)
        a.html_deviation = dct["html_deviation"]
        a.strava_activity = dct["strava_activity"]
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
    list_new_strava_activities = [d for d in list_strava_activities if d['id'] not in set_activity_id]
    for a in list_new_strava_activities:
        if 'start_date_local' in a:
            a['start_time_local'] = datetime.datetime.strptime(a["start_date_local"], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=None)
            logger.debug(f"{a['start_date_local']=} on {a['start_time_local']=}")
    # display the results
    context = {'athlete': social_account, 'list_strava_activities': list_new_strava_activities}
    return render(request, 'django_bpaml_strava/athlete.html', context)


def create_activity_from_strava(social_account: SocialAccount, dct_activity):
    """
    Given the JSON data for a single activity from Strava (already converted to a dict)
    create an Activity record linked to the correct User and save in the database.
    Checks first if an activity exists for that date and modifies it if so.
    """
    # timezone looks like '(GMT+10:00) Australia/Brisbane'
    zi = zi_from_strava_timezone(dct_activity["timezone"])
    start_time = datetime.datetime.strptime(dct_activity["start_date"], "%Y-%m-%dT%H:%M:%SZ").astimezone(zi)
    start_time_local = datetime.datetime.strptime(dct_activity["start_date_local"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=zi)
    start_date = start_time.date()
    logger.info(f'{start_time:%d-%b-%Y %H:%M} {start_time_local:%d-%b-%Y %H:%M %z} {dct_activity["distance"] / 1000:6.1f}km {dct_activity["name"]}')
    dct_activity_by_date = {a.date: a for a in social_account.user.activity_set.all()}
    if 'splits_metric' in dct_activity:
        try:
            strava_json = json.dumps(dct_activity)
        except TypeError:
            # probably caused by datetime in dct_activity
            logger.warning(f"Could not serialize activity json for {dct_activity}")
            strava_json = None
    else:
        strava_json = None
    if start_date in dct_activity_by_date:
        logger.info("updating existing activity")
        a = dct_activity_by_date[start_date]
        a.athlete=social_account.user
        a.activity_id=dct_activity["id"]
        a.start_time=start_time
        a.start_time_local=start_time_local.replace(tzinfo=None)
        a.timezone=str(zi)
        a.distance=dct_activity["distance"]
        a.title=dct_activity["name"]
        a.strava_duration=datetime.timedelta(seconds=dct_activity["elapsed_time"])
        a.polyline=dct_activity["map"]["summary_polyline"]
        a.device_name=dct_activity.get("device_name", "no device")
        if strava_json is not None:
            a.strava_json=strava_json
    else:
        logger.info("creating new activity")
        a = Activity.objects.create(
            athlete=social_account.user,
            activity_id=dct_activity["id"],
            date=start_date,
            start_time=start_time,
            start_time_local=start_time_local.replace(tzinfo=None),
            timezone=str(zi),
            distance=dct_activity["distance"],
            title=dct_activity["name"],
            strava_duration=datetime.timedelta(seconds=dct_activity["elapsed_time"]),
            polyline=dct_activity["map"]["summary_polyline"],
            device_name=dct_activity.get("device_name", "no device"),
            strava_json=strava_json,
        )
    a.save()
    return a


def save_activity_from_id(strava_id, activity_id):
    social_account: SocialAccount = social_account_with_sorted_activities(strava_id)
    social_token = fetch_strava_token(strava_id)
    # Define the endpoint and headers to fetch a single activity
    url = f'https://www.strava.com/api/v3/activities/{activity_id}'
    headers = {'Authorization': f'Bearer {social_token.token}'}

    # Define parameters for the request. We don't need all efforts for this activity
    params = {
        'include_all_efforts': True,
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
    return


@login_required
def save_activity(request, strava_id, activity_id):
    save_activity_from_id(strava_id, activity_id)
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


def save_activities(strava_id, list_strava_activities):
    social_account = social_account_with_sorted_activities(strava_id)
    set_saturday = set(a.date for a in social_account.user.activity_set.all() if len(str(a.activity_id)) > 8)
    for dct_activity in list_strava_activities:
        # timezone looks like '(GMT+10:00) Australia/Brisbane'
        zi = zi_from_strava_timezone(dct_activity["timezone"])
        start_time = datetime.datetime.strptime(dct_activity["start_date"],"%Y-%m-%dT%H:%M:%SZ").astimezone(zi)
        start_time_local = datetime.datetime.strptime(dct_activity["start_date_local"],"%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=zi)
        start_date = start_time_local.date()
        latest_start_time = start_time.replace(hour=7, minute=10, second=0, microsecond=0)
        # Find last start on each Saturday before 7:10am local time that is between 4.7km and 5.3km
        if start_date not in set_saturday and start_time.weekday() == 5 and start_time < latest_start_time and 4700 < dct_activity["distance"] < 5300:
            create_activity_from_strava(social_account, dct_activity)
            set_saturday.add(start_date)


@login_required
def fetch_and_save_activities(request, strava_id):
    """Fetch activities from strava, filter out non-parkrun events and save the rest if nothing else
    already saved for that date"""
    lst_strava_activities = fetch_activities_from_strava(strava_id)
    if lst_strava_activities is None:
        return index_page(request)
    save_activities(strava_id, lst_strava_activities)
    return redirect('view-activities', strava_id=strava_id)


@login_required
def fetch_and_save_activities_all(request):
    """Fetch activities from strava, filter out non-parkrun events and save the rest if nothing else
    already saved for that date for all users. Requires admin access"""
    if request.user.is_superuser:
        list_social_accounts = list_active_strava_accounts()
        for sa in list_social_accounts:
            list_strava_activities = fetch_activities_from_strava(sa.uid)
            if list_strava_activities:
                save_activities(sa.uid, list_strava_activities)
        messages.info(request, "All users' activities fetched")
    else:
        messages.info(request, "Superuser access required to fetch all users' activities")
    return redirect('index')


def fetch_parkruns(request, social_account):
    """Fetch all the parkrun results for a user in first three months of current year without saving them"""
    parkrun_id = social_account.user.parkrun_id
    if parkrun_id is None:
        messages.error(request, f"Parkrun id not found for user {social_account.user.get_full_name()}")
        raise TypeError(f"Parkrun id not found for user {social_account.user.get_full_name()}")
    url = f"https://www.parkrun.com.au/parkrunner/{parkrun_id}/all/"

    response = requests.get(url, headers=HEADERS_PARKRUN, timeout=10)
    try:
        # fetch HTML from parkrun or raise timeout exception if no response
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'lxml')
    except requests.exceptions.Timeout:
        messages.error(request, "The request timed out. Please try again later.")
        raise

    except requests.exceptions.HTTPError as e:
        if response.status_code == 404:
            messages.error(request, f"{response.status_code} The requested page was not found. {url}")
        elif response.status_code == 403:
            messages.error(request, f"{response.status_code} Access to this resource is forbidden. {url}")
        elif response.status_code >= 500:
            messages.error(request, f"{response.status_code} The server is experiencing issues. Please try again later. {url}")
        else:
            messages.error(request, f"{response.status_code} An error occurred: {url} {e}")
        raise

    except requests.exceptions.ConnectionError:
        messages.error(request, "Unable to connect to the server. Please check your internet connection.")
        raise

    except requests.exceptions.RequestException:
        messages.error(request, "An unexpected error occurred. Please try again.")
        raise

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
        raise ValueError("No parkrun results found in html")

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
    return dct_parkruns_by_date


def create_activity_from_parkrun(social_account: SocialAccount, dct_parkrun):
    """
    Given the JSON data for a single activity from Strava (already converted to a dict)
    create an Activity record linked to the correct User and save in the database.
    Checks first if an activity exists for that date and modifies it if so.
    """
    dct_activity_by_date = {a.date: a for a in social_account.user.activity_set.all()}
    if dct_parkrun["date"] in dct_activity_by_date:
        a = dct_activity_by_date[dct_parkrun["date"]]
        logger.info(f"Link {dct_parkrun['date']=} {dct_parkrun['location']} to {a.date=} ")
        a.location = dct_parkrun["location"]
        a.parkrun_duration = dct_parkrun["parkrun_duration"]
    else:
        logger.info(f"Create {dct_parkrun['date']=} {dct_parkrun['location']}")
        a = Activity.objects.create(
            athlete=social_account.user,
            activity_id=int(f"{social_account.user.parkrun_id}{dct_parkrun['date']:%Y%m%d}"),
            date=dct_parkrun["date"],
            parkrun_duration=dct_parkrun["parkrun_duration"],
            location=dct_parkrun["location"],
            distance=5000,
        )
    a.save()
    return a


@login_required
def fetch_and_save_parkruns(request, strava_id):
    """Fetch all the parkrun results for a user in first three months of current year and save them"""
    social_account = social_account_with_sorted_activities(strava_id)
    try:
        dct_parkruns_by_date = fetch_parkruns(request, social_account)
    except (TypeError, requests.exceptions.Timeout, HTTPError, requests.exceptions.ConnectionError, requests.exceptions.RequestException):
        return redirect('view-activities', strava_id=strava_id)
    for dct_parkrun in dct_parkruns_by_date.values():
        create_activity_from_parkrun(social_account, dct_parkrun)
    return redirect('view-activities', strava_id=strava_id)


@login_required
def fetch_and_save_parkruns_all(request):
    """Delete activities for all users - requires admin access"""
    if request.user.is_superuser:
        list_social_accounts = list_active_strava_accounts()
        for sa in list_social_accounts:
            try:
                dct_parkruns_by_date = fetch_parkruns(request, sa)
                for dct_parkrun in dct_parkruns_by_date.values():
                    create_activity_from_parkrun(sa, dct_parkrun)
            except (TypeError, requests.exceptions.Timeout, HTTPError, requests.exceptions.ConnectionError,
                    requests.exceptions.RequestException):
                pass
        messages.info(request, "All users' parkruns fetched and saved")
    else:
        messages.info(request, "Superuser access required to fetch all users' parkruns")
    return redirect('index')


@login_required
def delete_activities(request, strava_id):
    social_account = social_account_with_sorted_activities(strava_id)
    social_account.user.activity_set.all().delete()
    return redirect('view-activities', strava_id=strava_id)


@login_required
def delete_activities_all(request):
    """Delete activities for all users - requires admin access"""
    if request.user.is_superuser:
        list_social_accounts = SocialAccount.objects.filter(
            provider='strava'
        ).select_related('user').prefetch_related('user__activity_set')
        for sa in list_social_accounts:
            sa.user.activity_set.all().delete()
        messages.info(request, "All users' activities deleted")
    else:
        messages.info(request, "Superuser access required to delete all users' activities")
    return redirect('index')


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


@login_required()
def volunteer(request, strava_id):
    """Update member details for currently logged-in user

    First name
    Last name
    Parkrun ID
    Goal Duration
    """
    social_account = social_account_with_sorted_activities(strava_id)
    if request.method == "POST":
        parkrun_id = str(social_account.user.parkrun_id)
        location = request.POST.get("volunteer-location")
        # Convert to lower case and remove all characters which are not letter of the alphabet
        location = re.sub(r'[^a-z]', '', location.lower())
        # If event number missing (empty str), check latest results
        event = request.POST.get("volunteer-event") or "latestresults"
        url = f"https://www.parkrun.com.au/{location}/results/{event}/"
        logger.info(f"Volunteer {location=} {event=} {url=}")
        response = requests.get(url, headers=HEADERS_PARKRUN, timeout=10)
        try:
            # fetch HTML from parkrun or raise timeout exception if no response
            response.raise_for_status()
            soup = BeautifulSoup(response.content, 'lxml')
        except requests.exceptions.Timeout:
            messages.error(request, "The request timed out. Please try again later.")
            return redirect('view-activities', strava_id=strava_id)

        except requests.exceptions.HTTPError as e:
            if response.status_code == 404:
                messages.error(request, f"{response.status_code} The requested page was not found. {url}")
            elif response.status_code == 403:
                messages.error(request, f"{response.status_code} Access to parkrun is forbidden.")
            elif response.status_code >= 500:
                messages.error(request, f"{response.status_code} The parkrun server is experiencing issues. Please try again later.")
            else:
                messages.error(request, f"{response.status_code} An error occurred: {e}")
            return redirect('view-activities', strava_id=strava_id)

        except requests.exceptions.ConnectionError:
            messages.error(request, "Unable to connect to parkrun server. Please check your internet connection.")
            return redirect('view-activities', strava_id=strava_id)

        except requests.exceptions.RequestException:
            messages.error(request, "An unexpected error occurred. Please try again.")
            return redirect('view-activities', strava_id=strava_id)
        print(soup)
        # return redirect('view-activities', strava_id=strava_id)
        div_content = soup.find("div", {"id": "content"})
        table = div_content.find("table", {"class": "Volunteers-table"})
        volunteer_links = table.find_all("a", href=True)
        volunteer_ids = [link["href"].split('/')[-1] for link in volunteer_links if "parkrunner" in link["href"]]
        logger.info(f"{volunteer_ids=}")
        if parkrun_id in volunteer_ids:
            div_results_header = soup.find("div", {"class": "Results-header"})
            location = div_results_header.find("h1").get_text().replace(" parkrun", "")
            span_date = div_results_header.find("span", {"class": "format-date"})
            parkrun_date = datetime.datetime.strptime(span_date.get_text(), "%Y-%m-%d").date()
            span_event = div_results_header.find_all("span")[-1]
            volunteer_event = int(span_event.get_text().replace("#", ""))
            for a in social_account.user.activity_set.all():
                if parkrun_date == a.date:
                    a.location = location
                    a.volunteer_event = volunteer_event
                    a.save()
                    break
            else:
                a = Activity.objects.create(
                    athlete=social_account.user,
                    activity_id=int(f"{parkrun_id}{parkrun_date:%Y%m%d}"),
                    date=parkrun_date,
                    volunteer_event=volunteer_event,
                    location=location,
                )
                a.save()
        else:
            messages.warning(request, "You are not listed as a volunteer at that event.")
        return redirect('view-activities', strava_id=strava_id)
    else:
        context = {"athlete": social_account}
        return render(request, "django_bpaml_strava/volunteer.html", context=context)


def zi_from_strava_timezone(strava_timezone) -> zoneinfo.ZoneInfo | datetime.timezone:
    list_timezone = strava_timezone.split(' ')
    timezone_name = list_timezone[1]
    try:
        return zoneinfo.ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        logger.warning(f"Timezone not found for {strava_timezone} in strava activity")
        # Parse the offset from the string
        match = re.search(r'GMT([+-])(\d+):(\d+)', list_timezone[0])
        if match:
            sign = 1 if match.group(1) == '+' else -1
            hours = int(match.group(2))
            minutes = int(match.group(3))
            offset = datetime.timedelta(hours=sign * hours, minutes=sign * minutes)
            logger.info(f"Using offset {offset}")
            return datetime.timezone(offset)
        else:
            # Fallback to UTC if parsing fails
            logger.warning("Using UTC")
            return datetime.timezone.utc


def min_sec(duration) -> str:
    if not duration:
        return "0:00"
    if hasattr(duration, "total_seconds"):
        total_seconds = int(duration.total_seconds())
    else:
        total_seconds = int(duration)
    return f"{total_seconds // 60}:{total_seconds % 60:02d}"


SLOWDOWN_FACTOR = 1
RUN_FACTOR = 20
VOLUNTEER_FACTOR = 50
SANDBAGGING_FACTOR = 100
STRAIGHT_FACTOR = 15
DEVIANT_FACTOR = 0.2
STRAVA_FACTOR = 0
NEGATIVE_SPLIT_FACTOR = 4
GOAL_FACTOR = 1
GOAL_BONUS = 30

def score_for_dates(goal_duration: datetime.timedelta, volunteers: int, dates: list[datetime.date], durations: list[float], splits: list[list[float]]):
    # Set best_dates to dates etc so that if this is the highest score then values will be ready to be used
    dct_score: dict[str, list[datetime.date]|list[float]|float|int] = {
        "best_dates": dates,
        "best_durations": durations,
    }
    # Set volunteer score
    dct_score.update({
        "volunteers": volunteers,
        "score_volunteer": VOLUNTEER_FACTOR if volunteers > 0 else 0
    })
    # Set score for number of runs completed
    dct_score.update({"num_runs": len(durations), "score_num_runs": len(durations) * RUN_FACTOR})
    # Fastest time
    shortfall = max(0.0, int((min(durations)) * 60 + 0.5) -  goal_duration.total_seconds())
    dct_score.update({"shortfall": shortfall, "score_shortfall": -shortfall * GOAL_FACTOR, "score_goal_bonus": GOAL_BONUS if shortfall == 0 else 0})

    # Calculate climb in times with this set
    y_prev = None
    slowdown = 0
    for i, y in enumerate(durations):
        if y_prev is not None and y_prev < y:
            slowdown += y - y_prev
        y_prev = y
    dct_score.update({"slowdown": slowdown * 60, "score_slowdown": -slowdown * 60 * SLOWDOWN_FACTOR})

    days = [(d - dates[0]).days for d in dates]
    slope, intercept, r_value, _, _ = stats.linregress(days, durations)
    if isnan(r_value):
        r_value = 0
        slope = 0
        intercept = durations[0] if len(durations) > 0 else goal_duration.total_seconds() / 60
    r_squared = r_value ** 2
    dct_score.update({"sandbagging_slope": slope, "score_sandbagging_slope": slope * SANDBAGGING_FACTOR if slope < 0 else 0})
    dct_score.update({"best_fit_intercept": intercept})
    dct_score.update({"r_value": r_value, "score_r_value": r_squared * STRAIGHT_FACTOR})

    # Calculate standard deviation of splits
    score_strava = 0
    num_strava = 0
    score_negative_split = 0
    num_negative_split = 0
    score_deviant = 0
    sum_deviant = 0
    for lst_pace in splits:
        if len(lst_pace) > 3:
            split_prev = None
            num_strava += 1
            for s in lst_pace:
                if split_prev is not None and split_prev > s:
                    num_negative_split += 1
                split_prev = s
            # Calculate standard deviation for splits
            sum_deviant += statistics.stdev(lst_pace)
    dct_score.update({"num_strava": num_strava, "score_num_strava": num_strava * STRAVA_FACTOR})
    dct_score.update({"num_negative_split": num_negative_split, "score_num_negative_split": num_negative_split * NEGATIVE_SPLIT_FACTOR})
    # Maximum deviant penalty is bonuses from strava so suppliers of strava data are not penalised for supplying data
    dct_score.update({"sum_deviant": sum_deviant, "score_deviant": -min(sum_deviant * DEVIANT_FACTOR, num_strava * STRAVA_FACTOR + num_negative_split * NEGATIVE_SPLIT_FACTOR)})
    total = 0
    for k, v in dct_score.items():
        if k.startswith("score"):
            total += v
    dct_score.update({"total": total})
    return dct_score


def score_for_athlete(social_account) -> dict[str, list[datetime.date|int|float|str] | int | float] :
    # Prepare data for scoring and charting
    dates: list[datetime.date] = []  # parkrun dates where athlete ran
    durations: list[float] = []  # number of minutes
    splits: list[list[float]] = []  # list of splits for each parkrun
    volunteer_dates: list[datetime.date] = []  # parkrun dates where athlete volunteered
    volunteer_locations: list[str] = []  # locations where athlete volunteered
    # slope = 0
    # intercept = social_account.user.goal_duration.total_seconds()/60
    sa = social_account_with_sorted_activities(social_account.uid)
    for activity in sa.user.activity_set.all():
        # Get the fastest duration between the measurements from strava and parkrun
        fastest = activity.get_fastest()
        if fastest is not None:
            dates.append(activity.date)
            total_seconds = int(fastest.total_seconds())
            durations.append(total_seconds / 60)  # Convert to minutes
        if activity.volunteer_event is not None:
            # Can't volunteer and run on same day
            location = re.sub(r'[^a-z]', '', activity.location.lower())
            activity.volunteer_url = f"https://www.parkrun.com.au/{location}/results/{activity.volunteer_event}/"
            volunteer_dates.append(activity.date)
            volunteer_locations.append(activity.location)
        dct = pace_std_dev(activity)
        splits.append(dct["lst_pace"])

    days: list[int] = [(d - dates[0]).days for d in dates]  # number of days since first date

    if len(dates) > 8:
        # Find best 8 results that show no increase in time over period

        # Find the best 8 points
        n_points = min(8, len(dates))
        highest_indices = None
        highest_score = {"total": 0}

        # Try all combinations of n_points to see which ones
        for indices in combinations(range(len(dates)), n_points):
            # subset_days = [days[i] for i in indices]
            subset_dates = [dates[i] for i in indices]
            subset_durations = [durations[i] for i in indices]
            subset_splits = [splits[i] for i in indices]
            dct_score = score_for_dates(
                social_account.user.goal_duration,
                len(volunteer_dates),
                subset_dates,
                subset_durations,
                subset_splits
            )

            if highest_indices is None or dct_score["total"] > highest_score["total"]:
                highest_score = dct_score
                highest_indices = indices
    else:
        highest_score = score_for_dates(
            social_account.user.goal_duration,
            len(volunteer_dates),
            dates,
            durations,
            splits
        )
        highest_indices = list(range(len(days)))
    return {
        "dates": dates,
        "days": days,
        "durations": durations,
        "best_dates": [dates[i] for i in highest_indices],
        "best_days": [days[i] for i in highest_indices],
        "best_durations": [durations[i] for i in highest_indices],
        "volunteer_dates": volunteer_dates,
        "volunteer_locations": volunteer_locations,
    } | highest_score


@login_required()
def view_athlete_activity_chart(request, strava_id):
    # Get activities for the user
    social_account = social_account_with_sorted_activities(strava_id=strava_id)

    # Prepare data for the chart
    dct_score = score_for_athlete(social_account)
    dates = dct_score["dates"]
    days: list[int] = dct_score["days"]
    durations = dct_score["durations"]
    labels = []
    volunteer_dates = dct_score["volunteer_dates"]
    volunteer_locations = dct_score["volunteer_locations"]
    # If not enough datapoints for a line of best fit then plot on horizontal line through goal time
    slope = dct_score["sandbagging_slope"]
    intercept = dct_score["best_fit_intercept"]  # social_account.user.goal_duration.total_seconds()/60
    shortfall: int = dct_score["shortfall"]  # number of seconds shortfall
    slowdown: float = dct_score["slowdown"]  # number of seconds slowdown
    r_value = dct_score["r_value"]

    for activity in social_account.user.activity_set.all():
        # Get the fastest duration between the two measurements strava and parkrun
        fastest = activity.get_fastest()
        if fastest is not None:
            if activity.location:
                labels.append(activity.location + "<br>" + min_sec(fastest))
            else:
                labels.append(min_sec(fastest))

    # Create the Plotly figure
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=dates,
        y=durations,
        mode='markers+text',
        name='All runs',  # f'{social_account.user.get_full_name()}',
        text=labels,
        textposition='top center',
        textfont=dict(size=10),
    ))

    if len(dates) > 0 and social_account.user.goal_duration:
        if len(dates) == 1:
            # Single data point for goal time
            fig.add_trace(go.Scatter(
                x=[dates[0]],
                y=[social_account.user.goal_duration.total_seconds() / 60] * 1,
                mode='lines',
                name=f'Goal {min_sec(social_account.user.goal_duration)} (shortfall={shortfall}s)',
            ))
        else:
            # horizontal line for goal time
            fig.add_trace(go.Scatter(
                x=[dates[0], dates[-1]],
                y=[social_account.user.goal_duration.total_seconds() / 60] * 2,
                mode='lines',
                name=f'Goal {min_sec(social_account.user.goal_duration)} (shortfall={shortfall}s)',
            ))

    # Convert dates to numeric values (days since first date)
    best_dates = dct_score["best_dates"]
    best_durations = dct_score["best_durations"]
    if len(dates) > 1:
        # Create points for the line of best fit across entire date range
        line_x = [dates[0], dates[-1]]
        line_y = [intercept, slope * days[-1] + intercept]

        # Add line of best fit
        fig.add_trace(go.Scatter(
            x=line_x,
            y=line_y,
            mode='lines',
            name=f'Line of best fit (r={r_value:.3f}), {len(best_dates)} best points',
            line=dict(dash='dash', color='green')
        ))
        # Highlight the 8 points used
        fig.add_trace(go.Scatter(
            x=best_dates,
            y=best_durations,
            mode='lines+markers',
            name=f'Points used for fit (slowdown={int(slowdown+0.5)}s)',
            marker=dict(size=12, color='green', symbol='circle-open', line=dict(width=2))
        ))

    if len(volunteer_locations) > 0:
        # Highlight volunteered
        volunteer_durations = [slope * (d - dates[0]).days + intercept for d in volunteer_dates]
        fig.add_trace(go.Scatter(
            x=volunteer_dates,
            y=volunteer_durations,
            text=volunteer_locations,
            textposition='top center',
            mode='markers+text',
            name=f'Volunteered {len(volunteer_dates)} times',
            marker=dict(size=12, line_color='midnightblue', color='lightskyblue', symbol='triangle-down-dot', line=dict(width=2))
        ))

    # Set up x-axis to show every Saturday
    if dates:
        first_date = dates[0]
        fig.update_xaxes(
            tick0=first_date,
            dtick=7 * 24 * 60 * 60 * 1000,  # 7 days in milliseconds
            tickformat='%Y-%m-%d',
            tickangle=-45
        )

    fig.update_layout(
        title=f'Activity times {social_account.user.get_full_name()}',
        xaxis_title='Date',
        yaxis_title='Parkrun time (minutes)',
        hovermode='x unified',
        width=1200,
        height=600,
    )

    cdn = {'include_plotlyjs': 'cdn'}

    context = {
        'athlete': social_account,
        'chart_htmls': [fig.to_html(config={"responsive": True}, full_html=False, default_width='100%', **cdn)]
    }

    return render(request, 'django_bpaml_strava/athlete_chart.html', context)


@login_required()
def view_athlete_activity_map(request, strava_id, activity_id):
    # Get activities for the user
    social_account = social_account_with_sorted_activities(strava_id=strava_id)
    for activity in social_account.user.activity_set.all():
        if activity.activity_id == activity_id:
            break
    else:
        raise Http404()

    figure = folium.Figure()
    if activity.polyline:
        # Decode the polyline
        decoded_coords = polyline.decode(activity.polyline)
        # Create a map centered around the center of the polyline
        if decoded_coords:
            max_lat = max(decoded_coords, key=lambda x: x[0])[0]
            min_lat = min(decoded_coords, key=lambda x: x[0])[0]
            max_lon = max(decoded_coords, key=lambda x: x[1])[1]
            min_lon = min(decoded_coords, key=lambda x: x[1])[1]
            map_center = (max_lat + min_lat) / 2, (max_lon + min_lon) / 2

            m = folium.Map(location=[map_center[0], map_center[1]],
                           width=1200, height=600,
                           zoom_start=15, tiles="OpenStreetMap"
                           )
            # Add the polyline to the map
            folium.PolyLine(decoded_coords, color="blue", weight=2.5, opacity=1).add_to(m)

            m.add_to(figure)

    # Render and send to template
    figure.render()
    context = {
        'athlete': social_account,
        'activity': activity,
        'map': figure,
    }
    return render(request, 'django_bpaml_strava/athlete_map.html', context)


@login_required
def calculate_deviation(request, strava_id):
    """Fetch full activity for every strava activity for this strava id so that splits and standard deviations can be calculated"""
    social_account = social_account_with_sorted_activities(strava_id)
    for activity in social_account.user.activity_set.all():
        if activity.strava_duration and activity.activity_id and activity.strava_json is None:
            save_activity_from_id(strava_id, activity.activity_id)
    return redirect("view-activities", strava_id=strava_id)
