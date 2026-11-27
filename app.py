"""Streamlit app for the currency exchange and supplier invoice agent."""

import time

import matplotlib.pyplot as plt
import pandas as pd
import streamlit as st

from calculations import (
    RateError,
    build_report,
    calculate_exchange,
    calculate_invoice,
    find_anomalies,
    get_limits,
    get_rates,
    get_recommendation,
    load_settings,
    rank_currencies,
    save_settings,
    summarize_pair,
    what_if
)


# Cache API rates for 1 hour (3600s) to avoid repeated calls and rate limits
@st.cache_data(ttl=3600)
def get_saved_rates(selection, quote, url, history_days):
    return get_rates(selection, quote, url, history_days)


def draw_history_chart(ax, history, latest_date, latest_rate, lower, upper, selection, quote):
    ax.plot(history["date"], history["rate"], label="Historical rate")
    ax.scatter(latest_date, latest_rate, color="black", label="Latest rate")
    ax.axhline(upper, color="green", linestyle="--", label="Upper limit")
    ax.axhline(lower, color="red", linestyle="--", label="Lower limit")
    ax.annotate(
        "Convert above this line",
        (history["date"].iloc[0], upper),
        xytext=(4, 5),
        textcoords="offset points"
    )
    ax.set_xlabel("Date")
    ax.set_ylabel(f"{quote} per {selection}")
    ax.set_title(f"{selection} to {quote} rate history")
    ax.tick_params(axis="x", rotation=45)
    ax.legend()


def draw_amount_chart(ax, amounts, ylabel, title):
    bars = ax.bar(["Latest rate", "Average rate"], amounts)
    ax.bar_label(bars, fmt="%.2f", padding=3)
    ax.margins(y=0.15)
    ax.set_ylabel(ylabel)
    ax.set_title(title)


def draw_distribution_chart(ax, history, latest_rate, selection, quote):
    ax.hist(history["rate"], bins=8, edgecolor="black")
    ax.axvline(latest_rate, color="red", linestyle="--", label="Latest rate")
    ax.set_xlabel(f"{quote} per {selection}")
    ax.set_ylabel("Number of observations")
    ax.set_title("Distribution of historical exchange rates")
    ax.legend()


settings = load_settings()
currencies = settings["currencies"]

# Keep saved analyses and ranking alive when switching sidebar pages
if "history" not in st.session_state:
    st.session_state["history"] = []

if "comparison" not in st.session_state:
    st.session_state["comparison"] = pd.DataFrame()

st.title("Currency Exchange and Supplier Invoices")

page = st.sidebar.radio(
    "Menu",
    ["Analyze", "Compare", "History", "Settings", "Export", "Help", "Quit"]
)


