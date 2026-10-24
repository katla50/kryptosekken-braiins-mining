#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Konverterer en payout-eksport fra Braiins Pool til Kryptosekken sitt
generiske importformat.

Bakgrunn
--------
Braiins Pool (braiins.com/pool) kan eksportere utbetalinger som CSV med
kolonnene ``account, amount, user_fee, coin, target, date, transaction_id``.
Hver rad er en mining-utbetaling: poolen sender ``amount`` av ``coin`` til
``target`` (en Lightning-adresse, en on-chain-adresse eller en konto i en
børs). Det er *inntekt av mining*, ikke et salg, og skal derfor føres som
``Mining`` i Kryptosekken - ikke som ``Handel``.

Formatet som skrives er Kryptosekken sin generiske CSV-import
(https://www.kryptosekken.no/regnskap/importer-csv-generisk) med kolonnene:

    Tidspunkt, Type, Inn, Inn-Valuta, Ut, Ut-Valuta, Gebyr, Gebyr-Valuta,
    Marked, Notat

To måter å føre utbetalingene på
--------------------------------
1. Standard: inntekten føres direkte på mottakerlommeboken (``Marked`` =
   kortform av target, f.eks. ``Alby`` - full adresse i ``Notat``). Riktig når Lightning-lommeboken ikke
   følges separat i Kryptosekken - da slipper du overføringsrader og får
   aldri negativ saldo noe sted.
2. ``--at-pool --transfer``: inntekten føres først på selve poolen
   (``Marked`` = ``Braiins-Pool``), og deretter føres en overføring fra poolen
   til mottakeren (``Overføring-Ut`` + ``Overføring-Inn``). Bruk denne hvis
   du følger mottakerlommeboken i Kryptosekken og senere skal selge fra den.

Verdsettelse
------------
``Inn`` er kryptomengden, ikke et NOK-beløp. Kryptosekken priser selv
BTC-innbetalinger til markedsverdi på tidspunktet, som er nettopp det
skattemessig inntekt skal verdsettes til. Med ``--estimate-nok`` hentes i
tillegg en veiledende BTC/NOK-kurs fra CoinGecko, kun til oppsummeringen -
den skrives ikke i filen.

Bruk
----
    python braiins_mining_kryptosekken.py \\
        --input btc_payouts.csv \\
        --output kryptosekken_mining_import.csv

Se README.md for fullstendig dokumentasjon.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

try:
    import pandas as pd
except ImportError:  # pragma: no cover - avhenger av miljøet
    sys.stderr.write(
        "FEIL: pandas er ikke installert.\n"
        "Installer avhengighetene med:  pip install -r requirements.txt\n"
    )
    raise SystemExit(1)

try:
    import requests
except ImportError:  # pragma: no cover - avhenger av miljøet
    sys.stderr.write(
        "FEIL: requests er ikke installert.\n"
        "Installer avhengighetene med:  pip install -r requirements.txt\n"
    )
    raise SystemExit(1)


# ---------------------------------------------------------------------------
# Konstanter
# ---------------------------------------------------------------------------

# Kolonnene Kryptosekken forventer i den generiske CSV-importen.
KRYPTOSEKKEN_COLUMNS = [
    "Tidspunkt",
    "Type",
    "Inn",
    "Inn-Valuta",
    "Ut",
    "Ut-Valuta",
    "Gebyr",
    "Gebyr-Valuta",
    "Marked",
    "Notat",
]

# Kilden (Braiins Pool-eksport) -> internt feltnavn. Nøklene sammenlignes
# i små bokstaver og uten mellomrom/understreker, slik at både
# ``user_fee`` og ``User Fee`` blir gjenkjent.
INPUT_ALIASES = {
    "account": "account",
    "amount": "amount",
    "userfee": "user_fee",
    "fee": "user_fee",
    "coin": "coin",
    "currency": "coin",
    "target": "target",
    "address": "target",
    "date": "date",
    "datetime": "date",
    "timestamp": "date",
    "transactionid": "transaction_id",
    "txid": "transaction_id",
    "tx": "transaction_id",
    "id": "transaction_id",
}

REQUIRED_FIELDS = ["amount", "coin", "date"]

KRYPTOSEKKEN_TYPE = "Mining"
TRANSFER_OUT = "Overføring-Ut"
TRANSFER_IN = "Overføring-Inn"
EXPENSE_TYPE = "Forvaltningskostnad"

DEFAULT_POOL_NAME = "Braiins-Pool"
DEFAULT_POOL_DISPLAY = "Braiins Pool"

# CoinGecko-id per myntsymbol, brukes bare til --estimate-nok.
COINGECKO_IDS = {
    "BTC": "bitcoin",
    "BCH": "bitcoin-cash",
    "ETH": "ethereum",
    "LTC": "litecoin",
    "BSV": "bitcoin-cash-sv",
    "DASH": "dash",
    "ZEC": "zcash",
}
COINGECKO_HISTORY_URL = "https://api.coingecko.com/api/v3/coins/{coin_id}/history"
# Binance daglige klines (åpent API) og Norges Banks USD/NOK (SDMX-CSV).
BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
NORGES_BANK_URL = "https://data.norges-bank.no/api/data/EXR/B.USD.NOK.SP"

# Hvor mange dager bakover vi leter etter en kurs på en helg/helligdag.
MAX_RATE_LOOKBACK_DAYS = 14

LOGGER = logging.getLogger("braiins-mining-konverterer")


# ---------------------------------------------------------------------------
# Hjelpefunksjoner
# ---------------------------------------------------------------------------


def setup_logging(verbose: bool = False) -> None:
    """Setter opp logging til konsoll (INFO, eller DEBUG med --verbose)."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)-8s %(message)s",
        stream=sys.stdout,
    )


def to_decimal(value) -> Optional[Decimal]:
    """
    Gjør om en verdi til Decimal på en trygg måte.

    Vi går via tekst for å unngå flyttallfeil. Returnerer None hvis verdien
    ikke kan tolkes som et tall.
    """
    if value is None:
        return None
    text = str(value).strip().replace(" ", "")
    if text == "" or text.lower() in {"nan", "none", "-"}:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def format_amount(amount: Decimal, decimals: Optional[int] = None) -> str:
    """
    Formaterer et Decimal uten vitenskapelig notasjon.

    ``decimals`` tvinger et fast antall desimaler (brukes for NOK).
    Uten ``decimals`` fjernes overflødige nuller, slik at kryptomengden
    står så nær kilden som mulig.
    """
    if decimals is not None:
        return f"{amount:.{decimals}f}"
    return format(amount.normalize(), "f")


MARKET_PATTERN = re.compile(r"^[A-Za-z0-9-]{1,16}$")

# Notat er også validert: vi holder oss til bokstaver, tall, mellomrom og
# enkel tegnsetting. @ og | er observert avvist i praksis.
NOTE_ALLOWED = re.compile(r"[^0-9A-Za-zÆØÅæøåÄÖäöÜüÉéÈè \-_.,:;()/]")
NOTE_MAX_LENGTH = 200

# Gjenkjente Lightning-/lommeboktilbydere, sjekkes som delstreng i target.
PROVIDER_NAMES = (
    ("getalby.com", "Alby"),
    ("strike.me", "Strike"),
    ("primal.net", "Primal"),
    ("walletofsatoshi.com", "WalletOfSatoshis"),
    ("blink.sv", "Blink"),
    ("coinos.io", "Coinos"),
    ("lnbits", "LNbits"),
    ("ln.tips", "LNTips"),
)


def sanitize_market(value: str) -> str:
    """
    Gjør et navn om til en gyldig ``Marked``-verdi.

    Kryptosekken godtar bare 1-16 tegn av ``A-Z``, ``a-z``, ``0-9`` og
    bindestrek i dette feltet - mellomrom og parenteser gir
    "Innholdet i feltet Marked er ikke gyldig" ved import.
    """
    text = re.sub(r"[^A-Za-z0-9-]+", "-", (value or "").strip())
    text = re.sub(r"-{2,}", "-", text).strip("-")
    return text[:16].rstrip("-") or "Ukjent"


def wallet_label(target: str) -> str:
    """
    Lager et kort, gyldig lommeboknavn fra en target-adresse.

    ``testuser@strike.me`` -> ``Strike``, ``bc1q...`` -> de første tegnene av
    adressen. Full adresse beholdes i ``Notat``.
    """
    text = (target or "").strip()
    if not text:
        return "Ukjent"
    lower = text.lower()
    for needle, name in PROVIDER_NAMES:
        if needle in lower:
            return sanitize_market(name)
    if "@" in text:
        return sanitize_market(text.split("@")[0])
    return sanitize_market(text[:12])


def unique_label(label: str, used: Dict[str, str], target: str) -> str:
    """
    Sørger for at to ulike targets ikke får samme ``Marked``-navn.

    Første target beholder navnet; senere kollisjoner får ``-2``, ``-3``, …
    """
    if label not in used or used[label] == target:
        used[label] = target
        return label
    for suffix in range(2, 100):
        candidate = sanitize_market(f"{label[:13]}-{suffix}")
        if candidate not in used or used[candidate] == target:
            used[candidate] = target
            return candidate
    raise ValueError(f"Kunne ikke lage et unikt Marked-navn for {target}")


def sanitize_note(text: str, max_length: int = NOTE_MAX_LENGTH) -> str:
    """
    Gjør et notat om til en trygg ``Notat``-verdi.

    Kryptosekken avviser notatet med "Innholdet i feltet Notat er ikke
    gyldig" når det inneholder tegn utenfor det feltet godtar. Vi beholder
    bokstaver (også æøå), tall, mellomrom og ``- _ . , : ; ( ) /`` og
    fjerner alt annet - blant annet ``@`` og ``|``.
    """
    cleaned = NOTE_ALLOWED.sub("", text or "")
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    if len(cleaned) > max_length:
        cleaned = cleaned[: max_length - 3].rstrip() + "..."
    return cleaned


# ---------------------------------------------------------------------------
# Innlesing av kilden
# ---------------------------------------------------------------------------


def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Mapper kildens kolonnenavn til interne feltnavn."""
    rename: Dict[str, str] = {}
    for column in df.columns:
        key = str(column).strip().lower().replace(" ", "").replace("_", "").replace("-", "")
        if key in INPUT_ALIASES:
            rename[column] = INPUT_ALIASES[key]
    return df.rename(columns=rename)


def read_payouts(input_path: Path) -> pd.DataFrame:
    """
    Leser Braiins-eksporten og returnerer unike payout-rader.

    Feilhåndtering:
      * filen finnes ikke  -> FileNotFoundError med tydelig melding
      * kolonner mangler   -> ValueError som lister opp hva som mangler
      * ingen rader        -> ValueError

    Duplikater fjernes på ``transaction_id``. En eksport som er limt sammen
    to ganger (eller hentet på nytt med overlappende periode) ville ellers
    dobbeltført inntekten.
    """
    if not input_path.exists():
        raise FileNotFoundError(f"Inndatafilen finnes ikke: {input_path}")

    df = pd.read_csv(input_path, dtype=str, keep_default_na=False)
    df = normalize_columns(df)

    missing = [f for f in REQUIRED_FIELDS if f not in df.columns]
    if missing:
        raise ValueError(
            "Inndatafilen mangler disse kolonnene: "
            + ", ".join(missing)
            + f"\nFant: {', '.join(str(c) for c in df.columns)}"
        )

    if "transaction_id" not in df.columns:
        LOGGER.warning(
            "Fant ingen transaction_id-kolonne - duplikater kan ikke oppdages."
        )

    # Fjern tomme rader (eksporten kan ha en avsluttende tom linje).
    df = df[df["amount"].astype(str).str.strip() != ""].copy()

    if "target" not in df.columns:
        df["target"] = ""
    df["target"] = df["target"].astype(str).str.strip()
    df["coin"] = df["coin"].astype(str).str.strip().str.upper()

    if "transaction_id" in df.columns:
        before = len(df)
        df = df.drop_duplicates(subset=["transaction_id"], keep="first")
        removed = before - len(df)
        if removed:
            LOGGER.warning(
                "Fjernet %d dupliserte payout-rader (samme transaction_id). "
                "Uten dette ville inntekten blitt dobbeltført.",
                removed,
            )

    if df.empty:
        raise ValueError(f"Fant ingen payout-rader i {input_path}")

    LOGGER.info("Leste %d unike utbetalinger fra %s", len(df), input_path)
    return df


# ---------------------------------------------------------------------------
# Tidspunkt
# ---------------------------------------------------------------------------


def parse_timestamp(raw: str, source_tz: str, target_tz: str, tx_id: str) -> datetime:
    """
    Tolker kildens tidspunkt og konverterer det til norsk tid.

    Braiins oppgir tidspunktet i UTC uten tidssone-suffiks
    (``2026-01-10 09:06:07``). Er det allerede et suffiks (``Z`` eller
    ``+02:00``), respekteres det og ``source_tz`` ignoreres.
    """
    text = str(raw).strip()
    if not text:
        raise ValueError(f"Mangler dato for transaction_id {tx_id}")

    cleaned = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        raise ValueError(
            f"Kunne ikke tolke datoen '{text}' (transaction_id {tx_id}). "
            "Forventet format er YYYY-MM-DD HH:MM:SS."
        ) from None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(source_tz))
    return parsed.astimezone(ZoneInfo(target_tz))


