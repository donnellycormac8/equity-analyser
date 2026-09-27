"""Equity Analyser - fetch and display fundamental data for a stock ticker."""

import sys
import textwrap
from datetime import date
from pathlib import Path
from pprint import pprint
from xml.sax.saxutils import escape

import matplotlib

matplotlib.use("Agg")  # draw charts straight to image files, no pop-up window

import matplotlib.dates as mdates
import matplotlib.pyplot as plt

# Match the PDF's Helvetica; later names are fallbacks if it isn't installed.
plt.rcParams["font.family"] = ["Helvetica", "Arial", "DejaVu Sans"]
import pandas as pd
import yfinance as yf
from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.platypus import (
    HRFlowable,
    Image,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

# S&P 500 Total Return index: the S&P 500 with dividends reinvested, so it
# compares fairly with a stock's dividend-adjusted price history.
BENCHMARK_TICKER = "^SP500TR"
BENCHMARK_NAME = "S&P 500 (total return)"

CHART_DIR = Path(__file__).parent / "charts"

# Chart colours: two categorical series plus neutral inks and surfaces.
SERIES_STOCK = "#2a78d6"
SERIES_BENCHMARK = "#eb6834"
TEXT_PRIMARY = "#0b0b0b"
TEXT_SECONDARY = "#52514e"
GRID_COLOR = "#e6e5e1"
SURFACE = "#fcfcfb"


def fetch_raw_data(ticker: str) -> dict:
    """Fetch the raw info dictionary for a ticker from Yahoo Finance."""
    stock = yf.Ticker(ticker)
    return stock.info


def get_snapshot(data: dict) -> dict:
    """Pull the company snapshot fields out of the raw info dictionary."""
    return {
        "name": data.get("longName") or data.get("shortName"),
        "sector": data.get("sector"),
        "industry": data.get("industry"),
        "exchange": data.get("fullExchangeName"),
        # Some tickers (e.g. ETFs) have no currentPrice, so fall back.
        "price": data.get("currentPrice") or data.get("regularMarketPrice"),
        "currency": data.get("currency"),
        "market_cap": data.get("marketCap"),
    }


def format_large_number(value: float | None) -> str:
    """Format a big number as e.g. 3.83T, 250.10B, 45.00M."""
    if value is None:
        return "N/A"
    for threshold, suffix in [(1e12, "T"), (1e9, "B"), (1e6, "M")]:
        if abs(value) >= threshold:
            return f"{value / threshold:.2f}{suffix}"
    return f"{value:,.0f}"


def format_price(value: float | None, currency: str | None) -> str:
    """Format a price with its currency, e.g. 516.17 USD."""
    if value is None:
        return "N/A"
    return f"{value:,.2f} {currency or ''}".strip()


def print_snapshot(snapshot: dict) -> None:
    """Print the company snapshot in a readable layout."""
    print("=== Company Snapshot ===")
    print(f"Name:       {snapshot['name'] or 'N/A'}")
    print(f"Sector:     {snapshot['sector'] or 'N/A'}")
    print(f"Industry:   {snapshot['industry'] or 'N/A'}")
    print(f"Price:      {format_price(snapshot['price'], snapshot['currency'])}")
    print(f"Market cap: {format_large_number(snapshot['market_cap'])}")


def get_valuation(data: dict) -> dict:
    """Pull the valuation ratios out of the raw info dictionary."""
    return {
        "pe_trailing": data.get("trailingPE"),
        "pe_forward": data.get("forwardPE"),
        "ev_ebitda": data.get("enterpriseToEbitda"),
        "price_to_book": data.get("priceToBook"),
        "price_to_sales": data.get("priceToSalesTrailing12Months"),
    }


def format_ratio(value: float | None, decimals: int = 2) -> str:
    """Format a valuation multiple as e.g. 28.74x."""
    if value is None:
        return "N/A"
    return f"{value:.{decimals}f}x"


def print_valuation(valuation: dict) -> None:
    """Print the valuation ratios in a readable layout."""
    print("=== Valuation ===")
    print(f"P/E (trailing): {format_ratio(valuation['pe_trailing'])}")
    print(f"P/E (forward):  {format_ratio(valuation['pe_forward'])}")
    print(f"EV/EBITDA:      {format_ratio(valuation['ev_ebitda'])}")
    print(f"P/B:            {format_ratio(valuation['price_to_book'])}")
    print(f"P/S:            {format_ratio(valuation['price_to_sales'])}")


# Yahoo industries whose balance sheet *is* the business: banks, insurers,
# mortgage lenders and broker-dealers. Matched on the start of the name, so
# "Insurance - " covers Life, Property & Casualty, Reinsurance, etc., but
# not "Insurance Brokers", which are ordinary fee-earning businesses.
BALANCE_SHEET_INDUSTRIES = (
    "Banks - ",
    "Insurance - ",
    "Mortgage Finance",
    "Capital Markets",
)


def not_applicable_note(data: dict) -> str | None:
    """Return a note if industrial-style metrics don't apply to this company.

    Banks, insurers and similar firms have no cost of goods sold, and their
    balance sheets (deposits and policy reserves, with no current/non-current
    split) make gross margin, debt/equity and current ratio meaningless.
    Other financials, such as payment networks and asset managers, are
    ordinary businesses where these metrics still apply.
    """
    industry = data.get("industry")
    if industry and industry.startswith(BALANCE_SHEET_INDUSTRIES):
        return f"not applicable for {industry}"

    # Some lenders sit in mixed industries (e.g. Capital One is in "Credit
    # Services" alongside Visa). Yahoo gives them a gross margin of exactly 0,
    # which no real business has, so treat that as a bank-like signal.
    if data.get("sector") == "Financial Services" and data.get("grossMargins") == 0:
        return "not applicable for bank-like lenders"
    return None


def with_note(text: str, note: str | None) -> str:
    """Append a note in brackets to a formatted value, if there is one."""
    if note:
        return f"{text} ({note})"
    return text


def get_profitability(data: dict) -> dict:
    """Pull the margin and return ratios out of the raw info dictionary."""
    note = not_applicable_note(data)

    return {
        "gross_margin": None if note else data.get("grossMargins"),
        "gross_margin_note": note,
        "operating_margin": data.get("operatingMargins"),
        "net_margin": data.get("profitMargins"),
        "roe": data.get("returnOnEquity"),
        "roa": data.get("returnOnAssets"),
    }


def format_percent(value: float | None, signed: bool = False, decimals: int = 2) -> str:
    """Format a decimal as a percentage, e.g. 0.4031 -> 40.31%.

    With signed=True, positive values get a leading + (e.g. +40.31%).
    """
    if value is None:
        return "N/A"
    sign = "+" if signed else ""
    return f"{value:{sign}.{decimals}%}"


def print_profitability(profitability: dict) -> None:
    """Print the profitability ratios in a readable layout."""
    gross_text = with_note(
        format_percent(profitability["gross_margin"]),
        profitability["gross_margin_note"],
    )

    print("=== Profitability ===")
    print(f"Gross margin:     {gross_text}")
    print(f"Operating margin: {format_percent(profitability['operating_margin'])}")
    print(f"Net margin:       {format_percent(profitability['net_margin'])}")
    print(f"ROE:              {format_percent(profitability['roe'])}")
    print(f"ROA:              {format_percent(profitability['roa'])}")


def get_growth(data: dict) -> dict:
    """Pull the growth rates out of the raw info dictionary.

    Yahoo reports these for the latest quarter vs the same quarter a year ago.
    """
    return {
        "revenue_growth": data.get("revenueGrowth"),
        "eps_growth": data.get("earningsGrowth"),
    }


def print_growth(growth: dict) -> None:
    """Print the growth rates in a readable layout."""
    print("=== Growth (latest quarter, YoY) ===")
    print(f"Revenue growth: {format_percent(growth['revenue_growth'])}")
    print(f"EPS growth:     {format_percent(growth['eps_growth'])}")


def get_financial_health(data: dict) -> dict:
    """Pull the balance-sheet health ratios out of the raw info dictionary."""
    note = not_applicable_note(data)

    # Yahoo reports debt/equity as a percentage (29.1 means 0.291x),
    # so convert it to a plain ratio to match the other multiples.
    debt_to_equity = data.get("debtToEquity")
    if debt_to_equity is not None:
        debt_to_equity = debt_to_equity / 100

    return {
        "debt_to_equity": None if note else debt_to_equity,
        "debt_to_equity_note": note,
        "current_ratio": None if note else data.get("currentRatio"),
        "current_ratio_note": note,
    }


def print_financial_health(health: dict) -> None:
    """Print the financial health ratios in a readable layout."""
    de_text = with_note(
        format_ratio(health["debt_to_equity"]), health["debt_to_equity_note"]
    )
    cr_text = with_note(
        format_ratio(health["current_ratio"]), health["current_ratio_note"]
    )

    print("=== Financial Health ===")
    print(f"Debt/equity:   {de_text}")
    print(f"Current ratio: {cr_text}")


def fetch_price_history(ticker: str, period: str = "5y") -> pd.DataFrame:
    """Fetch daily price history for a ticker.

    'Close' is the price as quoted each day (adjusted for splits only).
    'Adj Close' also adjusts for dividends, so it tracks total return.
    """
    history = yf.Ticker(ticker).history(period=period, auto_adjust=False)
    if history.empty:
        return history

    # Drop the time and time zone, keeping just the date, so histories from
    # different exchanges (e.g. Toronto vs New York) line up day by day.
    history.index = pd.to_datetime(history.index.date)
    return history


def fetch_fx_history(currency: str | None, period: str = "5y") -> pd.Series | None:
    """Fetch daily exchange rates as units of `currency` per 1 USD.

    Returns None for USD stocks (nothing to convert) or if the fetch fails.
    Yahoo names these tickers e.g. CAD=X, EUR=X, GBP=X.
    """
    if not currency or currency == "USD":
        return None
    # London prices are quoted in pence ("GBp"). Upper-casing to GBP is safe
    # for returns: the constant x100 factor cancels out when we rebase to 100.
    history = fetch_price_history(f"{currency.upper()}=X", period)
    if history.empty:
        return None
    return history["Close"]


def get_price_context(
    data: dict,
    stock_history: pd.DataFrame,
    benchmark_history: pd.DataFrame,
    fx_history: pd.Series | None = None,
) -> dict:
    """Work out 52-week range and performance vs the S&P 500.

    If fx_history is given, the stock's prices are converted to USD before
    comparing, so both sides of the comparison are in the same currency.
    """
    price = data.get("currentPrice") or data.get("regularMarketPrice")
    high = data.get("fiftyTwoWeekHigh")
    low = data.get("fiftyTwoWeekLow")
    from_high = price / high - 1 if price and high else None

    context = {
        "currency": data.get("currency"),
        "high_52w": high,
        "low_52w": low,
        "from_high": from_high,
        "price_history": None if stock_history.empty else stock_history["Close"],
        "converted_to_usd": False,
        "indexed": None,
        "stock_return": None,
        "stock_return_local": None,
        "benchmark_return": None,
        "relative": None,
        "start_date": None,
        "end_date": None,
    }
    if stock_history.empty or benchmark_history.empty:
        return context

    # Put the dividend-adjusted series (and FX rate, if any) side by side.
    # join="inner" keeps only the dates all of them have, so every
    # comparison is like-for-like.
    series = {
        "stock": stock_history["Adj Close"],
        "benchmark": benchmark_history["Adj Close"],
    }
    if fx_history is not None:
        series["fx"] = fx_history
    combined = pd.concat(series, axis=1, join="inner").dropna()
    if combined.empty:
        return context

    # Keep the local-currency return, then convert the stock to USD.
    # The FX rate is local currency per 1 USD, so divide to get USD.
    stock_return_local = combined["stock"].iloc[-1] / combined["stock"].iloc[0] - 1
    if "fx" in combined:
        combined["stock"] = combined["stock"] / combined["fx"]
        context["converted_to_usd"] = True

    # Rebase both to 100 on the first shared day: "what 100 invested
    # at the start would be worth" for each.
    both = combined[["stock", "benchmark"]]
    indexed = both / both.iloc[0] * 100
    stock_return = indexed["stock"].iloc[-1] / 100 - 1
    benchmark_return = indexed["benchmark"].iloc[-1] / 100 - 1

    context.update(
        {
            "indexed": indexed,
            "stock_return": stock_return,
            "stock_return_local": stock_return_local,
            "benchmark_return": benchmark_return,
            "relative": stock_return - benchmark_return,
            "start_date": indexed.index[0].date(),
            "end_date": indexed.index[-1].date(),
        }
    )
    return context


def print_price_context(context: dict, ticker: str) -> None:
    """Print the 52-week range and 5-year performance comparison."""
    currency = context["currency"]

    print("=== Price Context ===")
    print(f"52-week high:      {format_price(context['high_52w'], currency)}")
    print(f"52-week low:       {format_price(context['low_52w'], currency)}")
    print(f"From 52-week high: {format_percent(context['from_high'], signed=True)}")

    if context["indexed"] is None:
        print("Performance vs S&P 500: N/A (price history unavailable)")
        return

    # Label with the actual dates: a recent listing has under 5 years of data.
    relative = context["relative"]
    print()
    print(
        f"Total return ({context['start_date']} to {context['end_date']}, "
        "dividends reinvested):"
    )
    if context["converted_to_usd"]:
        local_label = f"{ticker} (in {currency})"
        print(f"  {local_label:<24}{format_percent(context['stock_return_local'], signed=True)}")
        stock_label = f"{ticker} (in USD)"
    else:
        stock_label = ticker
    print(f"  {stock_label:<24}{format_percent(context['stock_return'], signed=True)}")
    print(f"  {BENCHMARK_NAME:<24}{format_percent(context['benchmark_return'], signed=True)}")
    print(f"  {'Difference':<24}{relative * 100:+.2f} percentage points")

    if context["converted_to_usd"]:
        print(f"  Comparison uses USD, converted with {currency.upper()}=X exchange rates.")
    elif currency and currency != "USD":
        print(
            f"  Note: {ticker} returns are in {currency}; the S&P 500 is in USD, "
            "so currency moves affect the comparison (exchange rates unavailable)."
        )


def style_axes(ax: plt.Axes, surface: str = SURFACE, labelsize: float = 9) -> None:
    """Apply a quiet, consistent look: light grid, no top/right border."""
    ax.set_facecolor(surface)
    ax.grid(axis="y", color=GRID_COLOR, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID_COLOR)
    ax.tick_params(colors=TEXT_SECONDARY, labelsize=labelsize, length=0)


def plot_price_context(context: dict, ticker: str, compact: bool = False) -> Path | None:
    """Save a two-panel chart: price history, and growth of 100 vs the S&P 500.

    compact=True draws the panels side by side at print size on white,
    for embedding in the PDF report.
    """
    if context["indexed"] is None:
        return None

    if compact:
        fig, (ax_price, ax_perf) = plt.subplots(
            1, 2, figsize=(7.4, 3.1), facecolor="white",
            gridspec_kw={"wspace": 0.2},
        )
        surface, title_size, text_size = "white", 9, 7
        price_title = f"Share price ({context['currency'] or 'N/A'})"
        perf_title = "Growth of 100 vs S&P 500"
    else:
        fig, (ax_price, ax_perf) = plt.subplots(
            2, 1, figsize=(10, 7.5), sharex=True, facecolor=SURFACE,
            gridspec_kw={"hspace": 0.35},
        )
        surface, title_size, text_size = SURFACE, 12, 9
        price_title = f"{ticker} share price ({context['currency'] or 'N/A'})"
        perf_title = "Growth of 100 invested, dividends reinvested"

    # First panel: the share price as it was actually quoted.
    prices = context["price_history"]
    ax_price.plot(prices.index, prices, color=SERIES_STOCK, linewidth=1.5)
    ax_price.set_title(
        price_title,
        loc="left", fontsize=title_size, color=TEXT_PRIMARY, fontweight="bold",
    )

    # Second panel: both series rebased to 100, so they share one scale.
    indexed = context["indexed"]
    ax_perf.axhline(100, color=TEXT_SECONDARY, linewidth=0.8, linestyle=(0, (4, 3)))
    lines = [
        ("stock", ticker, SERIES_STOCK),
        ("benchmark", BENCHMARK_NAME, SERIES_BENCHMARK),
    ]
    for column, label, color in lines:
        ax_perf.plot(indexed.index, indexed[column], color=color, linewidth=1.5, label=label)

    # Direct labels with each line's final value. If the two lines finish
    # close together, push the higher label up and the lower one down.
    final = indexed.iloc[-1]
    y_min, y_max = indexed.min().min(), indexed.max().max()
    too_close = abs(final["stock"] - final["benchmark"]) < (y_max - y_min) * 0.05
    for column, _, _ in lines:
        other = "benchmark" if column == "stock" else "stock"
        nudge = (6 if final[column] >= final[other] else -6) if too_close else 0
        ax_perf.annotate(
            f"{final[column]:.0f}",
            xy=(indexed.index[-1], final[column]),
            xytext=(6 if not compact else 4, nudge), textcoords="offset points",
            va="center", fontsize=text_size, color=TEXT_PRIMARY,
        )

    if context["converted_to_usd"]:
        perf_title += " (in USD)"
    ax_perf.set_title(
        perf_title,
        loc="left", fontsize=title_size, color=TEXT_PRIMARY, fontweight="bold",
    )
    # Legend sits below the plot, under the year labels, so it can't hide
    # data or collide with the title however long either gets.
    ax_perf.legend(
        loc="upper left", bbox_to_anchor=(0, -0.1), ncol=2,
        frameon=False, fontsize=text_size, labelcolor=TEXT_SECONDARY,
    )

    for ax in (ax_price, ax_perf):
        style_axes(ax, surface, labelsize=text_size)
        ax.xaxis.set_major_locator(mdates.YearLocator())
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))

    CHART_DIR.mkdir(exist_ok=True)
    suffix = "_print" if compact else ""
    path = CHART_DIR / f"{ticker}_price_context{suffix}.png"
    # 300 dpi for print so lines stay crisp; 150 is plenty for screen.
    fig.savefig(path, dpi=300 if compact else 150, bbox_inches="tight", facecolor=surface)
    plt.close(fig)
    return path