if page == "Analyze":
    option = st.radio(
        "What would you like to do?",
        ["Exchange Currency", "Pay Supplier Invoice"]
    )

    selection = st.selectbox("Which currency do you have?", currencies)

    choices = []
    for currency in currencies:
        if currency != selection:
            choices.append(currency)

    if option == "Exchange Currency":
        quote = st.selectbox("Which currency would you like to buy?", choices)
        question = f"How much {selection} would you like to exchange?"
    else:
        quote = st.selectbox("Which currency is the invoice in?", choices)
        question = f"How much {quote} do you owe the supplier?"

    amt = st.number_input(question, min_value=0.01, value=1000.00)

    if option == "Pay Supplier Invoice":
        budget = st.number_input(
            f"How much {selection} did you budget for this invoice?",
            min_value=0.0,
            value=1000.00
        )

    try:
        latest_rate, latest_date, history, latest_report, history_report = get_saved_rates(
            selection, quote, settings["url"], settings["history_days"]
        )
    except RateError as error:
        st.error(str(error))
        st.stop()

    summary = summarize_pair(selection, quote, latest_rate, latest_date, history, settings)

    average_rate = summary["Average"]
    lower = summary["Lower"]
    upper = summary["Upper"]
    decision = summary["Recommendation"]
    confidence = summary["Data coverage (%)"]

    result = {
        "Action": option,
        "Rate date": str(latest_date.date()),
        "Currency you have": selection,
        "Currency you buy": quote,
        "Amount entered": amt,
        "Latest rate": latest_rate,
        "Average rate": average_rate,
        "Recommendation": decision,
        "Data coverage (%)": round(confidence, 1)
    }

    if option == "Exchange Currency":
        latest_total, average_total, difference = calculate_exchange(
            amt, latest_rate, average_rate
        )

        st.subheader("Exchange result")
        st.write(f"At the latest rate, you get {latest_total:,.2f} {quote}.")
        st.write(f"At the average rate, you get {average_total:,.2f} {quote}.")

        if difference >= 0:
            st.write(f"You get {difference:,.2f} {quote} extra compared with the average.")
        else:
            st.write(f"You get {abs(difference):,.2f} {quote} less than average.")

        result["Amount currency"] = selection
        result["Latest total"] = round(latest_total, 2)
        result["Average total"] = round(average_total, 2)
        result["Difference"] = round(difference, 2)
        result["Result currency"] = quote

    else:
        latest_cost, average_cost, savings = calculate_invoice(
            amt, latest_rate, average_rate
        )
        endamount = round(budget - latest_cost, 2)

        st.subheader("Supplier invoice result")
        st.write(f"Invoice amount: {amt:,.2f} {quote}")
        st.write(f"Cost at the latest rate: {latest_cost:,.2f} {selection}")
        st.write(f"Cost at the average rate: {average_cost:,.2f} {selection}")

        if endamount < 0:
            st.warning(f"The invoice is {abs(endamount):,.2f} {selection} over budget.")
        elif endamount == 0:
            st.info("The invoice uses your exact budget.")
        else:
            st.success(f"You have {endamount:,.2f} {selection} left in your budget.")

        if savings >= 0:
            st.write(f"The invoice costs {savings:,.2f} {selection} less than average.")
        else:
            st.write(f"The invoice costs {abs(savings):,.2f} {selection} more than average.")

        result["Amount currency"] = quote
        result["Latest total"] = round(latest_cost, 2)
        result["Average total"] = round(average_cost, 2)
        result["Savings"] = round(savings, 2)
        result["Budget"] = budget
        result["Budget remaining"] = endamount
        result["Result currency"] = selection

    st.subheader("Exchange rate recommendation")
    st.write(f"On {latest_date.date()}, 1 {selection} buys {latest_rate:.5f} {quote}.")
    st.write(f"Average: {average_rate:.5f}")
    st.write(f"Usual range: {lower:.5f} to {upper:.5f}")

    if settings["range_method"] == "Percentile":
        upper_rule = f"{100 - settings['percentile']}th percentile"
        lower_rule = f"{settings['percentile']}th percentile"
    else:
        upper_rule = f"average + {settings['band_width']} standard deviation"
        lower_rule = f"average - {settings['band_width']} standard deviation"

    if decision == "Convert now":
        st.success(
            f"Convert now because the latest rate {latest_rate:.5f} is above "
            f"the upper limit {upper:.5f} ({upper_rule})."
        )
    elif decision == "Wait if you can":
        st.warning(
            f"Wait if you can because the latest rate {latest_rate:.5f} is below "
            f"the lower limit {lower:.5f} ({lower_rule})."
        )
    else:
        st.info(
            f"Neutral because the latest rate {latest_rate:.5f} is between "
            f"{lower:.5f} and {upper:.5f}."
        )

    st.write(
        f"Confidence measure: {confidence:.0f}% historical data coverage "
        f"({len(history)} daily rates in {settings['history_days']} calendar days)."
    )
    st.caption("Coverage describes the available data. Weekends can reduce it.")

    st.subheader("Two ways to set the usual range")
    method_rows = []

    for method in ["Standard deviation", "Percentile"]:
        low, up = get_limits(
            history["rate"], method, settings["band_width"], settings["percentile"]
        )
        method_rows.append({
            "Method": method,
            "Lower limit": round(low, 5),
            "Upper limit": round(up, 5),
            "Recommendation": get_recommendation(latest_rate, low, up)
        })

    st.dataframe(pd.DataFrame(method_rows), hide_index=True)
    st.caption(f"The app uses {settings['range_method']}. You can change it under Settings.")

    st.subheader("Should you use another currency?")
    check_others = st.checkbox("Check the other currencies", value=True)

    if check_others:
        rows = []

        own_row = dict(summary)
        own_row["Option"] = "Your choice"
        if option == "Pay Supplier Invoice":
            own_row["Invoice in this currency"] = round(amt, 2)
            own_row[f"Saving vs average ({selection})"] = round(savings, 2)
        rows.append(own_row)

        for other in currencies:
            if other != selection and other != quote:
                try:
                    other_rate, other_date, other_history, r1, r2 = get_saved_rates(
                        selection, other, settings["url"], settings["history_days"]
                    )
                    row = summarize_pair(
                        selection, other, other_rate, other_date, other_history, settings
                    )
                    row["Option"] = "Alternative"

                    if option == "Pay Supplier Invoice":
                        other_amount = amt * other_rate / latest_rate
                        cost_now = other_amount / other_rate
                        cost_average = other_amount / row["Average"]
                        row["Invoice in this currency"] = round(other_amount, 2)
                        row[f"Saving vs average ({selection})"] = round(cost_average - cost_now, 2)

                    rows.append(row)

                except RateError as error:
                    st.warning(str(error))

        table = rank_currencies(rows, settings["change_weight"], settings["stability_weight"])

        show_columns = ["Option", "Currency", "Latest", "Average", "Change (%)", "Recommendation"]
        if option == "Pay Supplier Invoice":
            show_columns.append("Invoice in this currency")
            show_columns.append(f"Saving vs average ({selection})")
        show_columns.append("Final score")

        st.dataframe(table[show_columns].round(3), hide_index=True)

        passing = table[table["Recommendation"] == "Convert now"]
        passing = passing[passing["Currency"] != quote]

        if decision == "Convert now":
            best = quote
            st.success(
                f"{selection} to {quote} already passes the convert-now rule, "
                f"so there is no need to switch currency."
            )
        elif len(passing) > 0:
            best_row = passing.iloc[0]
            best = best_row["Currency"]

            if option == "Pay Supplier Invoice":
                best_saving = best_row[f"Saving vs average ({selection})"]
                st.info(
                    f"{selection} to {quote} does not pass the convert-now rule ({decision}). "
                    f"{best} does: its rate is {best_row['Change (%)']:.2f}% above its average. "
                    f"If your supplier accepts {best}, the same invoice is "
                    f"{best_row['Invoice in this currency']:,.2f} {best}, which saves "
                    f"{best_saving:,.2f} {selection} compared with its usual rate "
                    f"(paying in {quote} saves {savings:,.2f} {selection})."
                )
            else:
                st.info(
                    f"{selection} to {quote} does not pass the convert-now rule ({decision}). "
                    f"If you can use {best} instead, its rate is "
                    f"{best_row['Change (%)']:.2f}% above its average."
                )
        else:
            best = "None"
            st.warning(
                "No other currency passes the convert-now rule either. "
                "Waiting is the safer choice if your deadline allows it."
            )

        st.caption(
            "All rates come from one source, so today's cost is the same in every currency. "
            "The difference is how good each rate is compared with its own usual range."
        )

        result["Best alternative"] = best

    if st.button("Save this result to history"):
        st.session_state["history"].append(result)
        st.success("Result saved. You can download it under Export.")

    st.subheader("Retrieved data")
    anomalies = find_anomalies(history)
    st.dataframe(history, hide_index=True)

    st.subheader("Unusual days")
    if len(anomalies) == 0:
        st.write("No day had a z-score above 2 or below -2.")
    else:
        st.dataframe(anomalies[["date", "rate", "z-score"]].round(3), hide_index=True)

    st.subheader("Data quality report")
    report = pd.DataFrame(
        [latest_report, history_report],
        index=["Latest rate", "Historical rates"]
    )
    st.dataframe(report)
    st.write("Rows with missing or invalid values were excluded. Duplicate dates were removed.")

    st.subheader("Charts")
    chart = st.selectbox(
        "Which chart would you like to see?",
        ["Rate history", "Amount comparison", "Rate distribution", "Dashboard (all three)"]
    )

    if option == "Pay Supplier Invoice":
        amounts = [latest_cost, average_cost]
        amount_ylabel = f"Invoice cost in {selection}"
        amount_title = f"Cost of a {amt:,.2f} {quote} invoice"
    else:
        amounts = [latest_total, average_total]
        amount_ylabel = f"Amount received in {quote}"
        amount_title = f"Exchanging {amt:,.2f} {selection}"

    if chart == "Dashboard (all three)":
        fig, axes = plt.subplots(1, 3, figsize=(16, 5))
        draw_history_chart(axes[0], history, latest_date, latest_rate, lower, upper, selection, quote)
        draw_amount_chart(axes[1], amounts, amount_ylabel, amount_title)
        draw_distribution_chart(axes[2], history, latest_rate, selection, quote)
        filename = f"dashboard_{selection}_{quote}_{time.strftime('%Y%m%d_%H%M%S')}.png"
    else:
        fig, ax = plt.subplots()

        if chart == "Rate history":
            draw_history_chart(ax, history, latest_date, latest_rate, lower, upper, selection, quote)
        elif chart == "Amount comparison":
            draw_amount_chart(ax, amounts, amount_ylabel, amount_title)
        else:
            draw_distribution_chart(ax, history, latest_rate, selection, quote)

        filename = f"{selection}_{quote}_{option}_{chart}.png"

    fig.tight_layout()
    st.pyplot(fig)

    fig.savefig(filename)
    file = open(filename, "rb")
    st.download_button("Download this chart", file.read(), file_name=filename)
    file.close()

    plt.close(fig)


