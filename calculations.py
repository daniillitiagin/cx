"""Calculations for the currency exchange agent: API, cleaning, SQL, rules and tests."""

import json
import sqlite3
import time

import numpy as np
import pandas as pd
import requests



# 1. Custom error raised when exchange-rate data is missing
class RateError(ValueError):
    pass


# 2. Save the user's settings to config.json
def save_settings(settings):
    file = open("config.json", "w")
    json.dump(settings, file, indent=4)
    file.close()


# 3. Load user's settings 
def load_settings():
    defaults = {
        "url": "https://api.frankfurter.dev/v2/rates",
        "currencies": ["GBP", "USD", "JPY", "AED", "CAD", "EUR"],
        "history_days": 30,
        "range_method": "Standard deviation",
        "band_width": 1.0,
        "percentile": 10,
        "change_weight": 70,
        "stability_weight": 30
    }

    try:
        file = open("config.json", "r")
        settings = json.load(file)
        file.close()
    except FileNotFoundError:
        settings = {}

    # Add any setting that is missing
    changed = False
    for key in defaults:
        if key not in settings:
            settings[key] = defaults[key]
            changed = True

    if changed:
        save_settings(settings)

    return settings

# 4. Get data from the API (try again if it fails, up to 3 times)
# get_request is normally requests.get. The tests swap in a fake
# function that fails on purpose (a "mock"), so we can test errors
# without switching off the Wi-Fi.
def fetch_with_retry(url, params, retries=3, get_request=requests.get):
    """Call the API and return a list of records, or [] if it fails."""
    for attempt in range(1, retries + 1):
        try:
            response = get_request(url, params=params, timeout=20)

            if response.status_code == 200:
                data = response.json()

                if type(data) == list:
                    return data

                print("The API returned an unexpected response.")
                return []

            elif response.status_code == 404:
                print("404: No rates found for these currencies.")
                return []

            elif response.status_code == 429:
                print("429: Too many requests.")

            elif response.status_code == 500:
                print("500: The API has a server error.")

            else:
                print("Request failed:", response.status_code)
                return []

        except requests.Timeout:
            print("The request timed out.")

        except requests.RequestException:
            print("Could not get data from the API.")

        if attempt < retries:
            print("Trying again in", attempt, "second(s).")
            time.sleep(attempt)

    return []


# 5. Check one record from the API
def validate_record(record):
    """Return True if a record has the right keys, types and sensible values."""
    # Each record must be a dictionary
    if type(record) != dict:
        return False

    # All four keys must be there
    for key in ["date", "base", "quote", "rate"]:
        if key not in record:
            return False

    # The date must be a real date like 2026-01-10
    date = pd.to_datetime(record["date"], format="%Y-%m-%d", errors="coerce")
    if pd.isna(date):
        return False

    # Currency codes must be 3 letters, like AED
    for key in ["base", "quote"]:
        code = str(record[key])
        if len(code) != 3 or not code.isalpha():
            return False

    # The rate must be a number above 0
    try:
        rate = float(record["rate"])
    except ValueError:
        return False
    except TypeError:
        return False

    if rate <= 0 or not np.isfinite(rate):
        return False

    return True


# 6. Clean the API data and build the data quality report
def clean_records(data, selection, quote):
    """Keep valid rows for one currency pair and return (DataFrame, report)."""
    columns = ["date", "base", "quote", "rate"]

    # Count the missing values in each column before cleaning
    raw = pd.DataFrame(data, columns=columns)
    missing = raw.isna().sum()

    # Keep the records that pass validate_record
    valid_rows = []
    for record in data:
        if validate_record(record):
            valid_rows.append(record)

    df = pd.DataFrame(valid_rows, columns=columns)

    # Convert dates and rates to the right types
    df["date"] = pd.to_datetime(df["date"])
    df["rate"] = pd.to_numeric(df["rate"])

    # Keep only the currency pair we asked for
    df = df[df["base"] == selection]
    df = df[df["quote"] == quote]

    # Remove duplicate dates
    df = df.drop_duplicates(subset=["date"])

    report = {
        "Rows received": len(data),
        "Rows failed validation": len(data) - len(valid_rows),
        "Rows kept": len(df),
        "Rows removed in total": len(data) - len(df),
        "Missing dates": int(missing["date"]),
        "Missing base currencies": int(missing["base"]),
        "Missing quote currencies": int(missing["quote"]),
        "Missing rates": int(missing["rate"])
    }

    return df, report


