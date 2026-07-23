import httpx
import json
import os
import re
from loguru import logger

async def lookup_contact_by_name(full_name: str):
    """
    Queries CiviCRM for a contact by display_name and returns their phone numbers.
    Uses APIv4 chaining to get all phones in a single call.
    """
    url = os.getenv("CIVICRM_API_URL")
    if not url:
        logger.error("CIVICRM_API_URL not set")
        return []
    
    # Ensure URL ends with the correct endpoint for Contact.get
    if not url.endswith("Contact/get"):
        url = url.rstrip("/") + "/Contact/get"
    
    api_key = os.getenv("CIVICRM_API_KEY")
    site_key = os.getenv("CIVICRM_SITE_KEY")
    
    headers = {
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded"
    }
    
    # APIv4 query with chaining for Phone records
    params = {
        "select": ["display_name", "first_name", "last_name"],
        "where": [["display_name", "=", full_name]],
        "limit": 5,
        "chain": {
            "phones": ["Phone", "get", {
                "where": [["contact_id", "=", "$id"]],
                "select": ["phone", "location_type_id:label", "is_primary"]
            }]
        }
    }
    
    # CiviCRM REST often expects 'params' as a form field containing JSON
    body = {
        "api_key": api_key,
        "key": site_key,
        "params": json.dumps(params)
    }
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(url, headers=headers, data=body)
            response.raise_for_status()
            data = response.json()
            
            if data.get("is_error"):
                logger.error(f"CiviCRM API error: {data.get('error_message')}")
                return []
            
            contacts = []
            for val in data.get("values", []):
                contact = {
                    "display_name": val.get("display_name"),
                    "phones": []
                }
                
                seen_numbers = set()
                # Sort phones so primary comes first
                sorted_phones = sorted(val.get("phones", []), key=lambda x: not x.get("is_primary", False))
                
                for phone_rec in sorted_phones:
                    # Sanitize phone number (strip non-digits, but keep + for E.164)
                    raw_phone = phone_rec.get("phone", "")
                    clean_phone = re.sub(r'[^\d+]', '', raw_phone)
                    
                    if clean_phone:
                        # Normalize for deduplication (strip leading +1 or 1)
                        dedup_phone = re.sub(r'^\+?1', '', clean_phone) if len(clean_phone) >= 10 else clean_phone
                        if dedup_phone not in seen_numbers:
                            seen_numbers.add(dedup_phone)
                            contact["phones"].append({
                                "number": clean_phone,
                                "label": phone_rec.get("location_type_id:label", "Other"),
                                "is_primary": phone_rec.get("is_primary", False)
                            })
                
                contacts.append(contact)
            
            return contacts
            
    except Exception as e:
        logger.error(f"Failed to lookup CiviCRM contact: {e}")
        return []

def format_disambiguation_message(contacts):
    """
    Helps format a message for the LLM when multiple options are found.
    """
    if not contacts:
        return "No contact found with that exact name."
    
    if len(contacts) > 1:
        names = [c["display_name"] for c in contacts]
        return f"I found multiple contacts matching that name: {', '.join(names)}. Could you be more specific?"
    
    contact = contacts[0]
    phones = contact["phones"]
    
    if not phones:
        return f"I found {contact['display_name']}, but they don't have a phone number on file."
    
    if len(phones) == 1:
        return None # Success!
    
    # Multiple phones for one contact
    options = [f"{p['label']}: {p['number']}" for p in phones]
    return f"I found multiple numbers for {contact['display_name']}: {', '.join(options)}. Which one should I call?"

async def lookup_contact_by_phone(phone_number: str):
    """
    Queries CiviCRM for a contact by phone number and returns their first name or display name.
    """
    url = os.getenv("CIVICRM_API_URL")
    if not url:
        return None
    
    # Switch endpoint from Contact/get to Phone/get if necessary
    url = url.replace("Contact/get", "Phone/get")
    if not url.endswith("Phone/get"):
        url = url.rstrip("/") + "/Phone/get"
    
    api_key = os.getenv("CIVICRM_API_KEY")
    site_key = os.getenv("CIVICRM_SITE_KEY")
    
    headers = {
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded"
    }
    
    # Strip to digits for better matching
    clean_phone = re.sub(r'[^\d]', '', phone_number)
    if not clean_phone:
        return None
        
    # Match the last 10 digits to handle country codes
    match_phone = clean_phone[-10:] if len(clean_phone) >= 10 else clean_phone
    
    params = {
        "select": ["contact_id.first_name", "contact_id.display_name", "contact_id"],
        "where": [["phone", "LIKE", f"%{match_phone}%"]],
        "limit": 1
    }
    
    body = {
        "api_key": api_key,
        "key": site_key,
        "params": json.dumps(params)
    }
    
    try:
        async with httpx.AsyncClient(timeout=4.5) as client:
            response = await client.post(url, headers=headers, data=body)
            response.raise_for_status()
            data = response.json()
            
            if data.get("is_error") or not data.get("values"):
                return None
                
            val = data["values"][0]
            name = val.get("contact_id.first_name") or val.get("contact_id.display_name")
            contact_id = val.get("contact_id")
            
            if name and contact_id:
                return {"name": name, "contact_id": contact_id}
            return None
            
    except Exception as e:
        logger.error(f"Failed to lookup CiviCRM phone: {e}")
        return None
