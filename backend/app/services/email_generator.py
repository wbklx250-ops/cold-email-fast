"""
Email Generator Service

Generates 50 unique email addresses from a display name using proven patterns.
NO numbers, NO random suffixes - strictly name-based patterns only.
"""

import secrets
from typing import List, Dict

# Standard mailbox password used across all mailboxes
MAILBOX_PASSWORD = "#Sendemails1"


def generate_email_variations(
    first_name: str,
    last_name: str,
    domain: str,
    count: int = 50,
) -> List[Dict[str, str]]:
    """Generate unique email variations from a persona name."""
    first = first_name.lower()
    last = last_name.lower()
    emails = set()

    # Pattern 1: first name variations
    emails.add(f"{first}@{domain}")
    emails.add(f"{first[0]}@{domain}")

    # Pattern 2: first.last variations
    emails.add(f"{first}.{last}@{domain}")
    emails.add(f"{first}{last}@{domain}")
    emails.add(f"{first[0]}{last}@{domain}")
    emails.add(f"{first[0]}.{last}@{domain}")

    # Pattern 3: last.first variations
    emails.add(f"{last}.{first}@{domain}")
    emails.add(f"{last}{first}@{domain}")
    emails.add(f"{last}{first[0]}@{domain}")
    emails.add(f"{last}.{first[0]}@{domain}")

    # Pattern 4: initials
    emails.add(f"{first[0]}{last[0]}@{domain}")
    emails.add(f"{first[0]}.{last[0]}@{domain}")

    # Pattern 5: underscore variations
    emails.add(f"{first}_{last}@{domain}")
    emails.add(f"{last}_{first}@{domain}")

    # Pattern 6: hyphen variations
    emails.add(f"{first}-{last}@{domain}")
    emails.add(f"{last}-{first}@{domain}")

    # Pattern 7: progressive first name + last
    for i in range(2, len(first) + 1):
        emails.add(f"{first[:i]}{last}@{domain}")
        emails.add(f"{first[:i]}.{last}@{domain}")
        emails.add(f"{last}{first[:i]}@{domain}")
        emails.add(f"{last}.{first[:i]}@{domain}")

    # Pattern 8: first + progressive last name
    for i in range(1, len(last) + 1):
        emails.add(f"{first}{last[:i]}@{domain}")
        emails.add(f"{first}.{last[:i]}@{domain}")
        emails.add(f"{last[:i]}{first}@{domain}")
        emails.add(f"{last[:i]}.{first}@{domain}")

    # Pattern 9: progressive both
    for i in range(1, min(len(first), 4) + 1):
        for j in range(1, min(len(last), 4) + 1):
            emails.add(f"{first[:i]}.{last[:j]}@{domain}")
            emails.add(f"{last[:j]}.{first[:i]}@{domain}")

    # Pattern 10: letter-suffix fallback for high counts / short names (no digits introduced).
    # Appends a-z to common base patterns until we have enough unique addresses.
    if len(emails) < count:
        import string
        base_patterns = [
            f"{first}.{last}",
            f"{first}{last}",
            f"{first[0]}{last}",
            f"{last}.{first}",
            f"{last}{first}",
            f"{first}_{last}",
            f"{first}-{last}",
        ]
        for suffix in string.ascii_lowercase:
            if len(emails) >= count:
                break
            for base in base_patterns:
                if len(emails) >= count:
                    break
                emails.add(f"{base}{suffix}@{domain}")

    email_list = sorted(list(emails))[:count]
    display_name = f"{first_name} {last_name}".strip()
    return [
        {"email": email, "display_name": display_name}
        for email in email_list
    ]