elif page == "Compare":
    st.header("Compare currencies")
    selection = st.selectbox("Which currency do you have?", currencies)

    if st.button("Compare the five currencies"):
        results = []

        for quote in currencies:
            if quote != selection:
                try:
                    rate, date, history, latest_report, history_report = get_saved_rates(
                        selection, quote, settings["url"], settings["history_days"]
                    )
                    results.append(
                        summarize_pair(selection, quote, rate, date, history, settings)
                    )
                except RateError as error:
                    st.warning(str(error))

        if len(results) > 0:
            comparison = rank_currencies(
                results, settings["change_weight"], settings["stability_weight"]
            )
            st.session_state["comparison"] = comparison
        else:
            st.session_state["comparison"] = pd.DataFrame()
            st.warning("No currencies were available to compare.")

    comparison = st.session_state["comparison"]

    if len(comparison) > 0 and comparison["Base"].iloc[0] == selection:
        st.dataframe(comparison.round(3), hide_index=True)
        st.write(
            f"The score gives {settings['change_weight']}% weight to improvement "
            f"and {settings['stability_weight']}% to stability."
        )

        passing = comparison[comparison["Recommendation"] == "Convert now"]

        if len(passing) == 0:
            st.info("No currency currently passes the convert-now rule.")
        else:
            best = passing["Currency"].iloc[0]
            st.success(f"Best passing option if you can choose the currency: {best}.")

        st.caption("For a supplier invoice, use the currency on the bill.")

        st.subheader("What if the usual range was wider or narrower?")
        bands, counts = what_if(comparison)

        fig, ax = plt.subplots()
        ax.plot(bands, counts, marker="o")
        ax.axvline(settings["band_width"], color="red", linestyle="--", label="Current setting")
        ax.set_xlabel("Number of standard deviations")
        ax.set_ylabel("Currencies that pass convert now")
        ax.set_title(f"What-if analysis for {selection}")
        ax.legend()
        fig.tight_layout()
        st.pyplot(fig)
        fig.savefig(f"what_if_{selection}.png")
        plt.close(fig)

        st.caption("A wider range is stricter, so fewer currencies pass.")


