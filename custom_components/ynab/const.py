"""Constants for the YNAB integration."""

DOMAIN = "ynab"

API_BASE = "https://api.ynab.com/v1"
# YNAB's v1 API names budgets "plans" (/plans/{plan_id}/...). Kept in one
# place so a rename upstream is a one-line change.
PLAN_SEGMENT = "plans"

# Tool names must be prefixed with the domain and two underscores. Home
# Assistant stops accepting unprefixed names in 2027.3.
TOOL_PREFIX = f"{DOMAIN}__"

DEFAULT_SCAN_MINUTES = 15
MIN_SCAN_MINUTES = 5
MAX_SCAN_MINUTES = 60

# A category is "low" when available < share * target.
DEFAULT_LOW_SHARE = 0.20

# Allocation waterfall stages, in priority order.
STAGES = ("bills", "everyday", "fun", "savings")

# Never allocated to and never reported as low.
EXCLUDED_GROUP_NAMES = {"Credit Card Payments", "Internal Master Category"}

EVENT_TRANSACTION_IMPORTED = f"{DOMAIN}_transaction_imported"
EVENT_TRANSACTION_UPDATED = f"{DOMAIN}_transaction_updated"
EVENT_PROPOSAL_CREATED = f"{DOMAIN}_proposal_created"
EVENT_RECURRING_DETECTED = f"{DOMAIN}_recurring_detected"
EVENT_READY_TO_ASSIGN_INCREASED = f"{DOMAIN}_ready_to_assign_increased"
