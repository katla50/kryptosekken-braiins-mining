# Braiins Pool mining → Kryptosekken

Konverterer en utbetalings-eksport (CSV) fra **Braiins Pool** til
**Kryptosekken** sitt generiske importformat, slik at mining-inntekten havner
riktig i skatteberegningen — med riktig type, riktig tidspunkt og riktig
mottakerlommebok.

Lagd for samme formål som `kryptosekken-deposit-converter` (kryptokort), men
dette er **inntekt**, ikke salg: hver utbetaling føres som `Mining`, ikke
`Handel`. Kryptosekken verdsetter `Mining`-rader til markedsverdi på
tidspunktet, som er slik mining-inntekt skal beskattes.

## Kildens format

Eksporten fra Braiins (API: `account/payouts`, eller «Export CSV» i UI-et) ser
slik ut:

```csv
account,amount,user_fee,coin,target,date,transaction_id
testworker,0.00012345,0.00000000,BTC,testuser@getalby.com,2026-01-10 09:06:07,aaaa1111...
```

| Kolonne | Betydning |
|---|---|
| `account` | worker-/konto-navn |
| `amount` | beløp som faktisk ble sendt (netto) |
| `user_fee` | poolgebyr trukket fra opptjeningen |
| `coin` | myntsymbol (`BTC`, `BCH`, …) |
| `target` | mottaker: Lightning-adresse, on-chain-adresse eller børskonto |
| `date` | tidspunkt i **UTC** |
| `transaction_id` | Brukes til å oppdage duplikater |

## Utdata (Kryptosekken generisk import)

`Tidspunkt,Type,Inn,Inn-Valuta,Ut,Ut-Valuta,Gebyr,Gebyr-Valuta,Marked,Notat`

Én rad per utbetaling:

```csv
2026-01-10 10:06:07,Mining,0.00012345,BTC,,,,,Alby,Mining payout fra Braiins Pool til Alby (worker testworker), tx aaaa1111...
```

- `Inn` = kryptomengden (ikke NOK) — Kryptosekken priser den selv.
- `Marked` = mottakerlommeboken på **kortform** (`Alby`, `Strike`, `Primal`), slik at
  saldoen havner der myntene faktisk ligger. Feltet godtar bare 1–16 tegn av `A-Z`,
  `a-z`, `0-9` og bindestrek — mellomrom og parenteser gir importfeilen
  «Innholdet i feltet Marked er ikke gyldig». Full adresse står i `Notat`.
- `Notat` = pool, lommebok, worker og `transaction_id` (sporbarhet) — holdt innenfor
  det tegnsettet Kryptosekken faktisk godtar (se under).

## To måter å føre utbetalingene på

**1. Direkte på mottakerlommeboken (standard)**

```bash
.venv/bin/python braiins_mining_kryptosekken.py \
    -i btc_payouts.csv -o kryptosekken_mining_import.csv
```

Bruk denne når Lightning-lommeboken (Alby, Strike, Primal …) ikke følges
separat i Kryptosekken. Da slipper du overføringsrader og får aldri negativ
saldo noe sted.

**2. Først på poolen, deretter overføring**

```bash
.venv/bin/python braiins_mining_kryptosekken.py \
    -i btc_payouts.csv -o ut.csv --at-pool --transfer
```

Gir tre rader per utbetaling: `Mining` på `Braiins-Pool`, `Overføring-Ut` fra
poolen og `Overføring-Inn` på mottakeren. Bruk denne hvis du følger
mottakerlommeboken i Kryptosekken og senere skal selge/veksle fra den — da må
saldoen stemme i begge ender.

## Valg