# ---------------------------------------------------------------------------
# Veiledende NOK-verdi (kun til oppsummeringen)
# ---------------------------------------------------------------------------


def fetch_btc_usd_daily(
    start: date, end: date, timeout: int = 25
) -> Dict[date, Decimal]:
    """
    Henter daglige BTC/USD-sluttkurser fra Binance (åpent API, ingen nøkkel).

    Returnerer {dato: kurs}, der datoen er UTC-dagen kline-en gjelder for.
    """
    start_ms = int(
        datetime.combine(start, time.min, tzinfo=timezone.utc).timestamp() * 1000
    )
    end_ms = int(
        datetime.combine(end + timedelta(days=1), time.min, tzinfo=timezone.utc).timestamp()
        * 1000
    )
    params = {
        "symbol": "BTCUSDT",
        "interval": "1d",
        "startTime": start_ms,
        "endTime": end_ms,
        "limit": 1000,
    }
    response = requests.get(BINANCE_KLINES_URL, params=params, timeout=timeout)
    response.raise_for_status()

    prices: Dict[date, Decimal] = {}
    for kline in response.json():
        close = to_decimal(kline[4])
        if close is None:
            continue
        day = datetime.fromtimestamp(kline[0] / 1000, tz=timezone.utc).date()
        prices[day] = close
    if not prices:
        raise ValueError("Binance returnerte ingen BTC/USD-kurser for perioden")
    return prices


