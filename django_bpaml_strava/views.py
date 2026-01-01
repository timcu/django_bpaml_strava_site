import datetime
import logging
import re
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
from django.http import HttpResponseRedirect
from django.urls import reverse
import plotly.graph_objects as go
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


def index_page(request):
    """Find all athletes """
    list_social_accounts = SocialAccount.objects.filter(
        provider='strava'
    ).exclude(
        user__last_name="DON'T USE"
    ).select_related('user').prefetch_related('user__activity_set').annotate(
        volunteered=Count('user__activity', filter=Q(user__activity__volunteer_event__isnull=False))
    ).order_by(
        Lower('user__last_name'), Lower('user__first_name')
    )
    for sa in list_social_accounts:
        if sa.user == request.user or request.user.is_staff:
            sa.is_authenticated = request.user.is_authenticated
        else:
            sa.is_authenticated = False
        fastest = None
        for a in sa.user.activity_set.all():
            if a.parkrun_duration and (fastest is None or a.parkrun_duration < fastest):
                fastest = a.parkrun_duration
            if a.strava_duration and (fastest is None or a.strava_duration < fastest):
                fastest = a.strava_duration
        sa.fastest = fastest
    context = {'athletes': list_social_accounts}
    return render(request, 'django_bpaml_strava/athletes.html', context)


def athlete_page(request, strava_id):
    """Find just the one athlete with the supplied strava id"""
    a = social_account_with_sorted_activities(strava_id=strava_id)
    context = {'athlete': a}
    return render(request, 'django_bpaml_strava/athlete.html', context)


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
        zi = zi_from_strava_timezone(dct_activity["timezone"])
        start_time = datetime.datetime.strptime(dct_activity["start_date"],"%Y-%m-%dT%H:%M:%SZ").astimezone(zi)
        start_time_local = datetime.datetime.strptime(dct_activity["start_date_local"],"%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=zi)
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
            activity_id=int(f"{parkrun_id}{dct_parkrun['date']:%Y%m%d}"),
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


@login_required
def delete_activities_admin(request):
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

        except requests.exceptions.RequestException as e:
            messages.error(request, "An unexpected error occurred. Please try again.")
            return redirect('view-activities', strava_id=strava_id)
        print(soup)
        # return redirect('view-activities', strava_id=strava_id)
        div_content = soup.find("div", {"id": "content"})
        p = div_content.find("div", {"class": "paddedt"}).p
        volunteer_links = p.find_all("a", href=True)
        volunteer_ids = [link["href"].split('/')[-1] for link in volunteer_links]
        logger.info(f"{volunteer_ids=}")
        if parkrun_id in volunteer_ids:
            div_results_header = soup.find("div", {"class": "Results-header"})
            location = div_results_header.find("h1").get_text().replace(" parkrun", "")
            span_date = div_results_header.find("span", {"class": "format-date"})
            parkrun_date = datetime.datetime.strptime(span_date.get_text(), "%d/%m/%Y").date()
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


def view_athlete_activity_chart(request, strava_id):
    # Get activities for the user
    social_account = social_account_with_sorted_activities(strava_id=strava_id)

    # Prepare data for the chart
    dates = []
    durations = []
    labels = []
    volunteer_dates = []
    volunteer_locations = []
    # If not enough datapoints for a line of best fit then plot on horizontal line through goal time
    slope = 0
    intercept = social_account.user.goal_duration.total_seconds()/60

    for activity in social_account.user.activity_set.all():
        # Get the fastest duration between the two fields
        fastest = None
        if activity.parkrun_duration and activity.strava_duration:
            fastest = min(activity.parkrun_duration, activity.strava_duration)
        elif activity.parkrun_duration:
            fastest = activity.parkrun_duration
        elif activity.strava_duration:
            fastest = activity.strava_duration

        if fastest is not None:
            dates.append(activity.date)
            total_seconds = int(fastest.total_seconds())
            durations.append(total_seconds / 60)  # Convert to minutes
            labels.append(min_sec(fastest))

        if activity.volunteer_event is not None:
            volunteer_dates.append(activity.date)
            volunteer_locations.append(activity.location)

    # Create the Plotly figure
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=dates,
        y=durations,
        mode='lines+markers+text',
        name=f'{social_account.user.get_full_name()}',
        text=labels,
        textposition='top center',
        textfont=dict(size=10),
    ))

    if len(dates) > 0 and social_account.user.goal_duration:
        shortfall = int(max(0, min(durations)*60 - social_account.user.goal_duration.total_seconds()))
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

    logger.info(f"{len(dates)=}")
    # Convert dates to numeric values (days since first date)
    x_numeric = np.array([(d - dates[0]).days for d in dates])
    y_numeric = np.array(durations)
    if len(dates) > 8:
        # Find best 8 results that show no increase in time over period

        # Find the best 8 points
        n_points = min(8, len(dates))
        lowest_slowdown_indices = []
        best_indices = None
        lowest_slowdown = datetime.timedelta(seconds=60*60)

        # Try all combinations of n_points to see which ones
        for indices in combinations(range(len(dates)), n_points):
            y_subset = y_numeric[list(indices)]

            # Calculate climb in times with this set
            y_prev = None
            slowdown = 0
            for i, y in enumerate(y_subset):
                if y_prev is not None and y_prev < y:
                    slowdown += y - y_prev
                y_prev = y

            if len(lowest_slowdown_indices) == 0 or slowdown < lowest_slowdown:
                lowest_slowdown = slowdown
                lowest_slowdown_indices = [indices]
            elif slowdown == lowest_slowdown:
                lowest_slowdown_indices.append(indices)

        # There may be several with the same score so find which ones are straightest
        best_r_squared = 0
        for indices in lowest_slowdown_indices:
            x_subset = x_numeric[list(indices)]
            y_subset = y_numeric[list(indices)]

            # Calculate R-squared for this subset
            slope, intercept, r_value, _, _ = stats.linregress(x_subset, y_subset)
            r_squared = r_value ** 2

            if r_squared > best_r_squared:
                best_r_squared = r_squared
                best_indices = indices

        # Calculate final regression with best points
        x_best = x_numeric[list(best_indices)]
        y_best = y_numeric[list(best_indices)]
    else:
        x_best = x_numeric
        y_best = y_numeric
        y_prev = None
        best_indices = range(len(dates))  # all required because haven't exceeded 8 datapoints
        # Calculate lowest_slowdown on all points since haven't exceeded 8 datapoints
        lowest_slowdown = 0
        for y in y_numeric:
            if y_prev is not None and y_prev < y:
                lowest_slowdown += y - y_prev
            y_prev = y

    if len(dates) > 1:
        slope, intercept, r_value, p_value, std_err = stats.linregress(x_best, y_best)

        # Create points for the line of best fit across entire date range
        line_x = [dates[0], dates[-1]]
        line_y = [intercept, slope * x_numeric[-1] + intercept]

        # Add line of best fit
        fig.add_trace(go.Scatter(
            x=line_x,
            y=line_y,
            mode='lines',
            name=f'Line of best fit (r={r_value:.3f}), {len(x_best)} best points',
            line=dict(dash='dash', color='green')
        ))
        # Highlight the 8 points used
        best_dates = [dates[i] for i in best_indices]
        best_durations = [durations[i] for i in best_indices]
        fig.add_trace(go.Scatter(
            x=best_dates,
            y=best_durations,
            mode='markers',
            name=f'Points used for fit (slowdown={int(lowest_slowdown*60+0.5)}s)',
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