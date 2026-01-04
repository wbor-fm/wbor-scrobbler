"""
This is a script to get up-to-date track information from a Spinitron page for a radio station, 
and "scrobble" that information to a user's Last.fm profile (allowing the user to record the
songs they are listening to on the radio station).

This functionality requires prerequsites of an API key and API secret from Last.fm, as well as a 
Spinitron API key associated with the desired station. These should be entered into the .env file 
with the variable names LASTFM_API_KEY, LASTFM_API_SECRET, and SPINITRON_API_KEY.

Before starting scrobbling, there must also be a web service session established with an associated
session key. If one has not already been obtained, the script should be run with the --setup flag
to establish one. The session key should be entered into the .env file with the variable name 
LASTFM_SESSION_KEY.
"""

import argparse
import hashlib
import os
import signal
import sys
import time
import json
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
import requests as r
from dateutil import parser, tz
from dotenv import load_dotenv, set_key

LASTFM_API_URL = "https://ws.audioscrobbler.com/2.0/"
SPINITRON_API_URL = "https://spinitron.com/api"


class Colors:
    """
    Pretty terminal colors for logging
    """

    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    MAGENTA = "\033[95m"
    CYAN = "\033[96m"
    WHITE = "\033[97m"
    RESET = "\033[0m"


ERROR_CODES = [16, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 16, 26, 29]
"""
According to the Last.fm documentation:
16 : The service is temporarily unavailable, please try again.
2  : Invalid service - This service does not exist
3  : Invalid Method - No method with that name in this package
4  : Authentication Failed - You do not have permissions to access the service
5  : Invalid format - This service doesn't exist in that format
6  : Invalid parameters - Your request is missing a required parameter
7  : Invalid resource specified
8  : Operation failed - Something else went wrong
9  : Invalid session key - Please re-authenticate
10 : Invalid API key - You must be granted a valid key by last.fm
11 : Service Offline - This service is temporarily offline. Try again later.
13 : Invalid method signature supplied
16 : There was a temporary error processing your request. Please try again
26 : Suspended API key - Access for your account has been suspended, please contact Last.fm
29 : Rate limit exceeded - Your IP has made too many requests in a short period
"""

# Pull .env variables and set headers
load_dotenv(dotenv_path="/env/.env")
lastfm_api_key = os.getenv("LASTFM_API_KEY")
lastfm_api_secret = os.getenv("LASTFM_API_SECRET")
lastfm_session_key = os.getenv("LASTFM_SESSION_KEY")
spinitron_api_key = os.getenv("SPINITRON_API_KEY")
spinitron_headers = {"Authorization": f"Bearer {spinitron_api_key}"}

# Parse schedule
with open("schedule.json", "r") as f:
    config = json.load(f)

start_hour = config.get("start_hour")
end_hour = config.get("end_hour")


def signal_handler(sig, frame):
    """
    Ctrl+C handler
    """
    print(
        Colors.RED + "\nCtrl+C pressed, aborting application. Goodbye!" + Colors.RESET
    )
    sys.exit(0)


def generate_signature(params):
    """
    Takes parameters for a request and generates an md5 hash signature as specified in the Last.fm
    authentication specs

    Args:
        params (library): Library of stringe representing the parameters for the request
    Returns:
        str: The generated signature, as a string
    """
    signature = ""
    for key in sorted(params):
        signature += key + str(params[key])
    signature += lastfm_api_secret
    return hashlib.md5(signature.encode("utf-8")).hexdigest()


def get_token():
    """
    Performs API call auth.getToken to fetch a request token

    Returns:
        str: Returned token, as a string
    """
    params = {"method": "auth.getToken", "api_key": lastfm_api_key}
    params["api_sig"] = generate_signature(params)

    response = r.post(LASTFM_API_URL, params=params)
    root = ET.fromstring(response.content)
    return root.find("./token").text


def get_session_key(token):
    """
    Performs API call to auth.getSession to create a web service session and fetch the associated
    session key

    Args:
        token (str): A request token, retrived from auth.getToken
    Returns:
        str: Returned session key, as a string
    """
    params = {"method": "auth.getSession", "api_key": lastfm_api_key, "token": token}
    params["api_sig"] = generate_signature(params)

    response = r.post(LASTFM_API_URL, params=params)
    root = ET.fromstring(response.content)
    session_key_element = root.find("./session/key")
    if session_key_element is None:
        print(
            '\nSession key not returned from Last.fm. Did you open the link above and press "yes, allow access?" Aborting setup.'
        )
        sys.exit(0)

    session_key = session_key_element.text
    return session_key