# Rules of thumb for the written summary. These are broad market conventions,
# not sector-specific benchmarks, so treat the wording as a starting point.
PE_TYPICAL_RANGE = (15, 25)  # roughly where large US stocks have traded
NET_MARGIN_STRONG = 0.20
NET_MARGIN_HEALTHY = 0.10
REVENUE_GROWTH_STRONG = 0.15
REVENUE_GROWTH_MODERATE = 0.05
DEBT_EQUITY_LOW = 0.5
DEBT_EQUITY_MODERATE = 1.5


def pct(value: float) -> str:
    """Short percentage for prose, e.g. 0.4031 -> 40.3%."""
    return format_percent(value, decimals=1)


def summarise_valuation(report: dict) -> str | None:
    """One sentence placing the P/E against the typical range."""
    name = report["snapshot"]["name"] or report["ticker"]
    valuation = report["valuation"]
    trailing, forward = valuation["pe_trailing"], valuation["pe_forward"]
    low, high = PE_TYPICAL_RANGE

    if trailing and trailing > 0:
        if trailing < low:
            position = (
                f"below the typical {low}\u2013{high}x range for large companies, a "
                "discount that usually reflects modest growth expectations or "
                "higher perceived risk"
            )
        elif trailing <= high:
            position = f"within the typical {low}\u2013{high}x range for large companies"
        else:
            position = (
                f"above the typical {low}\u2013{high}x range for large companies, a "
                "premium that implies the market expects above-average growth"
            )
        sentence = f"{name} trades at {trailing:.1f}x trailing earnings, {position}"

        # Only mention the forward P/E when it differs meaningfully.
        if forward and 0 < forward < trailing * 0.9:
            sentence += f"; a forward P/E of {forward:.1f}x shows analysts expect earnings to rise"
        elif forward and forward > trailing * 1.1:
            sentence += f"; a higher forward P/E of {forward:.1f}x shows analysts expect earnings to fall"
        return sentence + "."

    # No positive earnings, so P/E is meaningless: fall back to price/sales.
    if valuation["price_to_sales"]:
        return (
            f"{name} has no positive trailing earnings, so P/E is not meaningful; "
            f"it is valued at {valuation['price_to_sales']:.1f}x sales."
        )
    return None


