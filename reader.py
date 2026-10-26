# reader.py - free-form parser with rich aliases (no AI needed)
import re

# Rich aliases – short forms, common spellings, and natural variants
ALIASES = {
    "rooms": {
        "pool": [
            "pool", "poolview", "poolviews", "poolviewsroom", "poolroom",
            "pv", "pview", "poolv",
        ],
        "city": [
            "city", "cityview", "cityviews", "cityviewsroom", "cityroom",
            "cv", "cview", "cityv",
        ],
        "double_twin": [
            "double", "twin", "doubletwin", "doubleortwin", "doubleortwinroom",
            "dbl", "dt", "dble", "doubletwinroom", "twindouble",
        ],
        "pod": [
            "pod", "podroom", "pods", "podrooms",
        ],
        "dorm4": [
            "dorm4", "4bed", "4beddorm", "4beddormitory", "4beddormitoryroom",
            "4b", "4bd", "fourbed", "4bedroom", "dorm4bed",
        ],
        "dorm6": [
            "dorm6", "6bed", "6beddorm", "6beddormitory", "6beddormitoryroom",
            "6b", "6bd", "sixbed", "6bedroom", "dorm6bed",
        ],
    },
    "tours": {
        "killing_field": [
            "killingfield", "killing", "killingfieldtour", "kf", "kfield",
            "killingfields", "killingfieldstour", "kftour",
        ],
        "bus": [
            "bus", "bustransport", "bustrans", "transport", "busticket",
        ],
        "scooter": [
            "scooter", "scooterhire", "scoot", "scooterental", "motorbike",
            "moto", "bikehire",
        ],
        "airport": [
            "airport", "airportpickup", "pickup", "airporttransfer",
            "airportpick", "ap", "airportpu",
        ],
        "sunset_boat": [
            "sunset", "sunsetboat", "sunsetboattour", "boat", "boattour",
            "sunsettour", "sb", "sboat",
        ],
        "night_city": [
            "night", "nightcity", "nightcitytour", "citytour", "nighttour",
            "nc", "nctour", "nightc",
        ],
    },
}

# Build reverse lookup: normalized name → (group, key)
LOOKUP = {
    a: (group, key)
    for group, items in ALIASES.items()
    for key, names in items.items()
    for a in names
}

BAR_NAMES = {
    "bar", "restaurant", "resto", "fnb", "food", "drink", "drinks",
    "cafe", "coffee", "beverage",
}


def _is_number(t):
    try:
        float(t)
        return True
    except ValueError:
        return False


def _normalize(text: str) -> str:
    """Lowercase + remove everything except letters and digits."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _split(item: str):
    """
    Split an item into (words_before_numbers, numbers, words_after_numbers).
    Examples:
        'pool 40 0 20'     → (['pool'], [40.0, 0.0, 20.0], [])
        'water 12'         → (['water'], [12.0], [])
        'discount 5 long'  → (['discount'], [5.0], ['long'])
        '5 long stay'      → ([], [5.0], ['long', 'stay'])
    """
    words, nums, rest = [], [], []
    for t in item.replace(",", " ").split():
        if _is_number(t) and not rest:
            nums.append(float(t))
        elif nums:
            rest.append(t)
        else:
            words.append(t)
    return words, nums, rest


def parse(text: str):
    """
    Parse free-form text into structured report data.
    Supports:
      - "pool 40, city 20 10, bar 80"
      - "kf 30 0 0, boat 25, airport 15"
      - "discount 5 long stay"
      - "expense water 12" or "exp laundry 8"
      - Multiple items separated by comma, newline or semicolon
    """
    data = {
        "rooms": {},
        "tours": {},
        "bar": [],
        "discount": {},
        "expenses": [],
    }
    unknown = []

    # Split on common separators
    for raw in re.split(r"[,;\n]+", text):
        item = raw.strip()
        if not item:
            continue

        words, nums, rest = _split(item)
        if not nums:
            # No numbers at all → skip or treat as unknown
            unknown.append(item)
            continue

        first = words[0].lower() if words else ""
        # Join all words and normalize for lookup
        name = _normalize("".join(words))

        # --- Expenses ---
        if first in ("expense", "expenses", "exp", "cost") and len(words) > 1:
            data["expenses"].append({
                "name": " ".join(words[1:]),
                "amount": nums[0],
            })
            continue

        # --- Discount ---
        if first in ("discount", "disc", "promo") or name in ("discount", "disc"):
            data["discount"] = {
                "amount": nums[0],
                "reason": " ".join(rest) if rest else " ".join(words[1:]) if len(words) > 1 else "",
            }
            continue

        # --- Bar / Restaurant ---
        if name in BAR_NAMES or first in BAR_NAMES:
            data["bar"] = (nums + [0, 0, 0])[:3]
            continue

        # --- Rooms & Tours ---
        if name in LOOKUP:
            group, key = LOOKUP[name]
            data[group][key] = (nums + [0, 0, 0])[:3]
            continue

        # Try matching only the first word (more forgiving)
        if words:
            first_norm = _normalize(words[0])
            if first_norm in LOOKUP:
                group, key = LOOKUP[first_norm]
                data[group][key] = (nums + [0, 0, 0])[:3]
                continue

        # Nothing matched
        unknown.append(item)

    return data, unknown