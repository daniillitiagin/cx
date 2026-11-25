"""Calculations for the currency exchange agent."""

import json
import sqlite3
import time

import numpy as np
import pandas as pd
import requests


class RateError(ValueError):
    pass


def save_settings(settings):
    file = open("config.json", "w")
    json.dump(settings, file, indent=4)
    file.close()


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

    changed = False
    for key in defaults:
        if key not in settings:
            settings[key] = defaults[key]
            changed = True

    if changed:
        save_settings(settings)

    return settings


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


def validate_record(record):
    """Return True if a record has valid keys, types and values."""
    if type(record) != dict:
        return False

    for key in ["date", "base", "quote", "rate"]:
        if key not in record:
            return False

    date = pd.to_datetime(record["date"], format="%Y-%m-%d", errors="coerce")
    if pd.isna(date):
        return False

    for key in ["base", "quote"]:
        code = str(record[key])
        if len(code) != 3 or not code.isalpha():
            return False

    try:
        rate = float(record["rate"])
    except ValueError:
        return False
    except TypeError:
        return False

    if rate <= 0 or not np.isfinite(rate):
        return False

    return True


def clean_records(data, selection, quote):
    """Filter valid rows for one currency pair and return (DataFrame, report)."""
    columns = ["date", "base", "quote", "rate"]

    raw = pd.DataFrame(data, columns=columns)
    missing = raw.isna().sum()

    valid_rows = []
    for record in data:
        if validate_record(record):
            valid_rows.append(record)

    df = pd.DataFrame(valid_rows, columns=columns)
    df["date"] = pd.to_datetime(df["date"])
    df["rate"] = pd.to_numeric(df["rate"])

    df = df[df["base"] == selection]
    df = df[df["quote"] == quote]
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


def get_rates(selection, quote, url, history_days):
    """Fetch latest and historical rates, store in SQLite, and merge."""
    params = {"base": selection, "quotes": quote}
    data = fetch_with_retry(url, params)
    df, latest_report = clean_records(data, selection, quote)

    if len(df) == 0:
        raise RateError(f"No valid latest rate for {selection} to {quote}.")

    df = df.sort_values("date")
    latest_rate = df["rate"].iloc[-1]
    latest_date = df["date"].iloc[-1]

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

    # Store history in SQLite and read back sorted by date
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

    # Merge latest rate onto historical rows
    latest = df[["base", "quote", "rate"]].tail(1)
    latest = latest.rename(columns={"rate": "latest_rate"})

    history = pd.merge(history, latest, on=["base", "quote"])
    history["Today vs this day (%)"] = (
        100 * (history["latest_rate"] - history["rate"]) / history["rate"]
    )

    return latest_rate, latest_date, history, latest_report, history_report


def calculate_exchange(amt, latest_rate, average_rate):
    latest_total = amt * latest_rate
    average_total = amt * average_rate
    difference = latest_total - average_total

    return latest_total, average_total, difference


def calculate_invoice(amt, latest_rate, average_rate):
    latest_cost = amt / latest_rate
    average_cost = amt / average_rate
    savings = average_cost - latest_cost

    return latest_cost, average_cost, savings


def get_recommendation(latest_rate, lower, upper):
    if latest_rate > upper:
        return "Convert now"
    elif latest_rate < lower:
        return "Wait if you can"
    else:
        return "Neutral"


def get_limits(rates, method, band_width, percentile):
    if method == "Percentile":
        lower = np.percentile(rates, percentile)
        upper = np.percentile(rates, 100 - percentile)
    else:
        average = np.mean(rates)
        std = np.std(rates)
        lower = average - band_width * std
        upper = average + band_width * std

    return lower, upper


def summarize_pair(selection, quote, rate, date, history, settings):
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


def scale_to_unit(values):
    low = np.min(values)
    high = np.max(values)
    if high == low:
        return 0.5
    return (values - low) / (high - low)


def rank_currencies(results, change_weight, stability_weight):
    comparison = pd.DataFrame(results)

    comparison["Change score"] = scale_to_unit(comparison["Change (%)"])
    comparison["Stability score"] = 1 - scale_to_unit(comparison["Volatility (%)"])
    comparison["Final score"] = (
        change_weight * comparison["Change score"]
        + stability_weight * comparison["Stability score"]
    )

    return comparison.sort_values("Final score", ascending=False)


