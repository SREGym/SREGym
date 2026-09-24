"""Exercise all eight services through a disposable Blueprint frontend.

This reserves a room using the application's built-in synthetic test user.
Use a test deployment, not a benchmark run whose database must be preserved.
"""

import argparse
import json
import urllib.error
import urllib.parse
import urllib.request


def check_app(base_url):
    def call(handler, **params):
        url = base_url.rstrip("/") + "/" + handler + "?" + urllib.parse.urlencode(params)
        with urllib.request.urlopen(url, timeout=15) as response:
            return json.load(response)["Ret0"]

    profiles = call(
        "SearchHandler",
        customerName="Blueprint User",
        inDate="2015-04-09",
        outDate="2015-04-10",
        lat=37.7835,
        lon=-122.41,
        locale="en",
    )
    assert profiles, "Search did not return hotel profiles"
    assert call("UserHandler", username="Cornell_1", password="1111111111") == "Login successful"
    try:
        call("UserHandler", username="Cornell_1", password="invalid")
    except urllib.error.HTTPError as error:
        assert error.code == 500, error.code
    else:
        raise AssertionError("Invalid credentials were accepted")
    for criterion in ("dis", "rate", "price"):
        assert call("RecommendHandler", lat=37.7835, lon=-122.41, require=criterion, locale="en")
    assert (
        call(
            "ReservationHandler",
            inDate="2015-04-09",
            outDate="2015-04-10",
            hotelId="1",
            customerName="Blueprint User",
            username="Cornell_1",
            password="1111111111",
            roomNumber=1,
        )
        == "Reservation successful"
    )
    print(
        json.dumps(
            {
                "passed": True,
                "search_profiles": len(profiles),
                "checks": [
                    "search",
                    "login",
                    "invalid-login",
                    "recommend-distance",
                    "recommend-rate",
                    "recommend-price",
                    "reservation",
                ],
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("frontend_url")
    check_app(parser.parse_args().frontend_url)
