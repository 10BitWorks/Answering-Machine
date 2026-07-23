import httpx
import json
import os
import re
from loguru import logger

async def _call_api(entity: str, action: str, params: dict):
    url = os.getenv("CIVICRM_API_URL")
    if not url:
        return {"is_error": True, "error_message": "CiviCRM API URL not configured"}
    
    # Construct endpoint
    endpoint = url.rstrip("/") + f"/{entity}/{action}"
    
    api_key = os.getenv("CIVICRM_API_KEY")
    site_key = os.getenv("CIVICRM_SITE_KEY")
    
    headers = {
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded"
    }
    
    body = {
        "api_key": api_key,
        "key": site_key,
        "params": json.dumps(params)
    }
    
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(endpoint, headers=headers, data=body)
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        logger.error(f"CiviCRM API call failed ({entity}.{action}): {e}")
        return {"is_error": True, "error_message": str(e)}

async def get_membership_info(contact_id: int):
    params = {
        "select": ["membership_type_id:label", "status_id:label", "join_date", "start_date", "end_date"],
        "where": [["contact_id", "=", contact_id]],
        "orderBy": {"end_date": "DESC"},
        "limit": 3
    }
    data = await _call_api("Membership", "get", params)
    if data.get("is_error") or not data.get("values"):
        return "No active membership records found."
    
    summary = "Membership status:\n"
    for m in data["values"]:
        status = m.get("status_id:label", "Unknown")
        m_type = m.get("membership_type_id:label", "Unknown")
        join_date = m.get("join_date", "N/A")
        start_date = m.get("start_date", "N/A")
        end_date = m.get("end_date", "N/A")
        summary += f"- {m_type}: {status} (Joined: {join_date}, Started: {start_date}, Expires: {end_date})\n"
    return summary

async def list_contact_info(contact_id: int):
    """
    Returns a summary of all addresses, phone numbers, and emails.
    """
    results = {}
    
    # Get Addresses
    addr_data = await _call_api("Address", "get", {
        "select": ["id", "street_address", "city", "postal_code", "location_type_id:label", "is_primary"],
        "where": [["contact_id", "=", contact_id]]
    })
    results["addresses"] = addr_data.get("values", [])
    
    # Get Phones
    phone_data = await _call_api("Phone", "get", {
        "select": ["id", "phone", "location_type_id:label", "is_primary"],
        "where": [["contact_id", "=", contact_id]]
    })
    results["phones"] = phone_data.get("values", [])
    
    # Get Emails
    email_data = await _call_api("Email", "get", {
        "select": ["id", "email", "location_type_id:label", "is_primary"],
        "where": [["contact_id", "=", contact_id]]
    })
    results["emails"] = email_data.get("values", [])
    
    summary = "Current Contact Information:\n\n"
    
    summary += "Addresses:\n"
    if not results["addresses"]: summary += "- None\n"
    for a in results["addresses"]:
        primary = " (PRIMARY)" if a.get("is_primary") else ""
        summary += f"- ID {a['id']}: {a['street_address']}, {a['city']} {a['postal_code']} [{a['location_type_id:label']}]{primary}\n"
        
    summary += "\nPhone Numbers:\n"
    if not results["phones"]: summary += "- None\n"
    for p in results["phones"]:
        primary = " (PRIMARY)" if p.get("is_primary") else ""
        summary += f"- ID {p['id']}: {p['phone']} [{p['location_type_id:label']}]{primary}\n"
        
    summary += "\nEmails:\n"
    if not results["emails"]: summary += "- None\n"
    for e in results["emails"]:
        primary = " (PRIMARY)" if e.get("is_primary") else ""
        summary += f"- ID {e['id']}: {e['email']} [{e['location_type_id:label']}]{primary}\n"
        
    return summary

async def add_address(contact_id: int, street: str, city: str, zip_code: str, is_primary: bool = False):
    params = {
        "records": [{
            "contact_id": contact_id,
            "street_address": street,
            "city": city,
            "postal_code": zip_code,
            "location_type_id": 1, # Home
            "is_primary": is_primary
        }]
    }
    data = await _call_api("Address", "save", params)
    return "Address added successfully." if not data.get("is_error") else f"Error: {data.get('error_message')}"

async def add_phone(contact_id: int, phone: str, is_primary: bool = False):
    # Validate that the phone number contains actual digits
    if not any(char.isdigit() for char in phone):
        logger.info(f"Skipping phone addition for contact {contact_id}: '{phone}' contains no digits.")
        return "Skipped adding phone: no digits found."

    params = {
        "records": [{
            "contact_id": contact_id,
            "phone": phone,
            "location_type_id": 1, # Home
            "is_primary": is_primary
        }]
    }
    data = await _call_api("Phone", "save", params)
    return "Phone number added successfully." if not data.get("is_error") else f"Error: {data.get('error_message')}"

async def add_email(contact_id: int, email: str, is_primary: bool = False):
    params = {
        "records": [{
            "contact_id": contact_id,
            "email": email,
            "location_type_id": 1, # Home
            "is_primary": is_primary
        }]
    }
    data = await _call_api("Email", "save", params)
    return "Email added successfully." if not data.get("is_error") else f"Error: {data.get('error_message')}"

async def set_primary_record(entity: str, record_id: int):
    """
    Sets a specific record (Address, Phone, or Email) as primary.
    """
    params = {
        "records": [{
            "id": record_id,
            "is_primary": True
        }]
    }
    data = await _call_api(entity, "save", params)
    return f"{entity} updated to primary." if not data.get("is_error") else f"Error: {data.get('error_message')}"

async def create_contact(first_name: str, last_name: str, phone_number: str):
    """
    Creates a new Individual contact and associates the phone number if valid.
    """
    params = {
        "records": [{
            "contact_type": "Individual",
            "first_name": first_name,
            "last_name": last_name
        }]
    }
    data = await _call_api("Contact", "save", params)
    if data.get("is_error") or not data.get("values"):
        return {"success": False, "message": data.get("error_message", "Unknown error creating contact")}
    
    contact_id = data["values"][0]["id"]
    
    # Only attempt to add the phone number if it contains digits (e.g. not "Unknown Caller")
    if any(char.isdigit() for char in phone_number):
        await add_phone(contact_id, phone_number, is_primary=True)
        msg = f"Contact record created for {first_name} {last_name} with phone {phone_number}."
    else:
        msg = f"Contact record created for {first_name} {last_name} (phone omitted as it was restricted/unknown)."
    
    return {
        "success": True, 
        "contact_id": contact_id, 
        "message": msg
    }

async def log_call_activity(contact_id: int, subject: str, details: str):
    """
    Creates a 'Phone Call' activity (type 2) in CiviCRM associated with the given contact.
    """
    system_contact_id = int(os.getenv("CIVICRM_SYSTEM_CONTACT_ID", "3800"))
    params = {
        "values": {
            "activity_type_id": 2, # 2 typically means Phone Call in CiviCRM
            "subject": subject,
            "details": details,
            "status_id": 2, # 2 typically means Completed
            "source_contact_id": system_contact_id,
            "target_contact_id": [contact_id]
        }
    }
    data = await _call_api("Activity", "create", params)
    return not data.get("is_error")

async def get_contact_email(contact_id: int):
    """
    Returns the primary email address for a contact, or None if not found.
    """
    params = {
        "select": ["email"],
        "where": [
            ["contact_id", "=", contact_id],
            ["is_primary", "=", 1]
        ]
    }
    data = await _call_api("Email", "get", params)
    if data.get("values"):
        return data["values"][0].get("email")
    return None

