# Pair quality report — 2026-08

Generated 2026-08-25 20:27 UTC by `scripts/pair_quality.py` (issue #2).

## Method

The script downloads recent public trades from kraken with the ccxt library.
No API keys are used. Only public endpoints are called. No orders are placed.
For each pair the script pages through the trade history until it has 10000 trades and 7 days of coverage, or until it hits the cap of 60 requests per pair.
Pairs with less coverage than the minimum days are marked low confidence. Time-based metrics for these pairs rest on a short window.

The metrics follow published heuristics:

- Benford first-digit test on trade sizes. Cong et al. (2023) show that wash trading distorts the first-digit distribution. The chi-square statistic (8 degrees of freedom) measures the deviation from the Benford law. Large values are a warning sign.
- Round-size share. A trade counts as round when its base size equals its own value rounded to 1 or 2 significant digits. Round sizes are a documented retail fingerprint.
- Weekend volume share (UTC Saturday and Sunday). Retail flow is active on weekends. Institutional flow concentrates on weekdays.

## Metrics

| Pair | Venue | Coverage (days) | Trades | Median size (EUR) | Daily volume (EUR) | Round share % (cnt/vol) | Benford χ² | Weekend vol % | Buy/sell | Flags |
|---|---|---|---|---|---|---|---|---|---|---|
| BTC/EUR | kraken | 1.81 | 59941 | 85.69 | 48,787,928 | 8.5 / 8.1 | 1,274.5 | 0.0 | 1.73 | low confidence |
| ETH/EUR | kraken | 2.84 | 59941 | 95.77 | 28,846,881 | 7.1 / 7.6 | 1,757.3 | 0.0 | 1.51 | low confidence |
| XRP/EUR | kraken | 3.06 | 59941 | 62.15 | 18,106,732 | 7.7 / 3.0 | 6,546.3 | 0.0 | 1.25 | low confidence |
| BTC/EUR | binance | 1.50 | 48547 | 105.3 | 12,723,294 | 58.1 / 19.8 | 4,321.8 | 0.0 | 1.03 | low confidence, aggregated trades |
| SOL/EUR | kraken | 3.28 | 59941 | 46.71 | 9,589,578 | 7.0 / 2.9 | 2,373.4 | 2.2 | 1.58 | low confidence |
| ADA/EUR | kraken | 7.00 | 52684 | 51.62 | 2,711,660 | 16.6 / 3.3 | 10,803.4 | 39.4 | 1.12 | — |
| LINK/EUR | kraken | 7.00 | 37556 | 43.1 | 2,388,740 | 6.2 / 2.7 | 4,570.2 | 38.4 | 1.07 | — |
| DOGE/EUR | kraken | 7.00 | 33036 | 28.11 | 1,781,430 | 5.9 / 3.0 | 3,268.4 | 40.8 | 1.03 | — |
| LTC/EUR | kraken | 7.00 | 56970 | 26.42 | 1,162,710 | 5.5 / 3.9 | 3,201.3 | 24.2 | 1.30 | — |
| AVAX/EUR | kraken | 7.00 | 14799 | 51.82 | 876,374 | 6.0 / 2.9 | 929.2 | 27.8 | 1.00 | — |
| DOT/EUR | kraken | 6.99 | 11612 | 35.01 | 329,658 | 10.8 / 5.5 | 1,152.8 | 26.6 | 1.00 | — |
| ATOM/EUR | kraken | 6.99 | 6146 | 30.62 | 121,356 | 3.5 / 1.7 | 3,807.7 | 35.6 | 1.00 | — |

The table is sorted by estimated daily quote volume.

## Coverage

- BTC/EUR (kraken): 2026-08-18T20:14:44+00:00 to 2026-08-20T15:45:26+00:00, 59941 trades, 60 requests. LOW CONFIDENCE: coverage under the minimum days.
- ETH/EUR (kraken): 2026-08-18T20:15:58+00:00 to 2026-08-21T16:22:39+00:00, 59941 trades, 60 requests. LOW CONFIDENCE: coverage under the minimum days.
- XRP/EUR (kraken): 2026-08-18T20:18:44+00:00 to 2026-08-21T21:45:55+00:00, 59941 trades, 60 requests. LOW CONFIDENCE: coverage under the minimum days.
- BTC/EUR (binance): 2026-08-18T20:24:02+00:00 to 2026-08-20T08:30:48+00:00, 48547 trades, 60 requests. LOW CONFIDENCE: coverage under the minimum days.
- SOL/EUR (kraken): 2026-08-18T20:17:13+00:00 to 2026-08-22T03:04:14+00:00, 59941 trades, 60 requests. LOW CONFIDENCE: coverage under the minimum days.
- ADA/EUR (kraken): 2026-08-18T20:21:46+00:00 to 2026-08-25T20:20:16+00:00, 52684 trades, 54 requests.
- LINK/EUR (kraken): 2026-08-18T20:22:09+00:00 to 2026-08-25T20:20:01+00:00, 37556 trades, 39 requests.
- DOGE/EUR (kraken): 2026-08-18T20:25:20+00:00 to 2026-08-25T20:22:00+00:00, 33036 trades, 35 requests.
- LTC/EUR (kraken): 2026-08-18T20:22:49+00:00 to 2026-08-25T20:22:56+00:00, 56970 trades, 58 requests.
- AVAX/EUR (kraken): 2026-08-18T20:27:10+00:00 to 2026-08-25T20:20:37+00:00, 14799 trades, 16 requests.
- DOT/EUR (kraken): 2026-08-18T20:23:38+00:00 to 2026-08-25T20:14:21+00:00, 11612 trades, 13 requests.
- ATOM/EUR (kraken): 2026-08-18T20:34:40+00:00 to 2026-08-25T20:20:08+00:00, 6146 trades, 8 requests.

## Reading

Ranking from most institutionally clean to most suspicious. The score is the mean normalised rank of three metrics: Benford chi-square, round-size share by volume, and weekend volume share. Low-confidence pairs do not score the weekend metric. Lower is cleaner.

1. AVAX/EUR (kraken) — score 0.17
2. SOL/EUR (kraken) — score 0.32
3. DOT/EUR (kraken) — score 0.33
4. LTC/EUR (kraken) — score 0.36
5. ATOM/EUR (kraken) — score 0.38
6. LINK/EUR (kraken) — score 0.53
7. BTC/EUR (kraken) — score 0.55
8. ETH/EUR (kraken) — score 0.55
9. DOGE/EUR (kraken) — score 0.64
10. XRP/EUR (kraken) — score 0.68
11. ADA/EUR (kraken) — score 0.79
12. BTC/EUR (binance) — score 0.86

Caveats:

- These are heuristics on one venue and one recent window. They are not proof.
- A high Benford chi-square has many possible causes. Wash trading is one cause.
- Round sizes and weekend activity point at retail flow, not at manipulation.
- Coarse lot-size quantisation inflates the round-size share. Compare venues with care.
- Low-liquidity pairs give unstable statistics. Treat their ranks with care.
- The Binance row uses aggregated trades (aggTrades). It is a comparison, not a peer.

## Skipped symbols

None. All requested symbols resolved.