# 7. Get the latest rate and the historical rates for one currency pair
def get_rates(selection, quote, url, history_days):
    """Return the latest rate, its date, the history table and two quality reports."""
    # First API call: the latest rate
    params = {"base": selection, "quotes": quote}
    data = fetch_with_retry(url, params)
    df, latest_report = clean_records(data, selection, quote)

    # "raise" stops the function and sends the error message to app.py
    if len(df) == 0:
        raise RateError(f"No valid latest rate for {selection} to {quote}.")

    df = df.sort_values("date")
    latest_rate = df["rate"].iloc[-1]
    latest_date = df["date"].iloc[-1]

    # Second API call: the rates for the days before the latest rate
    start = latest_date - pd.Timedelta(days=history_days)
    end = latest_date - pd.Timedelta(days=1)

    params = {
        "base": selection,
        "quotes": quote,
        "from": start.date(),
        "to": end.date()
    }

    data = fetch_with_retry(url, params)
    df_history, history_report = clean_records(data, selection, quote)

    if len(df_history) < 2:
        raise RateError(f"Too little history for {selection} to {quote}.")

    # Save the history in SQLite, then read it back with SQL
    # Each pair gets its own table, for example rates_AED_GBP
    table = f"rates_{selection}_{quote}"

    connection = sqlite3.connect("exchange_rates.db")

    df_history.to_sql(table, connection, if_exists="replace", index=False)

    query = f"""
SELECT date, base, quote, rate
FROM {table}
WHERE base = ? AND quote = ?
ORDER BY date
"""
    history = pd.read_sql_query(query, connection, params=(selection, quote))

    connection.close()

    history["date"] = pd.to_datetime(history["date"])

    # C1: join the two API results with pd.merge on the currency pair,
    # so every historical day sits next to today's rate
    latest = df[["base", "quote", "rate"]].tail(1)
    latest = latest.rename(columns={"rate": "latest_rate"})

    history = pd.merge(history, latest, on=["base", "quote"])
    history["Today vs this day (%)"] = (
        100 * (history["latest_rate"] - history["rate"]) / history["rate"]
    )

    return latest_rate, latest_date, history, latest_report, history_report


# 8. How much you get when you exchange money
def calculate_exchange(amt, latest_rate, average_rate):
    """Return the amount received at the latest and average rates, and the difference."""
    latest_total = amt * latest_rate
    average_total = amt * average_rate
    difference = latest_total - average_total

    return latest_total, average_total, difference


# 9. How much an invoice costs you in your own currency
def calculate_invoice(amt, latest_rate, average_rate):
    """Return the invoice cost at the latest and average rates, and the savings."""
    latest_cost = amt / latest_rate
    average_cost = amt / average_rate
    savings = average_cost - latest_cost

    return latest_cost, average_cost, savings


# 10. Compare the latest rate with the usual range
def get_recommendation(latest_rate, lower, upper):
    """Return Convert now, Wait if you can, or Neutral."""
    if latest_rate > upper:
        return "Convert now"
    elif latest_rate < lower:
        return "Wait if you can"
    else:
        return "Neutral"


# 11. C4: two ways to set the usual range
# Standard deviation: average +/- band_width x standard deviation
# Percentile: for example the 10th and 90th percentile of past rates
def get_limits(rates, method, band_width, percentile):
    """Return the lower and upper limit of the usual range."""
    if method == "Percentile":
        lower = np.percentile(rates, percentile)
        upper = np.percentile(rates, 100 - percentile)
    else:
        average = np.mean(rates)
        std = np.std(rates)
        lower = average - band_width * std
        upper = average + band_width * std

    return lower, upper