def summarise_profitability(report: dict) -> str | None:
    """One sentence on margin quality."""
    profitability = report["profitability"]
    net, operating, roe = (
        profitability["net_margin"],
        profitability["operating_margin"],
        profitability["roe"],
    )
    if net is None:
        return None

    if net < 0:
        sentence = f"The business is currently loss-making, with a net margin of {pct(net)}"
        if operating is not None:
            sentence += f" and an operating margin of {pct(operating)}"
        return sentence + "."

    # The margin bands are calibrated for ordinary businesses. A bank's
    # "revenue" is mostly net interest income, so state its figures plainly.
    if profitability["gross_margin_note"]:
        sentence = f"It keeps {pct(net)} of revenue as net profit"
    else:
        if net >= NET_MARGIN_STRONG:
            quality = "exceptionally strong"
        elif net >= NET_MARGIN_HEALTHY:
            quality = "healthy"
        else:
            quality = "thin"
        sentence = f"Margins are {quality}: it keeps {pct(net)} of revenue as net profit"

    # ROE above 100% usually means equity has been shrunk by buybacks, not
    # that returns are extraordinary, so leave it out rather than mislead.
    if roe is not None and 0 < roe <= 1:
        sentence += f" and earns a {pct(roe)} return on shareholders' equity"
    return sentence + "."