def fetch_usd_nok_daily(
    start: date, end: date, timeout: int = 30
) -> Dict[date, Decimal]:
    """
    Henter USD/NOK fra Norges Banks åpne API (semikolon-separert SDMX-CSV).

    Feltindekser: 2 = BASE_CUR, 4 = QUOTE_CUR, 14 = TIME_PERIOD,
    15 = OBS_VALUE.
    """
    params = {
        "format": "csv",
        "startPeriod": start.isoformat(),
        "endPeriod": end.isoformat(),
    }
    response = requests.get(NORGES_BANK_URL, params=params, timeout=timeout)
    response.raise_for_status()

    rates: Dict[date, Decimal] = {}
    for line in response.text.splitlines()[1:]:  # hopp over header
        fields = line.split(";")
        if len(fields) < 16:
            continue
        if fields[2].upper() != "USD" or fields[4].upper() != "NOK":
            continue
        value = to_decimal(fields[15].replace(",", "."))
        if value is None:
            continue
        try:
            rates[date.fromisoformat(fields[14])] = value
        except ValueError:
            continue
    if not rates:
        raise ValueError("Norges Bank returnerte ingen USD/NOK-kurser for perioden")
    return rates


def build_business_day_lookup(rates: Dict[date, Decimal]):
    """
    Oppslag som faller tilbake på nærmeste tidligere virkedag.

    Norges Bank publiserer bare på virkedager; en lørdag skal bruke
    fredagens kurs. Returnerer (kurs, faktisk_dato) eller (None, None).
    """
    def lookup(wanted: date):
        if wanted in rates:
            return rates[wanted], wanted
        for offset in range(1, MAX_RATE_LOOKBACK_DAYS + 1):
            candidate = wanted - timedelta(days=offset)
            if candidate in rates:
                return rates[candidate], candidate
        return None, None

    return lookup


