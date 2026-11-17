---
license: cc-by-4.0
language:
  - en
pretty_name: EverXCloud GPU Rental Price Report, November 2026
tags:
  - gpu
  - cloud-computing
  - pricing
  - h100
  - b200
  - ai-infrastructure
task_categories:
  - tabular-regression
size_categories:
  - n<1K
configs:
  - config_name: prices
    default: true
    data_files:
      - split: train
        path: data/gpu_rental_prices_2026_11.csv
  - config_name: summary
    data_files:
      - split: train
        path: data/summary_2026_11.csv
---

<p align="center">
  <a href="https://everxcloud.ai/?utm_source=github&utm_medium=readme&utm_campaign=brand">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="assets/brand/everxcloud-logo-on-dark.svg">
      <img src="assets/brand/everxcloud-logo-on-light.svg" alt="EverXCloud: Every GPU. One provider." width="420">
    </picture>
  </a>
</p>

# EverXCloud GPU Rental Price Report, November 2026

Public list prices for renting NVIDIA data center and consumer GPUs in the cloud, normalized to USD per GPU-hour. The dataset contains 75 prices for eight GPUs (H100 SXM, H100 PCIe, H200, B200, A100 80GB, L40S, RTX 4090 and RTX 5090) across 16 cloud providers, read on 2026-10-26 from each provider's official pricing page. Every row records the provider, the GPU, the offer type, the price, the exact listing used and the source URL, so each figure can be traced and checked. A per-GPU summary gives the number of providers, minimum, median, maximum and spread for each offer type.

The dataset is published by EverXCloud Inc., a US-based GPU rental marketplace. It contains third-party public prices only; no EverXCloud prices are included.

- Report page: https://everxcloud.ai/en/reports/gpu-rental-price-report-2026-11
- Source CSV: https://everxcloud.ai/media/reports/everxcloud-gpu-rental-price-report-2026-11.csv
- Website: https://everxcloud.ai

## Key findings

- The median on-demand H100 SXM price across 11 providers is $3.99 per GPU-hour, from $3.14 to $6.16: the most expensive listing costs 2.0× the cheapest.
- Spot, preemptible and community H100 SXM capacity has a median of $2.30 per GPU-hour across 3 providers, 42% below the on-demand median.
- The H200 carries a 24% premium over the H100 SXM at the median ($4.94 vs $3.99 on demand, 10 providers).
- On-demand B200 prices range from $6.00 to $8.60 per GPU-hour across 7 providers, with a median of $8.19.
- The A100 80GB shows the widest relative spread among data center GPUs: 2.6× between the cheapest ($1.09) and the most expensive ($2.79) on-demand listing, across 9 providers.
- AWS Capacity Blocks, a short-term reservation, list the H100 at $5.97 and the B200 at $14.21 per GPU-hour, 1.5× and 1.7× the on-demand medians of the other providers.

## On-demand summary (USD per GPU-hour)

| GPU | Providers | Min | Median | Max | Spread (max ÷ min) |
|---|---:|---:|---:|---:|---:|
| H100 SXM | 11 | 3.14 | 3.99 | 6.155 | 1.96× |
| H100 PCIe | 3 | 2.89 | 3.29 | 3.50 | 1.21× |
| H200 | 10 | 3.62 | 4.94 | 6.305 | 1.74× |
| B200 | 7 | 6.00 | 8.19 | 8.60 | 1.43× |
| A100 80GB | 9 | 1.09 | 1.49 | 2.79 | 2.56× |
| L40S | 6 | 0.97 | 1.56 | 2.25 | 2.32× |
| RTX 4090 | 3 | 0.39 | 0.64 | 0.89 | 2.28× |
| RTX 5090 | 2 | 0.65 | 0.92 | 1.19 | 1.83× |

Spot, preemptible and community statistics, and the AWS Capacity Block prices, are in `data/summary_2026_11.csv`.

## Files

| File | Description |
|---|---|
| `data/gpu_rental_prices_2026_11.csv` | 75 price rows, one per provider, GPU and offer type. Byte-identical to the CSV published with the report. |
| `data/gpu_rental_prices_2026_11.json` | The same 75 rows as a JSON array, with prices as numbers. |
| `data/summary_2026_11.csv` | Statistics per GPU and offer type, computed from the price rows. |
| `scripts/check.py` | Integrity check (Python standard library only) of every edition: columns, values, JSON and summary consistency. |
| `scripts/refresh.py` | Monthly refresher: re-reads each provider's pricing page and writes the next edition (standard library only). |
| `CITATION.cff` | Citation metadata. |
| `LICENSE` | Creative Commons Attribution 4.0 International legal code. |

## Column dictionary

### `gpu_rental_prices_2026_11.csv` / `.json`

| Column | Type | Description |
|---|---|---|
| `provider` | string | Cloud provider name. |
| `gpu` | string | GPU model: `H100 SXM`, `H100 PCIe`, `H200`, `B200`, `A100 80GB`, `L40S`, `RTX 4090` or `RTX 5090`. |
| `offer` | string | Offer type: `on-demand`; `spot-community` (spot, preemptible or community capacity); `reserved-capacity-block` (short-term reservation, AWS Capacity Blocks only). |
| `price_usd_per_gpu_hour` | number | Public list price in USD per GPU-hour, excluding tax. |
| `listing` | string | The listing the price was taken from, including the per-GPU conversion when the provider prices a multi-GPU instance (for example `8x instance $49.24 / 8`). |
| `source_url` | string | Official pricing page the price was read from. |
| `retrieved` | date | Date the price was read (`2026-10-26`). |

