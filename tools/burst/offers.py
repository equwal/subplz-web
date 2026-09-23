"""Live offers of the clouds: which machines each one can start now, and at what price.

Each function reads one provider's API and returns `core.Offer` objects. The
controller asks again each time it wants a new worker, so a price drop or new
stock at one cloud moves the next worker there.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from .core import Offer

# Instance types that can run a worker (4 vCPU, 8 GB), and their vCPUs. Arm
# types (c7g, c6g) need an arm64 install that nobody has measured yet: turn
# them on with SUBPLZ_BURST_AWS_TYPES after one test book.
AWS_VCPUS = {
    "c6a.xlarge": 4, "c7a.xlarge": 4, "m6a.xlarge": 4, "c6i.xlarge": 4, "c7i.xlarge": 4,
    "c7g.xlarge": 4, "c6g.xlarge": 4, "m7g.xlarge": 4,
}
AWS_ARM = {"c7g.xlarge", "c6g.xlarge", "m7g.xlarge"}


def aws_offers(
    spot_prices: Iterable[dict],
    *,
    max_price: float,
    extra_usd_per_hour: float,
    extra_usd_per_book: float,
    capacity: int,
) -> list[Offer]:
    """Offers from AWS spot price history (DescribeSpotPriceHistory items).

    The newest price of each instance type in each availability zone counts.
    A price above `max_price` is left out: the spot request would not start.
    `extra_usd_per_hour` is the public IPv4 address and the disk.
    """
    newest: dict[tuple[str, str], tuple[str, float]] = {}
    for item in spot_prices:
        key = (item["InstanceType"], item["AvailabilityZone"])
        stamp = str(item["Timestamp"])
        if key not in newest or stamp > newest[key][0]:
            newest[key] = (stamp, float(item["SpotPrice"]))
    return [
        Offer(
            provider="aws", machine=machine, zone=zone, vcpus=AWS_VCPUS[machine],
            usd_per_hour=price + extra_usd_per_hour, per_started_hour=False,
            extra_usd_per_book=extra_usd_per_book, capacity=capacity,
        )
        for (machine, zone), (_, price) in sorted(newest.items())
        if machine in AWS_VCPUS and price <= max_price
    ]


def fetch_aws_spot_prices(ec2, machines: list[str], since) -> list[dict]:
    """DescribeSpotPriceHistory for Linux, all pages."""
    items: list[dict] = []
    pages = ec2.get_paginator("describe_spot_price_history").paginate(
        InstanceTypes=machines, ProductDescriptions=["Linux/UNIX"], StartTime=since,
    )
    for page in pages:
        items.extend(page["SpotPriceHistory"])
    return items


def hetzner_offers(
    get_json: Callable[[str], dict],
    *,
    machines: set[str],
    usd_per_eur: float,
    ipv4_eur_per_hour: float,
    capacity: int,
) -> list[Offer]:
    """Offers from the Hetzner Cloud API: the server types for sale in each
    location, at the hourly net price, where a datacenter has them in stock."""
    server_types = get_json("/server_types?per_page=50")["server_types"]
    in_stock = {
        (dc["location"]["name"], type_id)
        for dc in get_json("/datacenters?per_page=50")["datacenters"]
        for type_id in dc["server_types"]["available"]
    }
    offers = []
    for st in server_types:
        if st["name"] not in machines or st.get("architecture") != "x86" or st.get("deprecation"):
            continue
        for price in st["prices"]:
            if (price["location"], st["id"]) not in in_stock:
                continue
            eur = float(price["price_hourly"]["net"]) + ipv4_eur_per_hour
            offers.append(Offer(
                provider="hetzner", machine=st["name"], zone=price["location"],
                vcpus=int(st["cores"]), usd_per_hour=eur * usd_per_eur,
                per_started_hour=True, capacity=capacity,
            ))
    return sorted(offers, key=lambda o: (o.machine, o.zone))