def get_sleep_duration(start_hour):
    """
    Gets the remaining time in seconds until start_hour to sleep until
    """
    current_datetime = datetime.now(timezone.utc)
    desired_time = current_datetime.replace(
        hour=start_hour, minute=0, second=0, microsecond=0
    )
    if desired_time < current_datetime:
        # If the desired start time is already passed for today,
        # set it for the next day
        desired_time += datetime.timedelta(days=1)
    return (desired_time - current_datetime).total_seconds()


def update_np(session_key, artist, track, album=None, duration=None):
    """
    Performs API call to track.updateNowPlaying to indicate to Last.fm that the user has started
    listening to a new track

    Args:
        session_key (str): A session key for an associated web service session generated by
            auth.getSession
        artist (str): The artist name
        track (str): The track name
        album (str, optional): The album name
        duration (int, optional): The length of the track in seconds
    Returns:
        int: Status code of the response
    """

    params = {
        "method": "track.updateNowPlaying",
        "artist": artist,
        "track": track,
        "api_key": lastfm_api_key,
        "sk": session_key,
    }
    if album:
        params["album"] = album
    if duration:
        params["duration"] = duration
    params["api_sig"] = generate_signature(params)

    response = r.post(LASTFM_API_URL, params=params)

    # Handle http error if necessary
    if not response.ok:
        handle_lastfm_http_error(response=response, request_type="NP")

    return response.status_code


def request_scrobble(session_key, artist, track, timestamp, album=None, duration=None):
    """
    Performs API call to track.scrobble to indicate to Last.fm that the user has listened to a song

    Args:
        session_key (str): A session key for an associated web service session generated by
            auth.getSession
        artist (str): The artist name
        track (str): The track name
        timestamp (float): The time the track started playing (UTC), in UNIX timestamp format
        album (str, optional): The album name
        duration (int, optional): The length of the track in seconds
    Returns:
        int: Status code of the response
    """
    params = {
        "method": "track.scrobble",
        "artist": artist,
        "track": track,
        "timestamp": timestamp,
        "api_key": lastfm_api_key,
        "sk": session_key,
    }
    if album:
        params["album"] = album
    if duration:
        params["duration"] = duration
    params["api_sig"] = generate_signature(params)

    response = r.post(LASTFM_API_URL, params=params)

    # Handle http error if necessary
    if not response.ok:
        handle_lastfm_http_error(response=response, request_type="scrobble")

    return response.status_code


def handle_lastfm_http_error(response, request_type):
    """
    Helper function for update_np and request_scrobble, which takes the returned response from an
    HTTP error, parses, and logs the information.

    Args:
        response (requests.Response): Response object that is returned by an HTTP request.
            Should only be responses where response.ok is false (status code >= 400)
        request_type (str): A string indicating the source of the HTTP error ('NP' if it comes
            from an NP request, 'scrobble' if it comes from a scrobble request)
    """
    http_error_str = (
        Colors.RED
        + f"An HTTP error occured while making a {request_type} request.\nHTTP error code {response.status_code}: {response.reason}"
        + Colors.RESET
    )
    try:
        # Get error info sent from last.fm if available
        root = ET.fromstring(response.content)
        data = root.find("./error")
        http_error_str += f"\nLast.fm error code {data.get('code')}: {data.text}"
    except ET.ParseError:
        http_error_str += "\nCould not parse response data for more information."

    print(http_error_str)


def setup():
    """
    Execution to run when the user has not established a web service session

    Returns:
        str: Established session key
    """

    token = get_token()

    # Prompt user to authorize for their account
    link = f"http://www.last.fm/api/auth/?api_key={lastfm_api_key}&token={token}"
    print(
        f'\nYou need to authorize this application with your Last.fm account. To do so, visit the following link. Click "yes, allow access." \n\n{link}\n'
    )

    # Wait for user to confirm that they have authorized before proceeding
    confirmation = ""
    print(
        "Please enter 'y' and then press \"Enter\" after you have authorized this application with your Last.fm account:"
    )
    while confirmation != "y":
        confirmation = input()
        if confirmation.lower() != "y":
            print(Colors.RED + "Did not receive 'y' - aborting setup..." + Colors.RESET)
            sys.exit(0)

    session_key = get_session_key(token)
    print(Colors.GREEN + "\nSuccess!" + Colors.RESET)

    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open("/env/setup_done", "w") as setup_done_file:
        setup_done_file.write(f"Setup completed at {current_time}\n")

    return session_key