def generate_password(length: int = 12) -> str:
    """
    Generate a secure password.
    
    Requirements:
    - At least one lowercase letter
    - At least one uppercase letter
    - At least one digit
    - At least one special character
    - No ambiguous characters (0, O, l, 1, I)
    - Compliant with M365 password policy
    """
    # Character sets (excluding ambiguous characters)
    lowercase = "abcdefghjkmnpqrstuvwxyz"  # no l
    uppercase = "ABCDEFGHJKMNPQRSTUVWXYZ"  # no I, O
    digits = "23456789"  # no 0, 1
    special = "!@#$%^&*()-_=+"
    
    # Ensure at least one of each required type
    password = [
        secrets.choice(lowercase),
        secrets.choice(uppercase),
        secrets.choice(digits),
        secrets.choice(special),
    ]
    
    # Fill remaining length with random mix
    all_chars = lowercase + uppercase + digits + special
    password.extend(secrets.choice(all_chars) for _ in range(length - 4))
    
    # Shuffle to randomize position of required characters
    password_list = list(password)
    secrets.SystemRandom().shuffle(password_list)
    
    return ''.join(password_list)


def parse_display_name(display_name: str) -> tuple:
    """
    Parse display name into first and last name.
    
    Args:
        display_name: Full name like "Jack Zuvelek"
    
    Returns:
        Tuple of (first_name, last_name) in original casing
    """
    parts = display_name.strip().split()
    
    if len(parts) < 2:
        raise ValueError(f"Display name must have first and last name: '{display_name}'")
    
    first = parts[0]
    last = parts[-1]  # Use last part as surname (handles middle names)
    
    return first, last


def generate_emails_for_domain(
    display_name: str,
    domain: str,
    count: int = 50
) -> List[Dict[str, str]]:
    """
    Generate email addresses for a domain.
    
    Args:
        display_name: Full name like "Jack Zuvelek"
        domain: Domain like "loancatermail13.info"
        count: Number of emails to generate (default 50)
    
    Returns:
        List of dicts with keys: email, display_name, password, local_part
    """
    first, last = parse_display_name(display_name)

    variations = generate_email_variations(first, last, domain, count)
    emails = []

    for variation in variations:
        email = variation["email"]
        local_part = email.split("@")[0]
        # Use standard mailbox password for all mailboxes
        password = MAILBOX_PASSWORD

        emails.append(
            {
                "email": email,
                "display_name": variation["display_name"],
                "password": password,
                "local_part": local_part,
            }
        )

    return emails


def generate_email_addresses(
    first_name: str,
    last_name: str,
    domain: str,
    count: int = 50
) -> List[Dict[str, str]]:
    """
    Backward compatibility wrapper for orchestrator.py.
    
    Args:
        first_name: First name like "Jack"
        last_name: Last name like "Zuvelek"
        domain: Domain like "loancatermail13.info"
        count: Number of emails to generate (default 50)
    
    Returns:
        List of dicts with keys: email, display_name, password
    """
    display_name = f"{first_name} {last_name}"
    return generate_emails_for_domain(display_name, domain, count)


def generate_emails_for_batch(
    display_name: str,
    domains: List[str],
    emails_per_domain: int = 50
) -> List[Dict[str, str]]:
    """
    Generate emails for multiple domains in a batch.
    
    Args:
        display_name: Full name like "Jack Zuvelek"
        domains: List of domains
        emails_per_domain: Number of emails per domain (default 50)
    
    Returns:
        List of all email dicts across all domains
    """
    all_emails = []
    
    for domain in domains:
        domain_emails = generate_emails_for_domain(
            display_name=display_name,
            domain=domain,
            count=emails_per_domain
        )
        all_emails.extend(domain_emails)
    
    return all_emails


# ============================================================================
# TESTING
# ============================================================================

if __name__ == "__main__":
    # Test with sample data
    test_display_name = "Jack Zuvelek"
    test_domain = "loancatermail13.info"
    
    print(f"Generating emails for '{test_display_name}' @ {test_domain}")
    print("=" * 60)
    
    emails = generate_emails_for_domain(test_display_name, test_domain)
    
    for i, email_data in enumerate(emails, 1):
        print(f"{i:2}. {email_data['email']:40} | {email_data['password']}")
    
    print("=" * 60)
    print(f"Total: {len(emails)} emails generated")
    
    # Verify all emails are unique
    email_set = set(e['email'] for e in emails)
    print(f"Unique: {len(email_set)} (should match total)")
    
    # Verify no numbers in local parts
    has_numbers = any(any(c.isdigit() for c in e['local_part']) for e in emails)
    print(f"Contains numbers: {has_numbers} (should be False)")