# 12. All the numbers for one currency pair in one dictionary
# (used by Analyze, Compare and the "use another currency" check)
def summarize_pair(selection, quote, rate, date, history, settings):
    """Return the statistics and the recommendation for one currency pair."""
    rates = history["rate"]
    average = np.mean(rates)
    std = np.std(rates)

    lower, upper = get_limits(
        rates, settings["range_method"], settings["band_width"], settings["percentile"]
    )

    return {
        "Base": selection,
        "Currency": quote,
        "Rate date": str(date.date()),
        "Latest": rate,
        "Average": average,
        "Std": std,
        "Lower": lower,
        "Upper": upper,
        "Change (%)": 100 * (rate - average) / average,
        "Volatility (%)": 100 * std / average,
        "Recommendation": get_recommendation(rate, lower, upper),
        "Data coverage (%)": min(100, 100 * len(history) / settings["history_days"])
    }


# 13. Give each currency a score based on improvement and stability
def scale_to_unit(values):
    """Scale a column of numbers to 0-1 (or 0.5 if every value is the same)."""
    low = np.min(values)
    high = np.max(values)
    if high == low:
        return 0.5
    return (values - low) / (high - low)


def rank_currencies(results, change_weight, stability_weight):
    """Score and rank currencies using the weights from the settings."""
    comparison = pd.DataFrame(results)

    # Change score: best improvement = 1, worst = 0
    comparison["Change score"] = scale_to_unit(comparison["Change (%)"])

    # Stability score: least volatile = 1, most volatile = 0
    comparison["Stability score"] = 1 - scale_to_unit(comparison["Volatility (%)"])

    # Final score uses the weights from Settings (for example 70% and 30%)
    comparison["Final score"] = (
        change_weight * comparison["Change score"]
        + stability_weight * comparison["Stability score"]
    )

    return comparison.sort_values("Final score", ascending=False)


# 14. C3: anomaly detection with the z-score
# z-score = how many standard deviations a day is away from the average
def find_anomalies(history):
    """Add a z-score column to history and return the days where |z| > 2."""
    average = np.mean(history["rate"])
    std = np.std(history["rate"])

    # If every rate is the same there is nothing unusual
    if std == 0:
        history["z-score"] = 0.0
    else:
        history["z-score"] = (history["rate"] - average) / std

    anomalies = history[np.abs(history["z-score"]) > 2]

    return anomalies


# 15. C5: what-if analysis
# Count how many currencies pass "Convert now" for different band widths
def what_if(comparison):
    """Return a list of band widths and how many currencies pass at each one."""
    bands = np.arange(0, 2.25, 0.25)
    counts = []

    for band in bands:
        upper = comparison["Average"] + band * comparison["Std"]
        passing = comparison[comparison["Latest"] > upper]
        counts.append(len(passing))

    return bands, counts


# 16. C10: write a Markdown summary of the saved results
def build_report(records, comparison):
    """Return a Markdown report of the saved analyses and the ranking."""
    lines = [
        "# Currency exchange report",
        "",
        "Created: " + time.strftime("%Y-%m-%d %H:%M"),
        "",
        "## Saved analyses"
    ]

    if len(records) == 0:
        lines.append("")
        lines.append("No analyses were saved.")
    else:
        for record in records:
            lines.append("")
            lines.append(
                f"### {record['Action']}: {record['Currency you have']} "
                f"to {record['Currency you buy']}"
            )
            for key in record:
                lines.append(f"- {key}: {record[key]}")

    lines.append("")
    lines.append("## Currency ranking")
    lines.append("")

    if len(comparison) == 0:
        lines.append("No ranking yet. Use Compare first.")
    else:
        for i in range(len(comparison)):
            row = comparison.iloc[i]
            lines.append(
                f"{i + 1}. {row['Currency']}: score {row['Final score']:.1f}, "
                f"{row['Recommendation']}"
            )

    # "\n".join puts every line on its own row in the file
    return "\n".join(lines)


# 17. Fake request functions for the tests (mocks)
# They act like requests.get, but fail on purpose
def timeout_request(url, params, timeout):
    """Pretend the API took too long."""
    raise requests.Timeout()


def disconnected_request(url, params, timeout):
    """Pretend the computer has no internet."""
    raise requests.ConnectionError()