| Flagg | Standard | Betydning |
|---|---|---|
| `-i, --input` | `btc_payouts.csv` | Braiins-eksport |
| `-o, --output` | `kryptosekken_mining_import.csv` | importfil |
| `--timezone` | `Europe/Oslo` | tidssone i `Tidspunkt` |
| `--source-tz` | `UTC` | tidssone i kilden når den ikke har suffiks |
| `--pool-name` | `Braiins-Pool` | navn i `Marked`/`Notat` (saniteres til gyldig `Marked`) |
| `--at-pool` | av | før inntekten på poolen |
| `--transfer` | av | legg til `Overføring-Ut/-Inn` (krever `--at-pool`) |
| `--gross-fee` | av | før brutto og bokfør poolgebyret som `Forvaltningskostnad` |
| `--note-style` | `safe` | `safe`: Notat uten `@`/`|` · `minimal`: bare tx-id · `full`: hele adressen |
| `--note-max-length` | `200` | maks lengde på `Notat` |
| `--estimate-nok` | av | vis veiledende NOK-verdi i oppsummeringen (påvirker ikke filen) |
| `-v, --verbose` | av | DEBUG-logging |

### Gebyr

Standard er å føre **netto** (`amount`), altså det som faktisk ble mottatt;
`user_fee` skrives i `Notat` for sporbarhet. Vil du ha poolgebyret som eget
fradrag, bruk `--gross-fee`: da føres `amount + user_fee` som `Mining` og
gebyret som en egen `Forvaltningskostnad`-rad i samme mynt.

### NOK-estimat

`--estimate-nok` henter BTC/USD fra Binance og USD/NOK fra Norges Bank (to
HTTP-kall for hele perioden) og skriver en kontrollsum i oppsummeringen. Den
skrives **ikke** i importfilen — Kryptosekken priser selv. Andre mynter hentes
fra CoinGecko.

## Forutsetninger og fallgruver

- **Tidspunkt:** Braiins oppgir UTC uten suffiks. Skriptet tolker kilden som
  UTC (overstyrbart med `--source-tz`) og skriver norsk tid, med riktig
  sommer-/vintertid.
- **Duplikater:** rader fjernes på `transaction_id`. En eksport som er limt
  sammen to ganger, eller hentet på nytt med overlappende periode, ville ellers
  dobbeltført inntekten.
- **Dobbeltimport i Kryptosekken:** importerer du samme fil to ganger, blir
  alt ført to ganger. Slett de gamle radene først hvis du må importere på nytt.
- **Ikke dobbeltfør overføringen:** bruker du `--at-pool --transfer`, må du
  ikke i tillegg føre de samme overføringene manuelt/importere dem på nytt.
- **`Notat`-feltet er også validert.** Samme feiltype («Innholdet i feltet Notat er
  ikke gyldig») kommer når notatet inneholder tegn feltet ikke tar, og `@` og `|` er
  observert avvist. Derfor skriver `--note-style safe` (standard) notatet uten dem:
  lommeboken nevnes med kortnavnet fra `Marked`, og tx-id beholdes i sin helhet.
  Nekter Kryptosekken fortsatt, prøv `--note-style minimal` (bare `tx <id>`) eller
  `--note-max-length 100`.
- **`Marked`-feltet:** Kryptosekken krever 1–16 tegn av `A-Z`, `a-z`, `0-9` og
  bindestrek. Skriptet saniterer navnene (`Alby (testuser)` → `Alby`) og nekter å
  skrive filen hvis en verdi likevel er ugyldig.
- **Beløp:** skrives med punktum som desimaltegn, uten tusenskille, med samme
  antall desimaler som kilden.
- **Dette er ikke skatterådgivning.** Kontroller `Marked`-navn, tidssone og
  gebyrføring før filen brukes.

## Oppsett

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -q          # 19 tester
```

## Filer

- `braiins_mining_kryptosekken.py` — konvertereren
- `tests/test_converter.py` — tester
- `examples/braiins_payouts_example.csv` — syntetisk eksempelfil (ingen ekte data)
- `README.md`

Ekte eksport- og importfiler skal ligge utenfor repoet (`.gitignore` dekker
`*.csv` bortsett fra `examples/`).

## Lisens

MIT — se [LICENSE](LICENSE). © 2026 katla50.
