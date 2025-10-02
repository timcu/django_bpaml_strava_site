# Standard libraries
import datetime
import logging
# Third-party libraries
from allauth.socialaccount.models import SocialToken, SocialAccount, SocialApp
import requests

logger = logging.getLogger(__name__)


def fetch_strava_token(strava_id):
    """Retrieves token from database and, if expired, refreshes using Strava api"""
    social_account = SocialAccount.objects.get(uid=strava_id, provider='strava')
    logger.info(f"Found athlete {social_account.uid}")
    # Check if user has any social tokens
    social_tokens = SocialToken.objects.filter(account=social_account)
    logger.info(f"Found {len(social_tokens)} social tokens")
    # Get the stored access token for this athlete. social_tokens should actually only be one token
    strava_token = social_tokens.first()
    if strava_token.expires_at <= datetime.datetime.now(datetime.timezone.utc):
        logger.info(f"token expired at {strava_token.expires_at} - refreshing")
        # https://developers.strava.com/docs/authentication/ "Refreshing Expired Access Tokens"
        token_url = 'https://www.strava.com/api/v3/oauth/token'
        strava_app = SocialApp.objects.get(provider='strava')
        payload = {
            'client_id': strava_app.client_id,
            'client_secret': strava_app.secret,
            'grant_type': 'refresh_token',
            'refresh_token': strava_token.token_secret
        }
        response = requests.post(token_url, data=payload)
        if response.ok:
            new_tokens = response.json()
            # Update the api_key dictionary with the new access and refresh tokens and expiry time
            strava_token.token = new_tokens['access_token']
            strava_token.token_secret = new_tokens['refresh_token']
            strava_token.expires_at = datetime.datetime.fromtimestamp(new_tokens['expires_at'], datetime.timezone.utc)
            logger.info("Token refreshed successfully.")
            # Save the refreshed key for a later session
            strava_token.save()
        else:
            logger.info(f"Error refreshing token: {response.status_code} - {response.text}")
    return strava_token
