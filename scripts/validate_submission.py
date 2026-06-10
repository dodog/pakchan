#!/usr/bin/env python3
"""Validate issue submissions and parse them into JSON format."""

import json
import os
import re
import sys
from github import Github

def parse_issue_body(body: str) -> dict:
    """Parse the issue body into structured data."""
    data = {
        "package_name": "",
        "display_name": "",
        "homepage": "",
        "category": "",
        "sources": [],
        "notes": ""
    }
    
    # Extract package name
    name_match = re.search(r'\*\*Package Name\*\*:\s*(.+)', body)
    if name_match:
        data["package_name"] = name_match.group(1).strip()
    
    # Extract display name
    display_match = re.search(r'\*\*Display Name\*\*:\s*(.+)', body)
    if display_match:
        data["display_name"] = display_match.group(1).strip()
    
    # Extract homepage
    homepage_match = re.search(r'\*\*Homepage\*\*:\s*(.+)', body)
    if homepage_match:
        data["homepage"] = homepage_match.group(1).strip()
    
    # Extract category
    category_match = re.search(r'\*\*Category\*\*:\s*(.+)', body)
    if category_match:
        data["category"] = category_match.group(1).strip()
    
    # Extract sources
    source_sections = re.findall(r'#### Source \d+\n(.*?)(?=\n####|\n###|\Z)', body, re.DOTALL)
    for section in source_sections:
        source = {}
        for line in section.strip().split('\n'):
            if line.startswith('- '):
                parts = line[2:].split(':', 1)
                if len(parts) == 2:
                    key = parts[0].strip().lower().replace(' ', '_')
                    value = parts[1].strip()
                    if value != 'N/A':
                        source[key] = value
        if source:
            data["sources"].append(source)
    
    # Extract notes
    notes_match = re.search(r'\*\*Additional Notes\*\*\n(.*?)(?=\n---|\Z)', body, re.DOTALL)
    if notes_match:
        notes = notes_match.group(1).strip()
        if notes != 'None':
            data["notes"] = notes
    
    return data

def main():
    if len(sys.argv) < 2:
        print("Usage: validate_submission.py <issue_number>")
        sys.exit(1)
    
    issue_number = int(sys.argv[1])
    
    # Get GitHub token
    token = os.environ.get('GITHUB_TOKEN')
    if not token:
        print("Error: GITHUB_TOKEN not set")
        sys.exit(1)
    
    # Get issue from GitHub API
    g = Github(token)
    repo = g.get_repo(os.environ['GITHUB_REPOSITORY'])
    issue = repo.get_issue(issue_number)
    
    # Parse the issue
    data = parse_issue_body(issue.body)
    
    # Validate
    errors = []
    if not data["package_name"]:
        errors.append("Package name is required")
    if not data["sources"]:
        errors.append("At least one source is required")
    
    if errors:
        # Add validation-failed label and comment
        issue.add_to_labels("validation-failed")
        issue.create_comment(
            "❌ **Validation Failed**\n\n" +
            "\n".join(f"- {e}" for e in errors) +
            "\n\nPlease fix these issues and update the issue."
        )
        sys.exit(1)
    
    # Save parsed data
    output_dir = "data/submissions"
    os.makedirs(output_dir, exist_ok=True)
    
    with open(f"{output_dir}/submission_{issue_number}.json", "w") as f:
        json.dump(data, f, indent=2)
    
    # Add validated label
    issue.add_to_labels("validated")
    issue.create_comment(
        "✅ **Submission Validated**\n\n"
        "The submission has been validated and will be added to the database shortly."
    )
    
    print(f"Successfully validated submission for {data['package_name']}")

if __name__ == "__main__":
    main()