def summarise_growth(report: dict) -> str | None:
    """One sentence on the revenue trend and whether earnings are keeping up."""
    revenue, eps = report["growth"]["revenue_growth"], report["growth"]["eps_growth"]
    if revenue is None:
        return None

    if revenue < 0:
        sentence = f"Revenue fell {pct(-revenue)} year on year in the latest quarter"
    else:
        if revenue >= REVENUE_GROWTH_STRONG:
            pace = "strong"
        elif revenue >= REVENUE_GROWTH_MODERATE:
            pace = "moderate"
        else:
            pace = "slow"
        sentence = f"Revenue growth is {pace} at {pct(revenue)} year on year in the latest quarter"

    # Compare earnings growth with revenue growth: a gap of 5+ points is a signal.
    if eps is not None:
        if eps < 0:
            sentence += f", but earnings per share fell {pct(-eps)}"
        elif eps > revenue + 0.05:
            sentence += (
                f", with earnings per share growing faster ({pct(eps)}), "
                "a sign of widening margins"
            )
        elif eps < revenue - 0.05:
            sentence += (
                f", but earnings per share grew more slowly ({pct(eps)}), "
                "suggesting margin pressure"
            )
        else:
            sentence += f", with earnings per share growing broadly in line ({pct(eps)})"
    return sentence + "."


