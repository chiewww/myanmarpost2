import html
import json
import re
import time
import urllib.parse
from datetime import datetime, timezone

import requests


# =========================================================
# MYANMAR POST
# =========================================================

BASE_URL = "https://myanmarpost.com.mm"
PRICING_URL = f"{BASE_URL}/pricing?tab=international"
CALCULATE_URL = f"{BASE_URL}/pricing"

# The exact weight used by the Myanmar Post calculator.
WEIGHT_KG = 0.02

# The user wants to find prices with 6 or more digits.
# 100,000 is the smallest 6-digit number.
ABNORMAL_PRICE_THRESHOLD = 100000

# HTTP settings.
REQUEST_TIMEOUT = 60
MAX_REQUEST_ATTEMPTS = 3
RETRY_DELAYS = (2, 5, 10)

# Be conservative with the Myanmar Post server.
# We intentionally process countries sequentially.
REQUEST_DELAY = 0.25

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 "
    "(KHTML, like Gecko) "
    "Chrome/151.0.0.0 Safari/537.36"
)


# =========================================================
# HTTP CLIENT
# =========================================================

class MyanmarPostClient:
    """
    HTTP client for Myanmar Post.

    The International Pricing page contains the country list
    inside the Inertia data-page payload.

    The calculator itself is a POST request to /pricing.

    FP delivery duration is retrieved separately from:

        /deliver-duration/{country_code}
    """

    def __init__(self):
        self.session = requests.Session()

        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept-Language": "en-US,en;q=0.9",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            }
        )

        self.pricing_page_body = None

    # -----------------------------------------------------
    # GET / POST with retries
    # -----------------------------------------------------

    def request(
        self,
        method,
        url,
        **kwargs,
    ):
        """
        Perform an HTTP request with retries.

        Retries:
            - connection failures
            - timeouts
            - HTTP 500
            - HTTP 502
            - HTTP 503
            - HTTP 504
        """

        last_error = None

        for attempt in range(
            1,
            MAX_REQUEST_ATTEMPTS + 1,
        ):
            try:
                response = self.session.request(
                    method,
                    url,
                    timeout=REQUEST_TIMEOUT,
                    **kwargs,
                )

                if response.status_code in (
                    500,
                    502,
                    503,
                    504,
                ):
                    print(
                        f"HTTP {response.status_code} "
                        f"for {url}"
                    )

                    if attempt < MAX_REQUEST_ATTEMPTS:
                        delay = RETRY_DELAYS[
                            min(
                                attempt - 1,
                                len(RETRY_DELAYS) - 1,
                            )
                        ]

                        print(
                            f"Retrying in {delay} "
                            f"seconds..."
                        )

                        time.sleep(delay)
                        continue

                response.raise_for_status()

                return response

            except (
                requests.exceptions.Timeout,
                requests.exceptions.ConnectionError,
                requests.exceptions.RequestException,
            ) as error:

                last_error = error

                print(
                    f"Request error for {url} "
                    f"(attempt "
                    f"{attempt}/"
                    f"{MAX_REQUEST_ATTEMPTS}): "
                    f"{repr(error)}"
                )

                if attempt < MAX_REQUEST_ATTEMPTS:
                    delay = RETRY_DELAYS[
                        min(
                            attempt - 1,
                            len(RETRY_DELAYS) - 1,
                        )
                    ]

                    print(
                        f"Retrying in {delay} "
                        f"seconds..."
                    )

                    time.sleep(delay)

                    continue

                raise

        if last_error is not None:
            raise last_error

        raise RuntimeError(
            f"Request failed: {method} {url}"
        )

    # -----------------------------------------------------
    # Browser-style headers
    # -----------------------------------------------------

    def browser_headers(
        self,
        referer=None,
    ):
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": (
                "text/html, "
                "application/xhtml+xml, "
                "application/json;q=0.9, "
                "*/*;q=0.8"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }

        if referer:
            headers["Referer"] = referer

        return headers

    # -----------------------------------------------------
    # Load International Pricing page
    # -----------------------------------------------------

    def load_pricing_page(self):
        """
        Download the International Pricing page.

        The response contains the Inertia data-page payload
        containing CountryZoneList.
        """

        print(
            "Loading Myanmar Post International "
            "Pricing page..."
        )

        response = self.request(
            "GET",
            PRICING_URL,
            headers=self.browser_headers(),
        )

        self.pricing_page_body = response.text

        print(
            f"Pricing page: HTTP "
            f"{response.status_code}, "
            f"{len(response.content)} bytes"
        )

        return response.text

    # -----------------------------------------------------
    # Extract Inertia data-page
    # -----------------------------------------------------

    def extract_inertia_page(
        self,
        body,
    ):
        """
        Extract and decode:

            <div id="app" data-page="...">

        The data-page value is HTML-entity encoded.
        """

        match = re.search(
            r'<div[^>]+id=["\']app["\'][^>]+'
            r'data-page=["\'](.*?)["\']',
            body,
            flags=re.IGNORECASE | re.DOTALL,
        )

        if not match:
            # Try the opposite attribute order just in case.
            match = re.search(
                r'<div[^>]+data-page=["\'](.*?)["\'][^>]+'
                r'id=["\']app["\']',
                body,
                flags=re.IGNORECASE | re.DOTALL,
            )

        if not match:
            raise RuntimeError(
                "Could not find the Inertia "
                "data-page attribute."
            )

        raw_page = html.unescape(
            match.group(1)
        )

        try:
            return json.loads(raw_page)

        except json.JSONDecodeError as error:
            raise RuntimeError(
                "Could not decode the Inertia "
                "data-page JSON."
            ) from error

    # -----------------------------------------------------
    # Find CountryZoneList
    # -----------------------------------------------------

    def find_country_zone_list(
        self,
        obj,
    ):
        """
        Recursively locate the object named:

            CountryZoneList

        and return its data list.
        """

        if isinstance(obj, dict):

            for key, value in obj.items():

                if key == "CountryZoneList":

                    if isinstance(
                        value,
                        dict,
                    ):
                        data = value.get("data")

                        if isinstance(
                            data,
                            list,
                        ):
                            return data

                    if isinstance(
                        value,
                        list,
                    ):
                        return value

                result = self.find_country_zone_list(
                    value
                )

                if result:
                    return result

        elif isinstance(obj, list):

            for item in obj:

                result = self.find_country_zone_list(
                    item
                )

                if result:
                    return result

        return []

    # -----------------------------------------------------
    # Get country list
    # -----------------------------------------------------

    def get_countries(self):
        """
        Download the International Pricing page and
        return the country records from CountryZoneList.
        """

        body = self.load_pricing_page()

        page_data = self.extract_inertia_page(
            body
        )

        countries = self.find_country_zone_list(
            page_data
        )

        if not countries:
            raise RuntimeError(
                "Could not find CountryZoneList "
                "in the Myanmar Post pricing page."
            )

        valid_countries = []

        for country in countries:

            if not isinstance(
                country,
                dict,
            ):
                continue

            name = str(
                country.get(
                    "name_en",
                    "",
                )
            ).strip()

            code = str(
                country.get(
                    "alpha_2_code",
                    "",
                )
            ).strip().upper()

            if not name or not code:
                continue

            valid_countries.append(
                {
                    "name": name,
                    "code": code,
                }
            )

        # Remove duplicate country codes while preserving
        # the first occurrence.
        unique = {}

        for country in valid_countries:

            code = country["code"]

            if code not in unique:
                unique[code] = country

        countries = list(
            unique.values()
        )

        # Sort alphabetically by English country name.
        countries.sort(
            key=lambda item: item["name"].lower()
        )

        print(
            f"Found {len(countries)} countries "
            f"in CountryZoneList."
        )

        return countries

    # -----------------------------------------------------
    # Obtain XSRF token if one exists
    # -----------------------------------------------------

    def get_xsrf_token(self):
        """
        Return an XSRF token from the session cookie
        if Myanmar Post provides one.

        The calculator currently appears to work without
        explicitly sending this header, but including it
        when available makes the request more compatible
        with Laravel-style applications.
        """

        token = self.session.cookies.get(
            "XSRF-TOKEN"
        )

        if not token:
            return None

        return urllib.parse.unquote(
            token
        )

    # -----------------------------------------------------
    # Calculate FP Letter price
    # -----------------------------------------------------

    def calculate_fp_letter_price(
        self,
        country_code,
    ):
        """
        Reproduce the browser's calculator request.

        Browser request captured from the current site:

            POST /pricing

        with:

            weight = 0.02
            country_code = AU
            type = fp
            tab = international
            parcel_type = letter
            item_type = letter
            source_postcode = 111601
            destination_postcode = 11183
            accept_type = walkin
            destination_country = AU
            service_type = fp
        """

        payload = {
            "weight": WEIGHT_KG,
            "country_code": country_code,
            "type": "fp",
            "tab": "international",
            "parcel_type": "letter",
            "item_type": "letter",
            "source_postcode": "111601",
            "destination_postcode": "11183",
            "accept_type": "walkin",
            "destination_country": country_code,
            "service_type": "fp",
        }

        headers = {
            "Accept": (
                "application/json, "
                "text/plain, "
                "*/*"
            ),
            "Content-Type": "application/json",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": BASE_URL,
            "Referer": PRICING_URL,
        }

        xsrf_token = self.get_xsrf_token()

        if xsrf_token:
            headers["X-XSRF-TOKEN"] = xsrf_token

        response = self.request(
            "POST",
            CALCULATE_URL,
            headers=headers,
            json=payload,
        )

        try:
            data = response.json()

        except ValueError as error:
            raise RuntimeError(
                f"Calculator returned non-JSON "
                f"response for {country_code}. "
                f"HTTP {response.status_code}. "
                f"Response starts with: "
                f"{response.text[:300]!r}"
            ) from error

        return self.extract_price(
            data,
            country_code,
        )

    # -----------------------------------------------------
    # Extract price
    # -----------------------------------------------------

    def extract_price(
        self,
        data,
        country_code,
    ):
        """
        Extract totalPrice from the calculator response.

        Expected response:

            {
                "data": {
                    "international": 11000,
                    "totalPrice": 11000
                }
            }
        """

        if not isinstance(
            data,
            dict,
        ):
            raise RuntimeError(
                f"Unexpected calculator response "
                f"for {country_code}."
            )

        response_data = data.get(
            "data",
            data,
        )

        if not isinstance(
            response_data,
            dict,
        ):
            raise RuntimeError(
                f"Unexpected calculator data "
                f"for {country_code}."
            )

        price = response_data.get(
            "totalPrice"
        )

        if price is None:
            price = response_data.get(
                "international"
            )

        if price is None:
            return None

        try:
            # The server may return either a number
            # or a numeric string.
            numeric_price = int(
                float(price)
            )

        except (
            ValueError,
            TypeError,
        ) as error:

            raise RuntimeError(
                f"Could not convert calculator "
                f"price for {country_code}: "
                f"{price!r}"
            ) from error

        return numeric_price

    # -----------------------------------------------------
    # Get FP duration
    # -----------------------------------------------------

    def get_fp_duration(
        self,
        country_code,
    ):
        """
        Get FP delivery duration from:

            /deliver-duration/{country_code}

        Example:

            {
                "data": {
                    "alpha_2_code": "AU",
                    "country": "Australia",
                    "fp": {
                        "dispatch": 3,
                        "final": 10,
                        "days_en":
                            "between 3 and 10 days"
                    }
                }
            }
        """

        url = (
            f"{BASE_URL}/deliver-duration/"
            f"{country_code}"
        )

        headers = {
            "User-Agent": USER_AGENT,
            "Accept": (
                "application/json, "
                "text/plain, "
                "*/*"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": PRICING_URL,
        }

        response = self.request(
            "GET",
            url,
            headers=headers,
        )

        try:
            data = response.json()

        except ValueError as error:
            raise RuntimeError(
                f"Invalid duration JSON "
                f"for {country_code}. "
                f"Response starts with: "
                f"{response.text[:300]!r}"
            ) from error

        if not isinstance(
            data,
            dict,
        ):
            return None

        response_data = data.get(
            "data",
            {},
        )

        if not isinstance(
            response_data,
            dict,
        ):
            return None

        fp = response_data.get(
            "fp",
            {},
        )

        if not isinstance(
            fp,
            dict,
        ):
            return None

        duration = fp.get(
            "days_en"
        )

        if duration is None:
            return None

        return str(
            duration
        ).strip()


# =========================================================
# LIST 2 RULES
# =========================================================

def has_abnormal_price(
    price,
):
    """
    Return True when the price has 6 or more digits.

    Examples:

        11000   -> False
        19500   -> False
        99999   -> False
        100000  -> True
        195000  -> True
    """

    if price is None:
        return False

    try:
        return int(price) >= (
            ABNORMAL_PRICE_THRESHOLD
        )

    except (
        ValueError,
        TypeError,
    ):
        return False


def duration_has_days(
    duration,
):
    """
    Return True only when the FP duration contains
    the word 'days'.

    Examples:

        'between 3 and 10 days' -> True
        '10 days'               -> True
        '-'                     -> False
        ''                      -> False
        None                    -> False
    """

    if duration is None:
        return False

    text = str(
        duration
    ).strip().lower()

    return "days" in text


# =========================================================
# MAIN
# =========================================================

def main():
    start_time = time.time()

    print(
        "========================================"
    )

    print(
        "Myanmar Post International "
        "Shipping Fee Monitor"
    )

    print(
        "========================================"
    )

    print(
        f"Service: FP"
    )

    print(
        f"Package: Letter"
    )

    print(
        f"Weight: {WEIGHT_KG} kg"
    )

    print("")

    client = MyanmarPostClient()

    # -----------------------------------------------------
    # Get country list.
    # -----------------------------------------------------

    countries = client.get_countries()

    if not countries:
        raise RuntimeError(
            "No countries were found."
        )

    # -----------------------------------------------------
    # Lists to build.
    # -----------------------------------------------------

    list_1 = []

    list_2 = []

    # -----------------------------------------------------
    # List 1.
    # -----------------------------------------------------

    for country in countries:

        list_1.append(
            country["name"]
        )

    print(
        f"List 1 contains {len(list_1)} countries."
    )

    print("")

    # -----------------------------------------------------
    # Calculate every country.
    # -----------------------------------------------------

    total = len(countries)

    for index, country in enumerate(
        countries,
        start=1,
    ):

        country_name = country[
            "name"
        ]

        country_code = country[
            "code"
        ]

        print(
            f"[{index}/{total}] "
            f"{country_name} "
            f"({country_code})"
        )

        price = None
        duration = None

        calculation_error = None
        duration_error = None

        # -------------------------------------------------
        # Calculate FP Letter 0.02 kg price.
        # -------------------------------------------------

        try:

            price = (
                client.calculate_fp_letter_price(
                    country_code
                )
            )

            if price is not None:
                print(
                    f"  Price: "
                    f"{price:,} Kyats"
                )

            else:
                print(
                    "  Price: unavailable"
                )

        except Exception as error:

            calculation_error = error

            print(
                f"  Price ERROR: {error}"
            )

        # -------------------------------------------------
        # Get FP duration.
        # -------------------------------------------------

        try:

            duration = (
                client.get_fp_duration(
                    country_code
                )
            )

            if duration:
                print(
                    f"  FP duration: "
                    f"{duration}"
                )

            else:
                print(
                    "  FP duration: unavailable"
                )

        except Exception as error:

            duration_error = error

            print(
                f"  Duration ERROR: {error}"
            )

        # -------------------------------------------------
        # Determine List 2 reasons.
        # -------------------------------------------------

        reasons = []

        # Price >= 100,000.
        if has_abnormal_price(
            price
        ):
            reasons.append(
                "6-digit-or-more price"
            )

        # Duration does not contain "days".
        if not duration_has_days(
            duration
        ):
            reasons.append(
                "FP duration does not show days"
            )

        # -------------------------------------------------
        # If there was a calculation error, don't silently
        # call it a 6-digit price. Instead, record the
        # error as a separate monitoring problem.
        #
        # The duration rule still applies independently.
        # -------------------------------------------------

        if calculation_error is not None:
            reasons.append(
                "FP price calculation failed"
            )

        if duration_error is not None:
            # Avoid duplicating the duration reason.
            if (
                "FP duration does not show days"
                not in reasons
            ):
                reasons.append(
                    "FP duration request failed"
                )

        if reasons:

            list_2.append(
                {
                    "name": country_name,
                    "code": country_code,
                    "price": price,
                    "duration": duration,
                    "reasons": reasons,
                    "calculation_error": (
                        str(calculation_error)
                        if calculation_error
                        else None
                    ),
                    "duration_error": (
                        str(duration_error)
                        if duration_error
                        else None
                    ),
                }
            )

            print(
                "  >>> LIST 2: FLAGGED"
            )

            for reason in reasons:
                print(
                    f"      Reason: {reason}"
                )

        else:

            print(
                "  OK"
            )

        # -------------------------------------------------
        # Small delay to be conservative with the server.
        # -------------------------------------------------

        if index < total:
            time.sleep(
                REQUEST_DELAY
            )

    # -----------------------------------------------------
    # Build deterministic output.
    # -----------------------------------------------------

    output = []

    output.append(
        "MYANMAR POST - INTERNATIONAL SHIPPING FEE"
    )

    output.append(
        "Service Type: FP"
    )

    output.append(
        "Package Type: Letter"
    )

    output.append(
        "Weight: 0.02 kg"
    )

    output.append("")

    # -----------------------------------------------------
    # LIST 1
    # -----------------------------------------------------

    output.append(
        "LIST 1 - ACCEPTING COUNTRY (TO)"
    )

    output.append(
        f"Total countries: {len(list_1)}"
    )

    output.append("")

    for number, country_name in enumerate(
        list_1,
        start=1,
    ):

        output.append(
            f"{number}. {country_name}"
        )

    output.append("")

    output.append(
        "LIST 2 - ABNORMAL RESULTS"
    )

    output.append(
        "A country appears here when the FP "
        "Letter 0.02 kg result has a price "
        "of 100,000 Kyats or more, or the "
        "FP delivery duration does not show "
        "the word 'days'."
    )

    output.append("")

    if not list_2:

        output.append(
            "NONE"
        )

    else:

        for number, item in enumerate(
            list_2,
            start=1,
        ):

            output.append(
                f"{number}. {item['name']} "
                f"({item['code']})"
            )

            if item["price"] is None:

                output.append(
                    "   Price: unavailable"
                )

            else:

                output.append(
                    "   Price: "
                    f"{item['price']:,} Kyats"
                )

            if item["duration"]:

                output.append(
                    "   FP duration: "
                    f"{item['duration']}"
                )

            else:

                output.append(
                    "   FP duration: "
                    "unavailable"
                )

            output.append(
                "   Reason: "
                + "; ".join(
                    item["reasons"]
                )
            )

            if item[
                "calculation_error"
            ]:

                output.append(
                    "   Calculation error: "
                    + item[
                        "calculation_error"
                    ]
                )

            if item[
                "duration_error"
            ]:

                output.append(
                    "   Duration error: "
                    + item[
                        "duration_error"
                    ]
                )

            output.append("")

    # -----------------------------------------------------
    # Write output.
    # -----------------------------------------------------

    result = "\n".join(
        output
    ).rstrip() + "\n"

    output_filename = (
        "myanmarpost_prices.txt"
    )

    with open(
        output_filename,
        "w",
        encoding="utf-8",
        newline="\n",
    ) as file:

        file.write(
            result
        )

    # -----------------------------------------------------
    # Display summary.
    # -----------------------------------------------------

    elapsed = (
        time.time()
        - start_time
    )

    print("")

    print(
        "========================================"
    )

    print(
        f"Countries checked: {len(countries)}"
    )

    print(
        f"List 2 flagged countries: "
        f"{len(list_2)}"
    )

    print(
        f"Output file: {output_filename}"
    )

    print(
        f"Runtime: {elapsed:.1f} seconds"
    )

    print(
        "========================================"
    )

    print("")

    print(
        result
    )


# =========================================================
# START
# =========================================================

if __name__ == "__main__":
    main()