def fetch_coingecko_nok(
    coin: str, day: date, timeout: int = 20, attempts: int = 3
) -> Optional[Decimal]:
    """
    Henter ``coin``/NOK for en dato fra CoinGecko.

    Brukes for andre mynter enn BTC. Det åpne endepunktet har streng
    rate-limit, så vi venter mellom forsøkene og gir opp stille.
    """
    coin_id = COINGECKO_IDS.get(coin.upper())
    if coin_id is None:
        LOGGER.warning(
            "Kjenner ikke CoinGecko-id for %s - hopper over NOK-estimat.", coin
        )
        return None

    params = {"date": day.strftime("%d-%m-%Y"), "localization": "false"}
    url = COINGECKO_HISTORY_URL.format(coin_id=coin_id)
    for attempt in range(1, attempts + 1):
        try:
            response = requests.get(url, params=params, timeout=timeout)
            response.raise_for_status()
            price = (
                response.json().get("market_data", {}).get("current_price", {}).get("nok")
            )
            if price is None:
                raise ValueError("mangler NOK-kurs i svaret")
            return Decimal(str(price))
        except (requests.RequestException, ValueError) as error:
            if attempt == attempts:
                LOGGER.warning(
                    "Fikk ingen %s/NOK-kurs for %s fra CoinGecko (%s).",
                    coin,
                    day,
                    error,
                )
                return None
            time.sleep(6 * attempt)
    return None


