# Currency exchange and supplier invoice agent

BUAI250 final project, currency exchange option.

You tell the app which currency you have and what you need: either a currency to buy, or a supplier invoice to pay. It pulls real exchange rates, compares today's rate with the last 30 days, and tells you whether to convert now, wait, or stay neutral. Then it checks the other currencies to see whether one of them is a better choice right now. Every recommendation comes with the rule that triggered it and the actual numbers.

## Files

| File | What it does |
|---|---|
| `app.py` | The Streamlit app: menu, screens, charts and downloads |
| `calculations.py` | API calls, cleaning, SQL, rules, scoring and the tests |
| `config.json` | Settings (created automatically on the first run) |
| `exchange_rates.db` | SQLite database with one table per currency pair (created automatically) |

## Packages

```
pip install streamlit pandas numpy matplotlib requests
```

Optional, only for the Excel export:

```
pip install openpyxl
```

The app still works without openpyxl. The Export screen just tells you to install it.

## How to run

Open a terminal in the project folder.

Run the app:

```
streamlit run app.py
```

Run the tests:

```
python calculations.py
```

On Windows, if `python` isn't found, use `py -3` instead (for example `py -3 -m streamlit run app.py`).

There are 20 tests. One of them sends a request to a broken address on purpose, so the run takes a few seconds while it retries.

## API documentation

I use the Frankfurter API (https://frankfurter.dev).

| | |
|---|---|
| Endpoint | `https://api.frankfurter.dev/v2/rates` |
| API key | None needed |
| Cost | Free, including commercial use |
| Rate limits | No daily or monthly quota. Requests are rate-limited to stop abuse, but the docs don't give a number. My code handles a 429 response by waiting and trying again. |
| Data | Reference rates blended from many central banks and official sources. These are not live market quotes. |

Parameters I use:

| Parameter | Example | Meaning |
|---|---|---|
| `base` | `AED` | The currency you have |
| `quotes` | `GBP` | The currency you want a rate for |
| `from` | `2026-10-25` | Start of the history period |
| `to` | `2026-11-23` | End of the history period |

Each currency pair needs two calls. The first one has no dates and returns the latest rate. The second one uses `from` and `to` to get the 30 days before that. The API returns a list of records like this:

```
{"date": "2026-11-24", "base": "AED", "quote": "GBP", "rate": 0.20816}
```

Error handling in `fetch_with_retry`:

| Problem | What happens |
|---|---|
| 404 | Message, no retry (the pair doesn't exist) |
| 429 | Message, wait, try again |
| 500 | Message, wait, try again |
| Timeout | Message, wait, try again |
| No internet | Message, wait, try again |

The wait gets longer each time (1 second, then 2), and the code gives up after 3 tries. If there's still no data, `get_rates` raises my own `RateError`, and the app shows the message without crashing.

## Settings (config.json)

| Key | Default | Meaning |
|---|---|---|
| `url` | Frankfurter v2 rates | API address |
| `currencies` | GBP, USD, JPY, AED, CAD, EUR | Currencies in the menus |
| `history_days` | 30 | Days of history |
| `range_method` | Standard deviation | Or Percentile |
| `band_width` | 1.0 | Standard deviations for the usual range |
| `percentile` | 10 | Lower limit percentile (upper is 100 minus this) |
| `change_weight` | 70 | Weight for rate improvement in the score |
| `stability_weight` | 30 | Weight for stability in the score |

You can change all of these on the Settings screen, and they're saved for the next run.

## Menu

Analyze, Compare, History, Settings, Export, Help and Quit.

## Challenge tasks

Points are left blank because my copy of the brief doesn't list them.

| Task | Area | Where it is | Points |
|---|---|---|---|
| C1 Merge two sources | Data retrieval | `get_rates`: the latest-rate call and the history call are joined with `pd.merge` on base and quote | |
| C3 Anomaly detection | Analysis | `find_anomalies`: z-score per day, days above 2 or below -2 are listed in Analyze | |
| C4 Percentile thresholds | Analysis | `get_limits`: standard deviation or percentile range, both shown side by side in Analyze | |
| C5 What-if analysis | Analysis | `what_if`: counts passing currencies for band widths 0 to 2, plotted in Compare | |
| C6 Business impact | Recommendation | Invoice budget left over, and savings in your own currency for every alternative | |
| C8 Saved preferences | Interaction | Settings are stored in `config.json` and loaded on the next run | |
| C9 Chart dashboard | Output | "Dashboard (all three)" chart, saved with a timestamp in the file name | |
| C10 Auto-generated report | Output | Export screen: Markdown report, Excel and CSV | |
| Graphical interface | Extra idea | Streamlit | |
