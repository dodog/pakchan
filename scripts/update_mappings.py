#!/usr/bin/env python3
"""Update the main mappings.json with validated submissions."""

import json
import os
from datetime import datetime

def update_mappings():
    """Process all validated submissions and update mappings.json."""
    
    # Load current mappings
    with open("data/mappings.json", "r") as f:
        mappings = json.load(f)
    
    # Process each validated submission
    submissions_dir = "data/submissions"
    if not os.path.exists(submissions_dir):
        print("No submissions to process")
        return
    
    for filename in os.listdir(submissions_dir):
        if not filename.endswith(".json"):
            continue
        
        filepath = os.path.join(submissions_dir, filename)
        
        with open(filepath, "r") as f:
            submission = json.load(f)
        
        # Add to mappings
        pkg_name = submission["package_name"].lower()
        
        mappings["packages"][pkg_name] = {
            "name": submission.get("display_name") or pkg_name,
            "homepage": submission.get("homepage", ""),
            "category": submission.get("category", "other"),
            "sources": submission.get("sources", []),
            "added_date": datetime.now().strftime("%Y-%m-%d"),
        }
        
        # Move processed file
        processed_dir = "data/processed"
        os.makedirs(processed_dir, exist_ok=True)
        os.rename(filepath, os.path.join(processed_dir, filename))
        
        print(f"Added {pkg_name} to mappings")
    
    # Update metadata
    mappings["last_updated"] = datetime.now().strftime("%Y-%m-%d")
    mappings["total_packages"] = len(mappings["packages"])
    
    # Save updated mappings
    with open("data/mappings.json", "w") as f:
        json.dump(mappings, f, indent=2, sort_keys=True)
    
    print(f"Mappings updated: {mappings['total_packages']} packages total")

if __name__ == "__main__":
    update_mappings()