def estimate_income_nok(details: List[dict]) -> Optional[dict]:
    """
    Regner ut en veiledende NOK-verdi av inntekten ved mottak.

    BTC prises som Binance daglig BTC/USD-sluttkurs ganget med Norges Banks
    USD/NOK for samme dag (binært: to HTTP-kall for hele perioden). Andre
    mynter hentes fra CoinGecko.

    Returnerer {"total_nok": Decimal, "missing": int, "source": str} eller
    None hvis ingen kurser kunne hentes. Feiler aldri hardt - dette er bare
    en kontrollsum, og importfilen er komplett uten den.
    """
    if not details:
        return None

    days = sorted({d["local_dt"].date() for d in details})
    start = days[0] - timedelta(days=MAX_RATE_LOOKBACK_DAYS)
    end = days[-1]

    prices: Dict[Tuple[str, date], Decimal] = {}
    sources = set()

    coins = {d["coin"] for d in details}
    if "BTC" in coins:
        try:
            btc_usd = fetch_btc_usd_daily(start, end)
            usd_nok = build_business_day_lookup(fetch_usd_nok_daily(start, end))
            for day in days:
                if day not in btc_usd:
                    continue
                fx, _ = usd_nok(day)
                if fx is None:
                    continue
                prices[("BTC", day)] = btc_usd[day] * fx
            sources.add("Binance BTC/USD x Norges Bank USD/NOK")
            LOGGER.info("Hentet BTC/NOK fra Binance og Norges Bank")
        except (requests.RequestException, ValueError) as error:
            LOGGER.warning("Klarte ikke å hente BTC/NOK-kurser: %s", error)

    for coin in sorted(coins - {"BTC"}):
        got = False
        for day in days:
            price = fetch_coingecko_nok(coin, day)
            if price is not None:
                prices[(coin, day)] = price
                got = True
        if got:
            sources.add("CoinGecko")

    if not prices:
        return None

    total = Decimal("0")
    missing = 0
    for detail in details:
        price = prices.get((detail["coin"], detail["local_dt"].date()))
        if price is None:
            missing += 1
            continue
        total += detail["amount"] * price

    return {
        "total_nok": total,
        "missing": missing,
        "source": " + ".join(sorted(sources)) or "ukjent",
    }


# ---------------------------------------------------------------------------
# Bygging av Kryptosekken-rader
# ---------------------------------------------------------------------------


