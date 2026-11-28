import re
from typing import AsyncIterator, Iterable

from scrapy import Request
from scrapy.http import TextResponse

from locations.categories import Categories, apply_category
from locations.hours import OpeningHours, sanitise_day
from locations.items import Feature
from locations.structured_data_spider import StructuredDataSpider

# The locator publishes no coordinates or hours; both are joined in from the brand's ordering API.
API_URL = "https://mobile.incentivio.com/incentivio-mobile-api"
LOCATIONS_URL = (
    f"{API_URL}/locations?count=10000&latitude=0&longitude=0&page=0&radius=11029160"
    "&sortby=title&sortdirection=DESC&langCode=en&iscatering=false"
    "&markdeliverablelocations=false&ismenubrowsing=false"
)


def street_key(street: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", street.lower())).strip()


class HangryJoesUSSpider(StructuredDataSpider):
    name = "hangry_joes_us"
    item_attributes = {
        "brand": "Hangry Joe's Hot Chicken & Wings",
        "brand_wikidata": "Q131378216",
        "name": "Hangry Joe's Hot Chicken & Wings",
    }
    wanted_types = ["Restaurant"]
    name_is_branch = True
    drop_attributes = {"image"}

    async def start(self) -> AsyncIterator[Request]:
        self.stores_by_phone: dict[str, dict] = {}
        self.stores_by_street: dict[tuple, dict] = {}
        yield Request(f"{API_URL}/clients/ordernow.hangryjoes.com/bydomain", callback=self.parse_client)

    def parse_client(self, response: TextResponse) -> Iterable[Request]:
        yield Request(LOCATIONS_URL, headers={"CLIENTID": response.json()["clientId"]}, callback=self.parse_stores)

    def parse_stores(self, response: TextResponse) -> Iterable[Request]:
        for store in response.json()["stores"]:
            if phone := store.get("phoneNumber"):
                self.stores_by_phone[re.sub(r"\D", "", phone)[-10:]] = store
            address = store["address"]
            self.stores_by_street[(street_key(address["streetAddress1"]), address["postalCode"])] = store
        yield Request("https://hangryjoes.com/find-a-location")

    def post_process_item(self, item: Feature, response, ld_item: dict):
        item["ref"] = item["ref"].split("#")[-1]
        street = ld_item["address"]["streetAddress"]
        store = self.stores_by_phone.get(re.sub(r"\D", "", item["phone"])[-10:]) or self.stores_by_street.get(
            (street_key(street.split(", ")[0]), re.search(r"(\d{5})(?:-\d{4})?$", street).group(1))
        )
        if store:
            item["lat"] = store["latitude"]
            item["lon"] = store["longitude"]
            if hours := store.get("pickupOrderHours"):
                oh = OpeningHours()
                for rule in hours:
                    # Some stores leave the day range null and carry times only in timeSlots
                    slots = rule["timeSlots"] if rule["startTime"] is None else [rule]
                    for slot in slots:
                        oh.add_range(
                            sanitise_day(rule["dayOfWeek"]),
                            slot["startTime"],
                            slot["endTime"],
                            time_format="%H:%M:%S",
                        )
                item["opening_hours"] = oh
        item["branch"] = item["branch"].removeprefix("Hangry Joe's ")
        if item["branch"] == item["city"]:
            item.pop("branch")
        # streetAddress embeds the ", City, ST ZIP" tail already held in city/state
        item["street_address"], _, state_and_postcode = item["street_address"].rsplit(", ", 2)
        if (postcode := state_and_postcode.partition(" ")[2]) != "zip unconfirmed":
            item["postcode"] = postcode
        apply_category(Categories.FAST_FOOD, item)
        yield item
