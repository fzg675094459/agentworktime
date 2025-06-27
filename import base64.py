import base64
with open("gdrive_service_account.json", "rb") as f:
    encoded_string = base64.b64encode(f.read()).decode('utf-8')
print(encoded_string)