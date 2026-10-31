"""Home region for the Penny Scanner. Change this file to move the app to another area.

update_list.py writes this into penny-list.json ("home_area"), and app.js reads it from there,
so this is the only place the location lives.
"""

REGION = {
    "label": "Clarksville / Nashville",
    "short": "Clarksville–Nashville",
    "state": "TN",
    "state_name": "Tennessee",
    "zip": "37040",
    # Store locations within roughly 45 minutes of Clarksville or Nashville, written "City, ST"
    # (the format the Lowe's penny list uses). Includes the Kentucky side of Fort Campbell.
    "near_cities": [
        "Clarksville, TN", "Oak Grove, KY", "Hopkinsville, KY", "Springfield, TN", "Dickson, TN",
        "Ashland City, TN", "White House, TN", "Goodlettsville, TN", "Hendersonville, TN", "Gallatin, TN",
        "Madison, TN", "Nashville, TN", "Antioch, TN", "Hermitage, TN", "Bellevue, TN", "Brentwood, TN",
        "Franklin, TN", "Mt. Juliet, TN", "Mount Juliet, TN", "Lebanon, TN", "Smyrna, TN", "La Vergne, TN",
        "Murfreesboro, TN", "Spring Hill, TN", "Columbia, TN", "Portland, TN",
    ],
    "home_store": {"name": "Home Depot Clarksville", "state": "TN"},
}

HOME_STATE = REGION["state"]