elif page == "History":
    st.header("Session history")

    if len(st.session_state["history"]) == 0:
        st.write("No saved results yet. Use Analyze and save a result.")
    else:
        df = pd.DataFrame(st.session_state["history"])
        st.dataframe(df, hide_index=True)


elif page == "Settings":
    st.header("Settings")

    method = st.radio(
        "How should the usual range be set?",
        ["Standard deviation", "Percentile"],
        index=["Standard deviation", "Percentile"].index(settings["range_method"])
    )

    band = st.number_input(
        "Number of standard deviations for the usual range",
        min_value=0.1,
        max_value=3.0,
        value=float(settings["band_width"]),
        step=0.1
    )

    percentile = st.number_input(
        "Percentile for the lower limit (the upper limit is 100 minus this)",
        min_value=1,
        max_value=49,
        value=int(settings["percentile"]),
        step=1
    )

    weight = st.slider(
        "Weight given to rate improvement (%)",
        min_value=0,
        max_value=100,
        value=settings["change_weight"]
    )

    st.write(f"Weight given to stability: {100 - weight}%")

    if st.button("Save settings"):
        settings["range_method"] = method
        settings["band_width"] = band
        settings["percentile"] = percentile
        settings["change_weight"] = weight
        settings["stability_weight"] = 100 - weight
        save_settings(settings)
        st.session_state["comparison"] = pd.DataFrame()
        st.success("Settings saved.")