### `summary_2026_11.csv`

| Column | Type | Description |
|---|---|---|
| `gpu` | string | GPU model. |
| `offer` | string | Offer type (same values as above). |
| `providers` | integer | Number of providers with a price for this GPU and offer type (one price per provider). |
| `min_usd_per_gpu_hour` | number | Lowest price. |
| `median_usd_per_gpu_hour` | number | Median price (mean of the two middle values when the count is even). |
| `max_usd_per_gpu_hour` | number | Highest price. |
| `spread_max_over_min` | number | Highest price divided by lowest price, rounded to three decimals. |

## Methodology

- Prices were read on 2026-10-26 on each provider's official public pricing page, in USD per GPU-hour, excluding tax. No price was estimated, negotiated or taken from a third-party aggregator.
- One price per provider, GPU and offer type. When a provider lists several configurations for the same GPU, the lowest listed price was kept, and the listing is recorded in the data.
- Multi-GPU instances are converted to a per-GPU price by dividing the instance price by the number of GPUs (for example CoreWeave and AWS 8-GPU instances).
- On-demand and spot, preemptible or community prices are reported separately. AWS prices are Capacity Block rates, a short-term reservation, and are not included in the on-demand statistics.
- Providers whose pricing page could not be read, showed prices only after sign-in or on request, or did not map prices to GPUs unambiguously were left out: Vast.ai, Crusoe, Vultr, TensorDock, Fluidstack, Genesis Cloud, Google Cloud, Microsoft Azure, Voltage Park, Cudo Compute, Scaleway, Paperspace and SF Compute.
- List prices change often. The data reflects the retrieval date only and does not capture discounts, committed-use contracts or regional variation beyond what each listing states.

## Providers and sources

| Provider | Pricing page |
|---|---|
| AWS | https://aws.amazon.com/ec2/capacityblocks/pricing/ |
| CloudRift | https://www.cloudrift.ai/gpu-rentals |
| CoreWeave | https://www.coreweave.com/pricing |
| DigitalOcean | https://www.digitalocean.com/pricing/gpu-droplets |
| Hyperstack | https://www.hyperstack.cloud/gpu-pricing |
| JarvisLabs | https://jarvislabs.ai/pricing |
| Lambda | https://lambda.ai/pricing |
| Lium | https://lium.io/gpu/rtx-4090 |
| Massed Compute | https://vm.massedcompute.com/pricing |
| Nebius | https://nebius.com/prices |
| Oblivus | https://www.oblivus.com/pricing/ |
| Runpod | https://www.runpod.io/pricing |
| Salad | https://salad.com/pricing |
| Thunder Compute | https://www.thundercompute.com/pricing |
| Together AI | https://www.together.ai/pricing |
| Verda (DataCrunch) | https://verda.com/pricing |

## Usage

```python
from datasets import load_dataset

prices = load_dataset("everxcloud/gpu-rental-prices", "prices", split="train")
summary = load_dataset("everxcloud/gpu-rental-prices", "summary", split="train")
```

```python
import pandas as pd

prices = pd.read_csv("data/gpu_rental_prices_2026_11.csv")
prices[prices.offer == "on-demand"].groupby("gpu").price_usd_per_gpu_hour.median()
```

Run `python3 scripts/check.py` to verify the files.

## Update cadence

Monthly. Each edition is published as new dated files (`*_YYYY_MM.csv`); earlier editions are kept unchanged.

On the 1st of each month at 06:00 UTC, the [`Monthly price refresh`](.github/workflows/monthly-refresh.yml) workflow re-reads every source URL with `scripts/refresh.py`, writes the new edition (price CSV, JSON and summary), runs `scripts/check.py` and opens a pull request. Nothing is published automatically: a maintainer checks the pull request before merging. Its description lists:

- every price that changed, with the previous value and the change in percent (changes above 50% are flagged);
- every unreadable row, with the reason. The refresher never guesses a price: when a page cannot be fetched or a price cannot be located unambiguously, the row keeps its previous price, listing and `retrieved` date, so `retrieved` always shows when a value was last read.

To preview a refresh locally (network access, Python 3.10+ standard library only):

```bash
python3 scripts/refresh.py                       # print the report, write nothing
python3 scripts/refresh.py --write --edition 2026_12
python3 -m unittest discover -s tests            # offline tests
```

## How to cite

Suggested citation: "Source: EverXCloud GPU Rental Price Report, November 2026", with a link to https://everxcloud.ai/en/reports/gpu-rental-price-report-2026-11.

```bibtex
@misc{everxcloud_gpu_rental_price_report_2026_11,
  author       = {{EverXCloud Inc.}},
  title        = {GPU Rental Price Report, November 2026},
  year         = {2026},
  month        = nov,
  version      = {2026.11},
  howpublished = {\url{https://everxcloud.ai/en/reports/gpu-rental-price-report-2026-11}},
  note         = {Prices retrieved 2026-10-26. Licensed under CC BY 4.0}
}
```

## License

Creative Commons Attribution 4.0 International (CC BY 4.0). You may share and adapt the data for any purpose, including commercial use, provided you give appropriate credit. See `LICENSE` and https://creativecommons.org/licenses/by/4.0/.

Provider names and trademarks belong to their respective owners. Prices are public list prices as displayed on the retrieval date and may have changed since.