# 18. Tests: check that the calculations give the right answers
def run_tests():
    """Run all the checks and print PASS or FAIL for each one."""
    tests = {}

    # Calculations
    latest, average, savings = calculate_invoice(100, 0.25, 0.20)
    tests["Invoice cost"] = (latest == 400)
    tests["Invoice savings"] = (savings == 100)

    latest, average, difference = calculate_exchange(100, 0.25, 0.20)
    tests["Exchange amount"] = (latest == 25)
    tests["Exchange difference"] = (difference == 5)

    # Recommendation rules
    tests["Above upper limit"] = (get_recommendation(11, 8, 10) == "Convert now")
    tests["Below lower limit"] = (get_recommendation(7, 8, 10) == "Wait if you can")
    tests["Exactly on limit"] = (get_recommendation(10, 8, 10) == "Neutral")

    # validate_record
    record = {"date": "2026-01-10", "base": "AED", "quote": "GBP", "rate": 0.20}
    tests["Valid record accepted"] = validate_record(record)

    record = {"date": "2026-01-10", "base": "AED", "quote": "GBP", "rate": -1}
    tests["Negative rate rejected"] = not validate_record(record)

    record = {"date": "2026-01-10", "base": "AED", "quote": "GBP"}
    tests["Missing rate rejected"] = not validate_record(record)

    record = {"date": "not a date", "base": "AED", "quote": "GBP", "rate": 0.20}
    tests["Bad date rejected"] = not validate_record(record)

    # clean_records: 1 good row, 1 negative rate, 1 duplicate, 1 missing rate
    data = [
        {"date": "2026-01-10", "base": "AED", "quote": "GBP", "rate": 0.20},
        {"date": "2026-01-11", "base": "AED", "quote": "GBP", "rate": -1},
        {"date": "2026-01-10", "base": "AED", "quote": "GBP", "rate": 0.20},
        {"date": "2026-01-12", "base": "AED", "quote": "GBP"}
    ]
    df, report = clean_records(data, "AED", "GBP")
    tests["Cleaning keeps only the good row"] = (len(df) == 1)
    tests["Missing rate is counted"] = (report["Missing rates"] == 1)

    # C4: both ways of setting the range
    rates = pd.Series([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
    lower, upper = get_limits(rates, "Standard deviation", 1.0, 10)
    tests["Standard deviation range is centred on the average"] = (round(lower + upper, 6) == 11)

    lower, upper = get_limits(rates, "Percentile", 1.0, 10)
    tests["Percentile range uses the 10th and 90th percentile"] = (
        round(lower, 6) == 1.9 and round(upper, 6) == 9.1
    )

    # C3: one clear outlier among 20 normal days
    history = pd.DataFrame({"rate": [1.0] * 10 + [1.01] * 10 + [2.0]})
    anomalies = find_anomalies(history)
    tests["Anomaly detection finds the outlier"] = (
        len(anomalies) == 1 and anomalies["rate"].iloc[0] == 2.0
    )

    # C5: what-if counts can only go down as the band gets wider
    comparison = pd.DataFrame({
        "Latest": [1.10, 1.02, 0.95],
        "Average": [1.00, 1.00, 1.00],
        "Std": [0.05, 0.05, 0.05]
    })
    bands, counts = what_if(comparison)
    tests["What-if: 2 pass at band 0, 0 pass at band 2"] = (counts[0] == 2 and counts[-1] == 0)

    # Mocked API failures
    result = fetch_with_retry("test", {}, retries=1, get_request=timeout_request)
    tests["Timeout handled (mock)"] = (result == [])

    result = fetch_with_retry("test", {}, retries=1, get_request=disconnected_request)
    tests["Connection failure handled (mock)"] = (result == [])

    # Custom exception: a broken URL gives no data, so get_rates must raise RateError
    print("(The next test retries a broken URL, so it takes a few seconds.)")
    try:
        get_rates("AED", "GBP", "http://localhost:1", 30)
        tests["RateError raised when there is no data"] = False
    except RateError:
        tests["RateError raised when there is no data"] = True

    # Print the results
    passed = 0

    for name in tests:
        if tests[name]:
            print("PASS:", name)
            passed += 1
        else:
            print("FAIL:", name)

    print(passed, "out of", len(tests), "checks passed.")


# Run the tests only when this file is run directly (py calculations.py),
# not when app.py imports it
if __name__ == "__main__":
    run_tests()