elif page == "Export":
    st.header("Export results")

    comparison = st.session_state["comparison"]

    if len(st.session_state["history"]) > 0:
        df = pd.DataFrame(st.session_state["history"])
        st.download_button(
            "Download saved analyses as CSV",
            df.to_csv(index=False),
            file_name="exchange_results.csv",
            mime="text/csv"
        )

        try:
            df.to_excel("exchange_results.xlsx", index=False)
            file = open("exchange_results.xlsx", "rb")
            st.download_button(
                "Download saved analyses as Excel",
                file.read(),
                file_name="exchange_results.xlsx"
            )
            file.close()
        except ImportError:
            st.info("To export to Excel, install openpyxl: pip install openpyxl")
    else:
        st.write("Save an analysis under Analyze to export it.")

    if len(comparison) > 0:
        st.download_button(
            "Download currency ranking as CSV",
            comparison.to_csv(index=False),
            file_name="currency_ranking.csv",
            mime="text/csv"
        )

    report_text = build_report(st.session_state["history"], comparison)
    st.download_button(
        "Download summary report (Markdown)",
        report_text,
        file_name="exchange_report.md",
        mime="text/markdown"
    )


elif page == "Help":
    st.header("How to use the app")
    st.write("Analyze: exchange money or check the cost of a supplier invoice. "
             "It also checks whether another currency is a better choice right now.")
    st.write("Compare: rank five currencies by improvement and stability.")
    st.write("History: view the results you saved during this session.")
    st.write("Settings: choose standard deviation or percentile limits and the ranking weights.")
    st.write("Export: download your results as CSV, Excel or a Markdown report.")
    st.write("Quit: end this session and clear its saved results.")
    st.write("Rates are estimates. Bank fees and invoice deadlines can affect your decision.")


elif page == "Quit":
    st.header("End session")
    st.write("Download any results you want to keep before ending the session.")

    if st.button("End and clear this session"):
        st.session_state["history"] = []
        st.session_state["comparison"] = pd.DataFrame()
        st.success("Session ended. Select Analyze to start again.")