def run():
    """
    Execution to run when the user has already established a web service session
    """
    timestamp_string = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    print("-------------------------------------")
    print("|                                   |")
    print("|     #   #  ###    ###   ####      |")
    print("|     #   #  #  #  #   #  #   #     |")
    print("|     #   #  ###   #   #  ####      |")
    print("|     # # #  #  #  #   #  #  #      |")
    print("|      # #   ###    ###   #   #     |")
    print("|                                   |")
    print("|              91.1 FM              |")
    print("|             wbor.org              |")
    print("-------------------------------------")
    print(Colors.GREEN + f"STARTUP @ {timestamp_string}" + Colors.RESET)
    print("\nSchedule:")
    print(f"START scrobbling at : {start_hour}:00 UTC")
    print(f"STOP scrobbling at  : {end_hour}:00 UTC\n")
    print("-------------------------------------")

    # Loop - each iteration is a check to Spinitron for new song data. All paths have
    # blocking of at least 5 seconds to avoid sending too many requests
    miss_count = 0
    last_spin_id = None
    while True:
        timestamp_string = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Get most recent spin info from Spinitron
        current_spin = r.get(
            f"{SPINITRON_API_URL}/spins?count=1", headers=spinitron_headers
        ).json()["items"][0]
        playlist_response = r.get(
            f"{SPINITRON_API_URL}/playlists/{current_spin['playlist_id']}",
            headers=spinitron_headers,
        )
        current_playlist = playlist_response.json()

        # Handle API errors or missing data
        if playlist_response.status_code != 200 or "title" not in current_playlist:
            print(
                Colors.YELLOW
                + f"\n---------{timestamp_string}---------\nWARNING: Could not fetch playlist data (status={playlist_response.status_code}). Response: {current_playlist}\nRetrying in 10 seconds...\n"
                + Colors.RESET
            )
            time.sleep(10)
            continue

        current_playlist_title = current_playlist["title"]
        current_playlist_category = current_playlist["category"]

        persona_response = r.get(
            f"{SPINITRON_API_URL}/personas/{current_playlist['persona_id']}",
            headers=spinitron_headers,
        )
        current_persona = persona_response.json()

        # Handle API errors or missing data
        if persona_response.status_code != 200 or "name" not in current_persona:
            print(
                Colors.YELLOW
                + f"\n---------{timestamp_string}---------\nWARNING: Could not fetch persona data (status={persona_response.status_code}). Response: {current_persona}\nRetrying in 10 seconds...\n"
                + Colors.RESET
            )
            time.sleep(10)
            continue

        current_persona_name = current_persona["name"]

        # Parse song data, get time difference between song end and current time
        spin_id = current_spin["id"]
        spin_song_title = current_spin["song"]
        spin_artist = current_spin["artist"]
        spin_duration = current_spin["duration"]

        song_start_datetime = parser.parse(current_spin["start"])
        song_end_datetime = parser.parse(current_spin["end"])
        song_start_hour = song_start_datetime.hour
        current_datetime = datetime.now(timezone.utc)
        current_hour = current_datetime.hour
        time_difference = (song_end_datetime - current_datetime).total_seconds()

        # If the current hour is outside of the defined schedule, sleep until the schedule starts
        if not (
            (start_hour <= current_hour < end_hour)
            or (
                start_hour > end_hour
                and (current_hour >= start_hour or current_hour < end_hour)
            )
        ):
            sleep_duration = get_sleep_duration(start_hour)
            print(
                Colors.YELLOW
                + f"\n---------{timestamp_string}---------\nOUTSIDE SCHEDULED SCROBBLING HOURS ({start_hour}:00-{end_hour}:00 UTC). Sleeping for next {sleep_duration} seconds until {start_hour}:00 UTC...\n"
                + Colors.RESET
            )
            time.sleep(sleep_duration)
        else:
            # If the current spin started playing at a time outside of the allowed scrobbling
            # schedule, pass
            if not (
                (start_hour <= song_start_hour < end_hour)
                or (
                    start_hour > end_hour
                    and (song_start_hour >= start_hour or song_start_hour < end_hour)
                )
            ):
                print(
                    Colors.YELLOW
                    + f"\n---------{timestamp_string}---------\nCURRENT SPIN BEGAN OUTSIDE SCHEDULED SCROBBLING HOURS. Disregarding...\n"
                    + Colors.RESET
                )
                time.sleep(15)
            else:
                # Check if a new song is playing
                if (time_difference > 0) and (spin_id != last_spin_id):

                    # TODO: make this user-definable in a new file
                    if (
                        current_playlist_category
                        and current_playlist_category != "Automation"
                    ):
                        print(f"\n---------{timestamp_string}---------")
                        print(
                            Colors.GREEN
                            + "NEW SONG: "
                            + Colors.RESET
                            + f"{spin_artist} - {spin_song_title}"
                        )
                        print(f"Spin ID: {spin_id}")
                        print(f"Spin Playlist: {current_playlist_title}")
                        print(f"Playlist Host: {current_persona_name}")

                        miss_count = 0

                        # Update now playing
                        np_code = update_np(
                            session_key=lastfm_session_key,
                            artist=current_spin["artist"],
                            track=current_spin["song"],
                            album=current_spin["release"],
                            duration=spin_duration,
                        )
                        if np_code in ERROR_CODES:
                            print(
                                Colors.RED
                                + f"ERROR: Now Playing request returned {np_code}"
                                + Colors.RESET
                            )
                        else:
                            timestamp_string = datetime.now().strftime("%H:%M:%S:%f")
                            print(
                                f"Now Playing updated successfully at {timestamp_string}\nWaiting for the end of song to submit scrobble..."
                            )

                        # Idle until end of song
                        time.sleep(time_difference)

                        # Last.fm asks that we only scrobbly songs longer than 30 seconds
                        if spin_duration > 30:
                            scrobble_code = request_scrobble(
                                session_key=lastfm_session_key,
                                artist=current_spin["artist"],
                                track=current_spin["song"],
                                timestamp=parser.parse(current_spin["end"]).timestamp(),
                                album=current_spin["release"],
                                duration=current_spin["duration"],
                            )
                            if scrobble_code in ERROR_CODES:
                                print(
                                    Colors.RED
                                    + f"ERROR: playback finished but the scrobble request returned {scrobble_code}"
                                    + Colors.RESET
                                )
                            else:
                                timestamp_string = datetime.now().strftime(
                                    "%H:%M:%S:%f"
                                )
                                print(f"✓ Scrobbled successfully at {timestamp_string}")
                        else:
                            print(
                                f"SCROBBLE SKIPPED: {spin_song_title} has a length of {spin_duration}, which is too short to scrobble."
                            )

                        time.sleep(5)
                    else:
                        print(
                            Colors.RED
                            + f"\n---------{timestamp_string}---------\nSPIN SKIPPED - belongs to playlist ({current_playlist_title}) with category `{current_playlist_category}`."
                            + Colors.RESET
                        )

                # The spin ID is the same as the ID returned in the most recent request
                else:
                    miss_count += 1
                    # print(Colors.YELLOW + f"\n{timestamp_string}\nMISS #{miss_count}" + Colors.RESET)

                    # In a perfect world, people spin in realtime, but in actuality, people get distracted and then batch-submit spins, which can lead to them being missed during this idle period.
                    #
                    # # If a miss occurs > 10 times in a row, idle for 3 minutes before next loop
                    # if miss_count > 10:
                    #     miss_str = Colors.YELLOW + f"\n---------{{timestamp_string}---------{\n{miss_count} requests since last spin. Currently {-1*int(time_difference)} seconds overdue according to last spin's end time value. Waiting 3 minutes before next request..." + Colors.RESET
                    #     print(miss_str)
                    #     time.sleep(180)

                    time.sleep(15)

                last_spin_id = spin_id


