import json
import pathlib
import sys
from django.core.management.base import BaseCommand
from allauth.socialaccount.models import SocialApp

STRAVA_KEY_PATH = pathlib.Path("strava-key.json")

def fetch_strava_key() -> dict[str, str | int]:
    """Fetches api key from file"""
    try:
        with STRAVA_KEY_PATH.open("r") as json_file:
            dct_api_key = json.load(json_file)
        return dct_api_key
    except FileNotFoundError:
        sys.exit(f'{STRAVA_KEY_PATH.resolve()} not found')


class Command(BaseCommand):
    help = 'Create social applications from key file or environment variables'

    def handle(self, *args, **options):
        strava_api_key = fetch_strava_key()
        social_apps = [
            {
                'provider': 'strava',
                'name': 'strava',
                'client_id': strava_api_key.get('client_id'),
                'secret': strava_api_key.get('client_secret'),
                'key': '',
                'settings': {},
            }
        ]

        for app_data in social_apps:
            if not app_data['client_id'] or not app_data['secret']:
                self.stdout.write(
                    self.style.ERROR(
                        f'Missing keys for {app_data["provider"]} in {STRAVA_KEY_PATH.resolve()}. {strava_api_key.get("client_id")=} {strava_api_key.get("client_secret")=}'
                    )
                )
                continue

            social_app, created = SocialApp.objects.get_or_create(
                provider=app_data['provider'],
                defaults={
                    'name': app_data['name'],
                    'client_id': app_data['client_id'],
                    'secret': app_data['secret'],
                    'key': app_data.get('key', ''),
                    'settings': app_data.get('settings', {}),
                }
            )

            if created:
                self.stdout.write(self.style.SUCCESS(f'Created social app: {app_data["name"]}'))
            else:
                social_app.client_id = app_data['client_id']
                social_app.secret = app_data['secret']
                social_app.save()
                self.stdout.write(self.style.WARNING(f'Updated social app: {app_data["name"]}'))