def _fmt_amount(value) -> str:
    """
    Formaterer et beløp for importfilen.

    En streng brukes ordrett, slik at mengden kan stå med nøyaktig samme
    antall desimaler som i kildefilen (Braiins skriver f.eks. 0.00000790).
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return format_amount(value)


def build_row(
    timestamp: str,
    tx_type: str,
    amount_in=None,
    currency_in: Optional[str] = None,
    amount_out=None,
    currency_out: Optional[str] = None,
    marked: str = "",
    note: str = "",
) -> dict:
    """Lager én rad i Kryptosekken-format."""
    return {
        "Tidspunkt": timestamp,
        "Type": tx_type,
        "Inn": _fmt_amount(amount_in),
        "Inn-Valuta": currency_in or "",
        "Ut": _fmt_amount(amount_out),
        "Ut-Valuta": currency_out or "",
        "Gebyr": "",
        "Gebyr-Valuta": "",
        "Marked": marked,
        "Notat": note,
    }


def build_kryptosekken_rows(
    payouts: pd.DataFrame,
    timezone: str = "Europe/Oslo",
    source_tz: str = "UTC",
    pool_name: str = DEFAULT_POOL_NAME,
    pool_display: Optional[str] = None,
    at_pool: bool = False,
    transfer: bool = False,
    gross_fee: bool = False,
    note_style: str = "safe",
    note_max_length: int = NOTE_MAX_LENGTH,
) -> Tuple[pd.DataFrame, List[dict]]:
    """
    Bygger Kryptosekken-rader fra payout-radene.

    Standard (``at_pool=False``): én ``Mining``-rad per utbetaling, ført på
    mottakerlommeboken (``Marked`` = target).

    ``at_pool=True``: inntekten føres på poolen (``Marked`` = saniteret
    ``pool_name``). Med ``transfer=True``
    legges det i tillegg på en ``Overføring-Ut`` (fra poolen) og en
    ``Overføring-Inn`` (til mottakeren) per utbetaling.

    ``gross_fee=True``: bokfører bruttobeløpet (``amount + user_fee``) som
    inntekt og legger til en ``Forvaltningskostnad``-rad for gebyret, slik at
    poolgebyret blir fradragsberettiget. Standard er å føre nettobeløpet,
    som er det som faktisk ble mottatt.

    Returnerer (importklar dataframe, liste med detaljer for oppsummeringen).
    """
    rows: List[dict] = []
    details: List[dict] = []

    # Marked godtar bare 1-16 tegn av A-Z, a-z, 0-9 og bindestrek.
    pool_display = pool_display or pool_name
    pool_label = sanitize_market(pool_name)
    if pool_label != pool_name:
        LOGGER.info(
            "Marked-navnet '%s' er ikke gyldig i Kryptosekken - bruker '%s' i "
            "Marked-kolonnen (fullt navn står i Notat).",
            pool_name,
            pool_label,
        )
    wallet_labels: Dict[str, str] = {}

    for _, row in payouts.iterrows():
        tx_id = str(row.get("transaction_id", "")).strip()
        coin = str(row["coin"]).strip().upper()
        target_raw = str(row.get("target", "")).strip()
        wallet_name = unique_label(wallet_label(target_raw), wallet_labels, target_raw)
        account = str(row.get("account", "")).strip()

        amount_net = to_decimal(row["amount"])
        if amount_net is None or amount_net <= 0:
            raise ValueError(
                f"Ugyldig amount for transaction_id {tx_id}: '{row['amount']}'"
            )

        fee = to_decimal(row.get("user_fee", "")) or Decimal("0")
        if fee < 0:
            raise ValueError(f"Negativt user_fee for transaction_id {tx_id}: {fee}")

        amount_mined = amount_net + fee if gross_fee else amount_net
        # Behold kildens desimaler når beløpet ikke er regnet om.
        amount_mined_value = (
            format_amount(amount_mined) if amount_mined != amount_net else str(row["amount"]).strip()
        )

        local_dt = parse_timestamp(str(row["date"]), source_tz, timezone, tx_id)
        timestamp = local_dt.strftime("%Y-%m-%d %H:%M:%S")

        if note_style == "full":
            note_parts = [f"Mining payout fra {pool_display}"]
            if target_raw:
                note_parts.append(f"til {target_raw}")
            if account:
                note_parts.append(f"(worker {account})")
            note_parts.append(f"| tx {tx_id}" if tx_id else "| uten tx-id")
            if fee > 0 and not gross_fee:
                note_parts.append(
                    f"| brutto {format_amount(amount_mined)} {coin}, "
                    f"gebyr {format_amount(fee)} {coin}"
                )
            note = " ".join(note_parts)
        elif note_style == "minimal":
            # Minst mulig: bare sporbar id, ingen tegnsetting å snuble i.
            note = sanitize_note(f"tx {tx_id}" if tx_id else "", note_max_length)
        else:
            # Trygg variant: ingen @ eller |, og lommeboken nevnes med
            # kortnavnet som allerede står i Marked.
            note = f"Mining payout fra {pool_display} til {wallet_name}"
            if account:
                note += f" (worker {account})"
            note += f", tx {tx_id}" if tx_id else ", uten tx-id"
            if fee > 0 and not gross_fee:
                note += (
                    f", brutto {format_amount(amount_mined)} {coin}"
                    f", gebyr {format_amount(fee)} {coin}"
                )
            note = sanitize_note(note, note_max_length)

        # 1) Selve inntekten.
        rows.append(
            build_row(
                timestamp=timestamp,
                tx_type=KRYPTOSEKKEN_TYPE,
                amount_in=amount_mined_value,
                currency_in=coin,
                amount_out=None,
                currency_out=None,
                marked=pool_label if at_pool else wallet_name,
                note=note,
            )
        )

        # 2) Eventuelt poolgebyr som egen kostnad.
        if gross_fee and fee > 0:
            rows.append(
                build_row(
                    timestamp=timestamp,
                    tx_type=EXPENSE_TYPE,
                    amount_in=None,
                    currency_in=None,
                    amount_out=fee,
                    currency_out=coin,
                    marked=pool_label,
                    note=(
                        sanitize_note(f"Poolgebyr {pool_display} | tx {tx_id}", note_max_length)
                        if tx_id
                        else "Poolgebyr"
                    )
                    if note_style == "safe"
                    else (f"Poolgebyr {pool_display} | tx {tx_id}" if tx_id else "Poolgebyr"),
                )
            )

        # 3) Eventuell overføring fra poolen til mottakeren.
        if at_pool and transfer:
            rows.append(
                build_row(
                    timestamp=timestamp,
                    tx_type=TRANSFER_OUT,
                    amount_in=None,
                    currency_in=None,
                    amount_out=amount_mined_value,
                    currency_out=coin,
                    marked=pool_label,
                    note=sanitize_note(
                        f"Sendt til {target_raw or 'ukjent mottaker'} | tx {tx_id}",
                        note_max_length,
                    )
                    if note_style == "safe"
                    else f"Sendt til {target_raw or 'ukjent mottaker'} | tx {tx_id}",
                )
            )
            rows.append(
                build_row(
                    timestamp=timestamp,
                    tx_type=TRANSFER_IN,
                    amount_in=amount_mined_value,
                    currency_in=coin,
                    amount_out=None,
                    currency_out=None,
                    marked=wallet_name,
                    note=sanitize_note(
                        f"Mottatt fra {pool_display} | tx {tx_id}", note_max_length
                    )
                    if note_style == "safe"
                    else f"Mottatt fra {pool_display} | tx {tx_id}",
                )
            )

        details.append(
            {
                "tidspunkt": timestamp,
                "local_dt": local_dt,
                "coin": coin,
                "amount": amount_mined,
                "amount_net": amount_net,
                "fee": fee,
                "wallet": wallet_name,
                "target": target_raw,
                "tx_id": tx_id,
            }
        )

    result = pd.DataFrame(rows, columns=KRYPTOSEKKEN_COLUMNS)
    if note_style in {"safe", "minimal"} and not result.empty:
        for value in result["Notat"].unique():
            if NOTE_ALLOWED.search(str(value)):
                raise ValueError(
                    f"Ugyldige tegn i Notat: '{value}'"
                )
    for value in result["Marked"].unique() if not result.empty else []:
        if not MARKET_PATTERN.match(str(value)):
            raise ValueError(
                f"Ugyldig Marked-verdi '{value}'. Kryptosekken krever 1-16 tegn "
                "av A-Z, a-z, 0-9 og bindestrek."
            )
    if not result.empty:
        # Eldste transaksjon først; stabil sortering beholder rekkefølgen
        # (inntekt, gebyr, overføring-ut, overføring-inn) innen samme sekund.
        result = result.sort_values("Tidspunkt", kind="stable").reset_index(drop=True)
        details.sort(key=lambda d: d["local_dt"])
    return result, details


# ---------------------------------------------------------------------------
# Skriving og oppsummering
# ---------------------------------------------------------------------------


def write_output(df: pd.DataFrame, output_path: Path) -> None:
    """Skriver importfilen til Kryptosekken (UTF-8 uten BOM, \\n-linjeslutt)."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_path, index=False, encoding="utf-8", lineterminator="\n")
    LOGGER.info("Skrev %d rader til %s", len(df), output_path)