if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal_handler)  # Ctrl+C handler

    # Parse for --setup flag
    cli_parser = argparse.ArgumentParser()
    cli_parser.add_argument(
        "--setup",
        action="store_true",
        help="Run script in setup mode (when a web service session has not been established)",
    )
    args = cli_parser.parse_args()

    # Check if necessary env vars are either not present or left as example value
    if (not (lastfm_api_key and lastfm_api_secret and spinitron_api_key)) or (
        any(
            all(char == "x" for char in string.lower())
            for string in [lastfm_api_key, lastfm_api_secret, spinitron_api_key]
        )
    ):
        print(
            Colors.RED
            + 'Please make sure you have set your LASTFM_API_KEY, LASTFM_API_SECRET, and SPINITRON_API_KEY values in the ".env" file.'
            + Colors.RESET
        )
    else:
        if args.setup:
            # If setup flag was used, run setup, then set the obtained session key in the
            # .env file and run()
            if not os.path.exists("/scrobbler/setup_done"):
                new_session_key = setup()
                set_key("/env/.env", "LASTFM_SESSION_KEY", new_session_key)
                load_dotenv(dotenv_path="/env/.env")
                lastfm_session_key = os.getenv("LASTFM_SESSION_KEY")
                print("LASTFM_SESSION_KEY automatically set in /env/.env\n")
                sys.exit(0)
            else:
                print(
                    Colors.YELLOW
                    + "Setup was done previously. Aborting..."
                    + Colors.RESET
                )
                sys.exit(0)
        else:
            # Check if session key variable is not present or left as example value
            if (not lastfm_session_key) or all(
                char == "x" for char in lastfm_session_key.lower()
            ):
                print(
                    Colors.YELLOW
                    + 'Please make sure you have set your LASTFM_SESSION_KEY value in the ".env" file. If you have not yet established a web service session, please run the script in setup mode using the --setup argument.'
                    + Colors.RESET
                )
                sys.exit(0)
            else:
                run()