def summarise_leverage(report: dict) -> str | None:
    """One sentence on balance-sheet leverage and liquidity."""
    health = report["financial_health"]
    if health["debt_to_equity_note"]:
        return (
            "As a balance-sheet business, standard leverage measures such as "
            "debt/equity do not apply; regulatory capital ratios are the better "
            "gauge of its financial strength."
        )

    debt_to_equity, current = health["debt_to_equity"], health["current_ratio"]
    if debt_to_equity is None:
        return None

    if debt_to_equity < DEBT_EQUITY_LOW:
        level = "conservatively financed"
    elif debt_to_equity < DEBT_EQUITY_MODERATE:
        level = "moderately leveraged"
    else:
        level = "highly leveraged"
    sentence = f"The balance sheet is {level}, with debt at {debt_to_equity:.2f}x equity"
    if current is not None:
        if current >= 1:
            sentence += (
                f", and a current ratio of {current:.2f}x means short-term assets "
                "cover near-term obligations"
            )
        else:
            sentence += (
                f", though a current ratio of {current:.2f}x means near-term "
                "obligations exceed short-term assets"
            )
    return sentence + "."


def generate_summary(report: dict) -> str:
    """Turn a combined report into a short plain-English paragraph."""
    sentences = [
        summarise_valuation(report),
        summarise_profitability(report),
        summarise_growth(report),
        summarise_leverage(report),
    ]
    # Skip any topic with missing data rather than writing "N/A" into prose.
    return " ".join(s for s in sentences if s)


def generate_report(ticker: str) -> dict:
    """Fetch everything for a ticker and run every section's calculations.

    Returns one combined dictionary, so the console output, summary and PDF
    can all be built from the same data without fetching anything twice.
    """
    data = fetch_raw_data(ticker)
    stock_history = fetch_price_history(ticker)
    benchmark_history = fetch_price_history(BENCHMARK_TICKER)
    fx_history = fetch_fx_history(data.get("currency"))

    return {
        "ticker": ticker,
        "report_date": date.today(),
        "snapshot": get_snapshot(data),
        "valuation": get_valuation(data),
        "profitability": get_profitability(data),
        "growth": get_growth(data),
        "financial_health": get_financial_health(data),
        "price_context": get_price_context(
            data, stock_history, benchmark_history, fx_history
        ),
    }