def print_summary(
    details: List[dict],
    output_path: Path,
    rows_written: int,
    estimate: Optional[dict] = None,
    timezone_label: str = "Europe/Oslo",
) -> None:
    """Skriver en oppsummering av konverteringen til konsoll."""
    if not details:
        print("\nIngen rader ble konvertert.")
        return

    total_crypto: Dict[str, Decimal] = {}
    total_fee: Dict[str, Decimal] = {}
    per_wallet: Dict[str, Decimal] = {}
    per_year: Dict[int, Decimal] = {}
    for detail in details:
        total_crypto[detail["coin"]] = (
            total_crypto.get(detail["coin"], Decimal("0")) + detail["amount"]
        )
        total_fee[detail["coin"]] = (
            total_fee.get(detail["coin"], Decimal("0")) + detail["fee"]
        )
        per_wallet[detail["wallet"]] = (
            per_wallet.get(detail["wallet"], Decimal("0")) + detail["amount"]
        )
        year = detail["local_dt"].year
        per_year[year] = per_year.get(year, Decimal("0")) + detail["amount"]

    print("\n" + "=" * 66)
    print("OPPSUMMERING")
    print("=" * 66)
    print(f"Antall utbetalinger       : {len(details)}")
    print(f"Rader skrevet til fil     : {rows_written}")
    print(
        f"Periode                   : {details[0]['tidspunkt']}  ->  "
        f"{details[-1]['tidspunkt']}  ({timezone_label})"
    )
    for coin, amount in sorted(total_crypto.items()):
        print(f"Totalt mottatt {coin:<11}: {format_amount(amount)}")
        if total_fee.get(coin, Decimal("0")) > 0:
            print(f"  herav poolgebyr {coin:<8}: {format_amount(total_fee[coin])}")
    print("\nPer mottakerlommebok:")
    for wallet, amount in sorted(per_wallet.items(), key=lambda kv: -kv[1]):
        print(f"  {wallet:<34} {format_amount(amount)}")
    print("\nPer år (skatteår):")
    for year, amount in sorted(per_year.items()):
        print(f"  {year}: {format_amount(amount)}")
    print(f"\nUtdatafil                 : {output_path}")

    if estimate:
        print(f"\nVeiledende NOK-verdi ved mottak ({estimate['source']}, dagskurs):")
        print(
            "  Sum estimert inntekt    : "
            f"{format_amount(estimate['total_nok'], decimals=2)} NOK"
        )
        if estimate["missing"]:
            print(
                f"  (ufullstendig - {estimate['missing']} av {len(details)} "
                "utbetalinger manglet kurs)"
            )
        print("  Merk: Kryptosekken priser selv krypto-innbetalinger til markedsverdi;")
        print("        dette er bare en kontrollsum, ikke en del av importfilen.")

    print("=" * 66)
    print(
        "Kontroller filen før den importeres. Valg av Marked/navn, tidssone og\n"
        "førring av poolgebyr er ditt ansvar - dette er ikke skatterådgivning."
    )
    print("=" * 66)


