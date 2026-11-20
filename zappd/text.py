"""Text normalization shared by menu search (catalog.py) and ingestion (ingest.py).

One function, so "LG FRY" means the same thing whether it comes from a POS
export or from a customer typing into an agent.
"""

import re

# Tribal knowledge about how this restaurant (and its customers) abbreviate things.
ABBREVIATIONS = {
    "lg": "large", "reg": "regular", "fry": "fries", "rngs": "rings", "chkn": "chicken",
    "crspy": "crispy", "sand": "sandwich", "sammy": "sandwich", "dbl": "double", "mush": "mushroom",
    "brg": "burger", "dt": "diet", "icd": "iced", "van": "vanilla", "choc": "chocolate",
    "bkfst": "breakfast", "bisc": "biscuit", "clsc": "classic", "milkshake": "shake",
    "coca": "coke", "cola": "",
}

# Words that carry no identity when MATCHING records ("Bolt Classic" == "Bolt Classic Burger").
FILLER = {"the", "and", "french", "burger"}


def normalize(name: str, drop_filler: bool = True) -> str:
    """'LG FRY' -> 'large fries', 'Mushroom & Swiss' -> 'mushroom swiss'."""
    words = re.sub(r"[^a-z0-9# ]", " ", name.lower()).split()
    expanded = [ABBREVIATIONS.get(w, w) for w in words]
    return " ".join(w for w in expanded if w and not (drop_filler and w in FILLER))