def find_anomalies(history):
    average = np.mean(history["rate"])
    std = np.std(history["rate"])

    if std == 0:
        history["z-score"] = 0.0
    else:
        history["z-score"] = (history["rate"] - average) / std

    return history[np.abs(history["z-score"]) > 2]


def what_if(comparison):
    bands = np.arange(0, 2.25, 0.25)
    counts = []

    for band in bands:
        upper = comparison["Average"] + band * comparison["Std"]
        passing = comparison[comparison["Latest"] > upper]
        counts.append(len(passing))

    return bands, counts


def build_report(records, comparison):
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

    return "\n".join(lines)


# Mock request functions for testing
def timeout_request(url, params, timeout):
    raise requests.Timeout()


def disconnected_request(url, params, timeout):
    raise requests.ConnectionError()


def run_tests():
    tests = {}

    latest, average, savings = calculate_invoice(100, 0.25, 0.20)
    tests["Invoice cost"] = (latest == 400)
    tests["Invoice savings"] = (savings == 100)

    latest, average, difference = calculate_exchange(100, 0.25, 0.20)
    tests["Exchange amount"] = (latest == 25)
    tests["Exchange difference"] = (difference == 5)

    tests["Above upper limit"] = (get_recommendation(11, 8, 10) == "Convert now")
    tests["Below lower limit"] = (get_recommendation(7, 8, 10) == "Wait if you can")
    tests["Exactly on limit"] = (get_recommendation(10, 8, 10) == "Neutral")

    record = {"date": "2026-01-10", "base": "AED", "quote": "GBP", "rate": 0.20}
    tests["Valid record accepted"] = validate_record(record)

    record = {"date": "2026-01-10", "base": "AED", "quote": "GBP", "rate": -1}
    tests["Negative rate rejected"] = not validate_record(record)

    record = {"date": "2026-01-10", "base": "AED", "quote": "GBP"}
    tests["Missing rate rejected"] = not validate_record(record)

    record = {"date": "not a date", "base": "AED", "quote": "GBP", "rate": 0.20}
    tests["Bad date rejected"] = not validate_record(record)

    data = [
        {"date": "2026-01-10", "base": "AED", "quote": "GBP", "rate": 0.20},
        {"date": "2026-01-11", "base": "AED", "quote": "GBP", "rate": -1},
        {"date": "2026-01-10", "base": "AED", "quote": "GBP", "rate": 0.20},
        {"date": "2026-01-12", "base": "AED", "quote": "GBP"}
    ]
    df, report = clean_records(data, "AED", "GBP")
    tests["Cleaning keeps only the good row"] = (len(df) == 1)
    tests["Missing rate is counted"] = (report["Missing rates"] == 1)

    rates = pd.Series([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
    lower, upper = get_limits(rates, "Standard deviation", 1.0, 10)
    tests["Standard deviation range is centred on the average"] = (round(lower + upper, 6) == 11)

    lower, upper = get_limits(rates, "Percentile", 1.0, 10)
    tests["Percentile range uses the 10th and 90th percentile"] = (
        round(lower, 6) == 1.9 and round(upper, 6) == 9.1
    )

    history = pd.DataFrame({"rate": [1.0] * 10 + [1.01] * 10 + [2.0]})
    anomalies = find_anomalies(history)
    tests["Anomaly detection finds the outlier"] = (
        len(anomalies) == 1 and anomalies["rate"].iloc[0] == 2.0
    )

    comparison = pd.DataFrame({
        "Latest": [1.10, 1.02, 0.95],
        "Average": [1.00, 1.00, 1.00],
        "Std": [0.05, 0.05, 0.05]
    })
    bands, counts = what_if(comparison)
    tests["What-if: 2 pass at band 0, 0 pass at band 2"] = (counts[0] == 2 and counts[-1] == 0)

    result = fetch_with_retry("test", {}, retries=1, get_request=timeout_request)
    tests["Timeout handled (mock)"] = (result == [])

    result = fetch_with_retry("test", {}, retries=1, get_request=disconnected_request)
    tests["Connection failure handled (mock)"] = (result == [])

    print("(The next test retries a broken URL, so it takes a few seconds.)")
    try:
        get_rates("AED", "GBP", "http://localhost:1", 30)
        tests["RateError raised when there is no data"] = False
    except RateError:
        tests["RateError raised when there is no data"] = True

    passed = 0
    for name in tests:
        if tests[name]:
            print("PASS:", name)
            passed += 1
        else:
            print("FAIL:", name)

    print(passed, "out of", len(tests), "checks passed.")


if __name__ == "__main__":
    run_tests()