def print_report(report: dict) -> None:
    """Print every section of a combined report to the console."""
    print_snapshot(report["snapshot"])
    print()
    print_valuation(report["valuation"])
    print()
    print_profitability(report["profitability"])
    print()
    print_growth(report["growth"])
    print()
    print_financial_health(report["financial_health"])
    print()
    print_price_context(report["price_context"], report["ticker"])
    print()
    print("=== Summary ===")
    print(textwrap.fill(generate_summary(report), width=88))


# --- PDF report ---------------------------------------------------------------

REPORT_DIR = Path(__file__).parent / "reports"
PAGE_MARGIN = 16 * mm
CONTENT_WIDTH = A4[0] - 2 * PAGE_MARGIN
COLUMN_GAP = 6 * mm

# Same inks as the chart, so the page and the chart feel like one design.
INK = colors.HexColor(TEXT_PRIMARY)
MUTED = colors.HexColor(TEXT_SECONDARY)
RULE = colors.HexColor(GRID_COLOR)
ACCENT = colors.HexColor(SERIES_STOCK)
TILE_BACKGROUND = colors.HexColor("#f4f4f1")


def pdf_style(name: str, size: float, leading: float | None = None,
              font: str = "Helvetica", color=INK, **kwargs) -> ParagraphStyle:
    """Build a paragraph style with the report's defaults."""
    return ParagraphStyle(
        name, fontName=font, fontSize=size, leading=leading or size * 1.25,
        textColor=color, **kwargs,
    )


PDF_STYLES = {
    "name": pdf_style("name", 20, 24, "Helvetica-Bold"),
    "meta": pdf_style("meta", 9, color=MUTED),
    "doc_label": pdf_style("doc_label", 8, font="Helvetica-Bold", color=ACCENT, alignment=TA_RIGHT),
    "doc_date": pdf_style("doc_date", 9, color=MUTED, alignment=TA_RIGHT),
    "tile_label": pdf_style("tile_label", 6.5, font="Helvetica-Bold", color=MUTED),
    "tile_value": pdf_style("tile_value", 14, 19, "Helvetica-Bold"),
    "tile_sub": pdf_style("tile_sub", 7.5, color=MUTED),
    "section": pdf_style("section", 7.5, font="Helvetica-Bold", color=MUTED, spaceAfter=5),
    "body": pdf_style("body", 9.5, 14.5),
    "note": pdf_style("note", 6.8, 9.2, color=MUTED),
}


def para(text: str, style: str) -> Paragraph:
    """A paragraph with its text escaped.

    reportlab reads paragraph text as mini-HTML, so a name like "S&P Global"
    would break it; escape() turns & into &amp; and so on.
    """
    return Paragraph(escape(text), PDF_STYLES[style])


def pdf_value(text: str, note: str | None = None) -> str:
    """Tidy a formatted value for print: 'N/A' -> 'n/a', and a dagger if noted."""
    text = "n/a" if text == "N/A" else text
    return f"{text} †" if note else text


def pdf_ratio(value: float | None, note: str | None = None, decimals: int = 1) -> str:
    return pdf_value(format_ratio(value, decimals=decimals), note)


def pdf_earnings_multiple(value: float | None) -> str:
    """Format an earnings-based multiple, showing 'n/m' when it's negative.

    A negative P/E or EV/EBITDA means earnings are negative, not that the
    stock is cheap, so the number itself is not meaningful.
    """
    if value is not None and value < 0:
        return "n/m"
    return pdf_ratio(value)


def has_negative_multiple(valuation: dict) -> bool:
    """True if any earnings-based multiple will be shown as 'n/m'."""
    keys = ("pe_trailing", "pe_forward", "ev_ebitda")
    return any(valuation[k] is not None and valuation[k] < 0 for k in keys)


def pdf_percent(value: float | None, signed: bool = False, note: str | None = None) -> str:
    return pdf_value(format_percent(value, signed=signed, decimals=1), note)


def pdf_header(report: dict) -> list:
    """Company name and descriptors on the left, document label on the right."""
    snapshot = report["snapshot"]
    descriptors = [report["ticker"], snapshot["exchange"], snapshot["sector"], snapshot["industry"]]
    meta = "  ·  ".join(d for d in descriptors if d)
    report_date = report["report_date"]

    table = Table(
        [[
            [para(snapshot["name"] or report["ticker"], "name"), para(meta, "meta")],
            [para("EQUITY SNAPSHOT", "doc_label"),
             para(f"{report_date.day} {report_date:%B %Y}", "doc_date")],
        ]],
        colWidths=[CONTENT_WIDTH * 0.72, CONTENT_WIDTH * 0.28],
    )
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    rule = HRFlowable(width="100%", thickness=1.2, color=ACCENT, spaceBefore=8, spaceAfter=10)
    return [table, rule]


