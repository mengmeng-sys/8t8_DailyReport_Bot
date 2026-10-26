# parser.py - reads your message with simple rules (no AI, no API key needed)
import re

ALIASES = {
    "rooms": {
        "pool": ["pool", "poolview", "poolviews", "poolviewsroom"],
        "city": ["city", "cityview", "cityviews", "cityviewsroom"],
        "double_twin": ["double", "twin", "doubletwin", "doubleortwin", "doubleortwinroom"],
        "pod": ["pod", "podroom"],
        "dorm4": ["dorm4", "4bed", "4beddorm", "4beddormitory", "4beddormitoryroom"],
        "dorm6": ["dorm6", "6bed", "6beddorm", "6beddormitory", "6beddormitoryroom"],
    },
    "tours": {
        "killing_field": ["killingfield", "killing", "killingfieldtour"],
        "bus": ["bus", "bustransport"],
        "scooter": ["scooter", "scooterhire"],
        "airport": ["airport", "airportpickup"],
        "sunset_boat": ["sunset", "sunsetboat", "sunsetboattour"],
        "night_city": ["night", "nightcity", "nightcitytour"],
    },
}
LOOKUP = {a: (group, key) for group, items in ALIASES.items()
          for key, names in items.items() for a in names}
BAR_NAMES = {"bar", "restaurant"}


def _is_number(t):
    try:
        float(t)
        return True
    except ValueError:
        return False


def _split(item):
    """'water 12' -> (['water'], [12.0]) ; words before the first number, then numbers"""
    words, nums, rest = [], [], []
    for t in item.split():
        if _is_number(t) and not rest:
            nums.append(float(t))
        elif nums:
            rest.append(t)       # words after the numbers (used for discount reason)
        else:
            words.append(t)
    return words, nums, rest


def parse(text):
    data = {"rooms": {}, "tours": {}, "bar": [], "discount": {}, "expenses": []}
    unknown = []
    for item in re.split(r"[,\n;]+", text):
        item = item.strip()
        if not item:
            continue
        words, nums, rest = _split(item)
        first = words[0].lower() if words else ""
        name = re.sub(r"[^a-z0-9]", "", "".join(words).lower())
        if first in ("expense", "expenses", "exp") and len(words) > 1 and nums:
            data["expenses"].append({"name": " ".join(words[1:]), "amount": nums[0]})
        elif first == "discount" and nums:
            data["discount"] = {"amount": nums[0], "reason": " ".join(rest)}
        elif name in BAR_NAMES and nums:
            data["bar"] = (nums + [0, 0, 0])[:3]
        elif name in LOOKUP and nums:
            group, key = LOOKUP[name]
            data[group][key] = (nums + [0, 0, 0])[:3]
        else:
            unknown.append(item)
    return data, unknown
