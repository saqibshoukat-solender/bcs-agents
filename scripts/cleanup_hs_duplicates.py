"""
Cleanup duplicate HubSpot deals.

For each group of deals that share the same dealname:
  - Keep the one with the lowest numeric deal ID (oldest created).
  - Archive (soft-delete) all others via DELETE /crm/v3/objects/deals/{id}.

Usage:
  docker compose run --rm casey python scripts/cleanup_hs_duplicates.py
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from integrations.hubspot import get_all_deals_paginated, archive_deal
from utils.logger import get_logger

logger = get_logger("cleanup_hs_duplicates")


def main() -> None:
    print("Fetching all HubSpot deals …")
    all_deals = get_all_deals_paginated(["dealname", "dealstage", "createdate"])

    if not all_deals:
        print("No deals found — nothing to clean up.")
        return

    print(f"Total deals fetched: {len(all_deals)}")

    # Group by normalised deal name
    groups: dict[str, list[dict]] = {}
    for deal in all_deals:
        name = (deal.get("properties", {}).get("dealname") or "").strip()
        groups.setdefault(name, []).append(deal)

    duplicates = {name: deals for name, deals in groups.items() if len(deals) > 1}

    if not duplicates:
        print("No duplicate deal names found — nothing to clean up.")
        total_unique = len(groups)
        print(f"Kept {total_unique} deals, deleted 0 duplicates.")
        return

    kept = 0
    deleted = 0

    for name, deals in duplicates.items():
        # Sort by numeric deal ID ascending — keep the lowest (oldest)
        try:
            sorted_deals = sorted(deals, key=lambda d: int(d["id"]))
        except (ValueError, KeyError):
            sorted_deals = deals  # fallback: keep original order

        keep = sorted_deals[0]
        to_delete = sorted_deals[1:]

        print(f"\nDuplicate: '{name}' ({len(deals)} copies)")
        print(f"  KEEPING  id={keep['id']}")

        for deal in to_delete:
            did = deal["id"]
            success = archive_deal(did)
            if success:
                print(f"  Deleted deal {did} ({name})")
                deleted += 1
            else:
                print(f"  FAILED to delete deal {did} ({name})")

        kept += 1

    # Also count non-duplicate deals as kept
    kept += sum(1 for deals in groups.values() if len(deals) == 1)

    print(f"\nSummary: Kept {kept} deals, deleted {deleted} duplicates.")


if __name__ == "__main__":
    main()
