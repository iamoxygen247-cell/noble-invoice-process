import json
from azure.ai.contentunderstanding import ContentUnderstandingClient
from azure.core.credentials import AzureKeyCredential

ENDPOINT = "https://invoice-processing-dev-resource.services.ai.azure.com/"
API_KEY = "9n1Mwhx32kjiipj5F3A62Pell1qcBqRZr1VpTNCuBsbxILzaiGxXJQQJ99CFAC4f1cMXJ3w3AAAAACOGphgz"
API_VERSION = "2025-11-01"

client = ContentUnderstandingClient(
    endpoint=ENDPOINT,
    credential=AzureKeyCredential(API_KEY),
    api_version=API_VERSION,
)

defaults = client.get_defaults()
data = defaults.as_dict() if hasattr(defaults, "as_dict") else defaults
print(json.dumps(data, indent=2, default=str))