def pdf_tiles(report: dict) -> Table:
    """Four headline figures across the top of the page."""
    snapshot, context = report["snapshot"], report["price_context"]
    currency = snapshot["currency"] or ""

    from_high = context["from_high"]
    price_sub = f"{pdf_percent(from_high, signed=True)} from 52-week high" if from_high is not None else ""

    market_cap = format_large_number(snapshot["market_cap"])
    market_cap_text = "n/a" if market_cap == "N/A" else f"{market_cap} {currency}".strip()

    low, high = context["low_52w"], context["high_52w"]
    range_text = f"{low:,.2f} – {high:,.2f}" if low and high else "n/a"

    if context["start_date"]:
        return_label = f"Total return since {context['start_date']:%b %Y}"
        return_sub = f"S&P 500: {pdf_percent(context['benchmark_return'], signed=True)}"
        if context["converted_to_usd"]:
            return_sub += "  ·  in USD"
    else:
        return_label, return_sub = "Total return", ""

    tiles = [
        ("Share price", pdf_value(format_price(snapshot["price"], currency)), price_sub),
        ("Market cap", market_cap_text, snapshot["exchange"] or ""),
        ("52-week range", range_text, currency),
        (return_label, pdf_percent(context["stock_return"], signed=True), return_sub),
    ]

    # Tiles alternate with narrow empty "gap" columns to space them apart.
    gap = 3 * mm
    tile_width = (CONTENT_WIDTH - 3 * gap) / 4
    cells, widths = [], []
    for i, (label, value, sub) in enumerate(tiles):
        if i:
            cells.append("")
            widths.append(gap)
        cells.append([para(label.upper(), "tile_label"), para(value, "tile_value"), para(sub, "tile_sub")])
        widths.append(tile_width)

    table = Table([cells], colWidths=widths)
    commands = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]
    for col in range(0, len(cells), 2):
        commands.append(("BACKGROUND", (col, 0), (col, 0), TILE_BACKGROUND))
    table.setStyle(TableStyle(commands))
    return table


def pdf_metric_block(sections: list, width: float) -> Table:
    """A column of labelled values, grouped under small-caps headings."""
    rows = []
    commands = [
        ("FONT", (0, 0), (-1, -1), "Helvetica", 8.5),
        ("TEXTCOLOR", (0, 0), (-1, -1), INK),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),  # right-align so decimals line up
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]
    for title, items in sections:
        row = len(rows)
        rows.append([title.upper(), ""])
        commands += [
            ("SPAN", (0, row), (1, row)),
            ("FONT", (0, row), (1, row), "Helvetica-Bold", 7),
            ("TEXTCOLOR", (0, row), (1, row), MUTED),
            ("LINEBELOW", (0, row), (1, row), 0.8, INK),
        ]
        if row:  # extra space above a second heading in the same column
            commands.append(("TOPPADDING", (0, row), (1, row), 12))
        for label, value in items:
            rows.append([label, value])
            commands.append(("LINEBELOW", (0, len(rows) - 1), (1, len(rows) - 1), 0.25, RULE))

    table = Table(rows, colWidths=[width * 0.6, width * 0.4])
    table.setStyle(TableStyle(commands))
    return table


def pdf_metrics(report: dict) -> Table:
    """All the metrics, grouped by category in four columns."""
    valuation = report["valuation"]
    profitability = report["profitability"]
    growth = report["growth"]
    health = report["financial_health"]
    context = report["price_context"]
    ticker = report["ticker"]

    columns = [
        [("Valuation", [
            ("P/E (trailing)", pdf_earnings_multiple(valuation["pe_trailing"])),
            ("P/E (forward)", pdf_earnings_multiple(valuation["pe_forward"])),
            ("EV/EBITDA", pdf_earnings_multiple(valuation["ev_ebitda"])),
            ("Price/book", pdf_ratio(valuation["price_to_book"])),
            ("Price/sales", pdf_ratio(valuation["price_to_sales"])),
        ])],
        [("Profitability", [
            ("Gross margin", pdf_percent(profitability["gross_margin"], note=profitability["gross_margin_note"])),
            ("Operating margin", pdf_percent(profitability["operating_margin"])),
            ("Net margin", pdf_percent(profitability["net_margin"])),
            ("Return on equity", pdf_percent(profitability["roe"])),
            ("Return on assets", pdf_percent(profitability["roa"])),
        ])],
        [
            ("Growth (latest quarter)", [
                ("Revenue", pdf_percent(growth["revenue_growth"], signed=True)),
                ("EPS", pdf_percent(growth["eps_growth"], signed=True)),
            ]),
            ("Financial health", [
                # Two decimals: balance-sheet ratios sit near 1, where 0.29
                # vs 0.34 matters and would both round to 0.3.
                ("Debt/equity", pdf_ratio(health["debt_to_equity"], health["debt_to_equity_note"], 2)),
                ("Current ratio", pdf_ratio(health["current_ratio"], health["current_ratio_note"], 2)),
            ]),
        ],
    ]

    performance = []
    if context["indexed"] is not None:
        if context["converted_to_usd"]:
            performance.append((f"{ticker} ({context['currency']})",
                                pdf_percent(context["stock_return_local"], signed=True)))
            performance.append((f"{ticker} (USD)", pdf_percent(context["stock_return"], signed=True)))
        else:
            performance.append((ticker, pdf_percent(context["stock_return"], signed=True)))
        performance += [
            ("S&P 500 TR", pdf_percent(context["benchmark_return"], signed=True)),
            ("Difference", f"{context['relative'] * 100:+.1f} pts"),
        ]
    else:
        performance.append(("Price history", "n/a"))
    columns.append([("Total return", performance)])

    # Four blocks separated by empty gap columns.
    block_width = (CONTENT_WIDTH - 3 * COLUMN_GAP) / 4
    cells, widths = [], []
    for i, sections in enumerate(columns):
        if i:
            cells.append("")
            widths.append(COLUMN_GAP)
        cells.append(pdf_metric_block(sections, block_width))
        widths.append(block_width)

    table = Table([cells], colWidths=widths)
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    return table