# ---------------------------------------------------------------------------
# Kommandolinje og main
# ---------------------------------------------------------------------------


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    """Definerer og leser kommandolinjeargumenter."""
    parser = argparse.ArgumentParser(
        description=(
            "Konverterer mining-utbetalinger fra Braiins Pool (CSV) til "
            "Kryptosekken sitt generiske importformat."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Eksempler:\n"
            "  # Enkel: inntekt føres rett på mottakerlommeboken\n"
            "  python braiins_mining_kryptosekken.py \\\n"
            "      -i btc_payouts.csv -o kryptosekken_mining_import.csv\n\n"
            "  # Før på poolen og overfør videre til lommeboken\n"
            "  python braiins_mining_kryptosekken.py \\\n"
            "      -i btc_payouts.csv -o ut.csv --at-pool --transfer\n"
        ),
    )
    parser.add_argument(
        "-i",
        "--input",
        type=Path,
        default=Path("btc_payouts.csv"),
        help="Braiins-eksport i CSV (standard: btc_payouts.csv)",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("kryptosekken_mining_import.csv"),
        help="Utdatafil i Kryptosekken-format "
        "(standard: kryptosekken_mining_import.csv)",
    )
    parser.add_argument(
        "--timezone",
        default="Europe/Oslo",
        help="Tidssone for Tidspunkt-feltet (standard: Europe/Oslo)",
    )
    parser.add_argument(
        "--source-tz",
        default="UTC",
        help="Tidssone i kildefilen når den ikke har suffiks (standard: UTC)",
    )
    parser.add_argument(
        "--pool-name",
        default=DEFAULT_POOL_NAME,
        help=f"Navn på poolen i Marked/Notat (standard: {DEFAULT_POOL_NAME})",
    )
    parser.add_argument(
        "--at-pool",
        action="store_true",
        help="Før inntekten på poolen i stedet for på mottakerlommeboken",
    )
    parser.add_argument(
        "--transfer",
        action="store_true",
        help="Legg til Overføring-Ut/-Inn fra pool til mottaker (krever --at-pool)",
    )
    parser.add_argument(
        "--gross-fee",
        action="store_true",
        help="Bokfør brutto mining-inntekt og poolgebyret som egen "
        "Forvaltningskostnad (standard: før nettobeløpet)",
    )
    parser.add_argument(
        "--note-style",
        choices=["safe", "full", "minimal"],
        default="safe",
        help="safe (standard): Notat uten @ og |, med kortnavn i stedet for "
        "target-adresse. full: hele adressen og skilletegn. minimal: bare tx-id",
    )
    parser.add_argument(
        "--note-max-length",
        type=int,
        default=NOTE_MAX_LENGTH,
        help=f"Maks lengde på Notat (standard: {NOTE_MAX_LENGTH})",
    )
    parser.add_argument(
        "--estimate-nok",
        action="store_true",
        help="Vis veiledende NOK-verdi fra CoinGecko i oppsummeringen "
        "(påvirker ikke filen)",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Vis detaljert logging (DEBUG)"
    )
    args = parser.parse_args(argv)
    if args.transfer and not args.at_pool:
        parser.error("--transfer krever --at-pool")
    return args


def main(argv: Optional[Iterable[str]] = None) -> int:
    """Hovedfunksjon: leser kilde, bygger rader, skriver Kryptosekken-CSV."""
    args = parse_args(argv)
    setup_logging(args.verbose)

    try:
        payouts = read_payouts(args.input)
        df, details = build_kryptosekken_rows(
            payouts,
            timezone=args.timezone,
            source_tz=args.source_tz,
            pool_name=args.pool_name,
            pool_display=DEFAULT_POOL_DISPLAY if args.pool_name == DEFAULT_POOL_NAME else args.pool_name,
            at_pool=args.at_pool,
            transfer=args.transfer,
            gross_fee=args.gross_fee,
            note_style=args.note_style,
            note_max_length=args.note_max_length,
        )
        write_output(df, args.output)
        estimate = estimate_income_nok(details) if args.estimate_nok else None
        if args.estimate_nok and estimate is None:
            LOGGER.warning(
                "Fikk ingen NOK-kurser - hopper over NOK-estimatet. "
                "Importfilen er komplett uansett."
            )
        print_summary(
            details,
            args.output,
            rows_written=len(df),
            estimate=estimate,
            timezone_label=args.timezone,
        )
        return 0
    except FileNotFoundError as error:
        LOGGER.error("%s", error)
        return 1
    except ValueError as error:
        LOGGER.error("%s", error)
        return 1
    except KeyboardInterrupt:  # pragma: no cover
        LOGGER.error("Avbrutt av brukeren.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
