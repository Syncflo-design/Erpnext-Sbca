# Copyright (c) 2026, doreen@gmail.com and contributors
# For license information, please see license.txt

import frappe
import requests
from frappe.model.document import Document


class ErpnextSbcaSettings(Document):
	pass

@frappe.whitelist()
def get_authentication_details():
	try:
		settings = frappe.get_doc("Erpnext Sbca Settings")
		company_settings = frappe.db.get_all("Company Sage Integration", filters={"parent": settings.name}, fields=["name"])
		for company in company_settings:
			company = frappe.get_doc("Company Sage Integration", company.name)
			headers = {
				"Content-Type": "application/json" ,
				"accept": "*/*" 

			}

			payload = {
				"provider": company.get_password('provider'),
				"clientType":company.get('client_type'),
				"userIdentifier": company.get_password('user_identifier'),
				"redirectBackTo": company.get('redirect_back_to')
				}
			
			response = requests.post(f"{settings.url}/auth/begin", json=payload, headers=headers)

			if response.status_code == 200:
				doc = frappe.get_doc("Company Sage Integration", company.get('name'))
				doc.auth_url = response.json().get("authUrl")
				doc.session_id = response.json().get("sessionId")
				doc.save()
				doc.reload()
				settings.reload()
				return frappe.msgprint(f"✅ Authentication details retrieved for {company.get('company')}")
			else:
				frappe.log_error(message=f"Failed to get authentication details for {company.get('company')}: {response.text}", title=f"Sage Authentication Error for {company.get('company')}")
				return frappe.msgprint(f"❌ Failed to get authentication details for {company.get('company')}<br>Error: {response.text}")
	except Exception as e:
		frappe.log_error(message=str(e), title="Error in get_authentication_details")
		return frappe.msgprint(f"❌ An error occurred while getting authentication details.<br>Error: {str(e)}")