def pdf_notes(report: dict) -> Paragraph:
    """Small-print definitions, so every figure on the page can be interpreted."""
    context = report["price_context"]
    notes = [
        "Growth compares the latest quarter with the same quarter a year earlier.",
    ]
    if context["start_date"]:
        total_return = (
            f"Total return runs {context['start_date']:%d %b %Y} to "
            f"{context['end_date']:%d %b %Y} with dividends reinvested, against the "
            "S&P 500 Total Return index; the difference is in percentage points"
        )
        if context["converted_to_usd"]:
            total_return += (
                f". Returns are compared in USD, converted at "
                f"{context['currency'].upper()}=X exchange rates"
            )
        notes.append(total_return + ".")
    notes.append("n/a: not available.")
    if has_negative_multiple(report["valuation"]):
        notes.append("n/m: not meaningful, because earnings are negative.")

    note = report["profitability"]["gross_margin_note"]
    if note:
        notes.append(
            f"† {note[0].upper()}{note[1:]}: gross margin, debt/equity and current "
            "ratio do not meaningfully describe a business whose balance sheet is its product."
        )
    return para(" ".join(notes), "note")


def pdf_footer(canvas, doc) -> None:
    """Draw the source line and disclaimer at the bottom of the page."""
    canvas.saveState()
    y = 11 * mm
    canvas.setStrokeColor(RULE)
    canvas.setLineWidth(0.5)
    canvas.line(PAGE_MARGIN, y + 4 * mm, A4[0] - PAGE_MARGIN, y + 4 * mm)
    canvas.setFont("Helvetica", 7)
    canvas.setFillColor(MUTED)
    canvas.drawString(PAGE_MARGIN, y, "Equity Analyser  ·  Data: Yahoo Finance via yfinance")
    canvas.drawRightString(A4[0] - PAGE_MARGIN, y, "For information only. Not investment advice.")
    canvas.restoreState()


def build_pdf_report(report: dict, summary: str, chart_path: Path | None) -> Path:
    """Lay out the one-page PDF report and save it to reports/."""
    story = pdf_header(report)
    story += [pdf_tiles(report), Spacer(1, 12)]

    if chart_path:
        width, height = ImageReader(str(chart_path)).getSize()
        story.append(Image(str(chart_path), width=CONTENT_WIDTH, height=CONTENT_WIDTH * height / width))
    else:
        story.append(para("Price history unavailable.", "note"))

    story += [
        Spacer(1, 12),
        para("KEY METRICS", "section"),
        pdf_metrics(report),
        Spacer(1, 14),
        para("SUMMARY", "section"),
    ]

    # Summary with a coloured bar down its left edge.
    summary_box = Table([[para(summary, "body")]], colWidths=[CONTENT_WIDTH])
    summary_box.setStyle(TableStyle([
        ("LINEBEFORE", (0, 0), (0, 0), 2.5, ACCENT),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 1),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    story += [summary_box, Spacer(1, 10), pdf_notes(report)]

    REPORT_DIR.mkdir(exist_ok=True)
    path = REPORT_DIR / f"{report['ticker']}_report.pdf"
    name = report["snapshot"]["name"] or report["ticker"]
    doc = SimpleDocTemplate(
        str(path), pagesize=A4,
        leftMargin=PAGE_MARGIN, rightMargin=PAGE_MARGIN,
        topMargin=14 * mm, bottomMargin=20 * mm,
        title=f"{name} - Equity Snapshot", author="Equity Analyser",
    )
    doc.build(story, onFirstPage=pdf_footer, onLaterPages=pdf_footer)
    if doc.page > 1:
        print(f"Warning: report ran to {doc.page} pages instead of one.")
    return path


def main() -> None:
    # Usage: python main.py TICKER [--raw]
    args = sys.argv[1:]
    show_raw = "--raw" in args
    tickers = [a for a in args if not a.startswith("--")]
    ticker = tickers[0].upper() if tickers else "AAPL"

    print(f"Fetching data for {ticker}...\n")
    if show_raw:
        pprint(fetch_raw_data(ticker))
        return

    report = generate_report(ticker)
    print_report(report)

    chart_path = plot_price_context(report["price_context"], ticker)
    if chart_path:
        print(f"\nChart saved to {chart_path}")

    print_chart = plot_price_context(report["price_context"], ticker, compact=True)
    pdf_path = build_pdf_report(report, generate_summary(report), print_chart)
    print(f"PDF report saved to {pdf_path}")


if __name__ == "__main__":
    main()
