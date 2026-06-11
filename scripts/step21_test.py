import sys
import json
from azure.ai.contentunderstanding import ContentUnderstandingClient
from azure.ai.contentunderstanding.models import AnalysisInput
from azure.core.credentials import AzureKeyCredential
from azure.core.exceptions import AzureError

endpoint = "https://invoice-processing-dev-resource.services.ai.azure.com/"
key = "9n1Mwhx32kjiipj5F3A62Pell1qcBqRZr1VpTNCuBsbxILzaiGxXJQQJ99CFAC4f1cMXJ3w3AAAAACOGphgz"
file_url = "https://stinvoicedevwestus.blob.core.windows.net/cont-invoice-prototype/250524_0009.pdf?sp=r&st=2026-06-10T19:56:07Z&se=2026-06-11T04:11:07Z&spr=https&sv=2026-02-06&sr=b&sig=I8QeQUTRK7v3YQvUzCZV9HFWeilt%2B3uRCmd%2BLc7iBuc%3D"

client = ContentUnderstandingClient(
    endpoint=endpoint,
    credential=AzureKeyCredential(key),
    api_version="2025-11-01"
)

try:
    poller = client.begin_analyze(
        analyzer_id="invoicerouter",
        inputs=[AnalysisInput(url=file_url)],
    )
    result = poller.result()
except AzureError as err:
    print(f"[Azure Error]: {err.message}")
    sys.exit(1)

# Print full result so we can see exact structure
full = result.as_dict()
print(json.dumps(full, indent=2))

# Also print a focused confidence summary
print("\n" + "="*50)
print("CONFIDENCE SUMMARY")
print("="*50)

# Fix: SDK returns contents at top level, not nested under "result"
contents = full.get("contents", [])
if contents:
    fields = contents[0].get("fields", {})
    for field_name, field_data in fields.items():
        conf = field_data.get("confidence", "NOT PRESENT")
        val  = (field_data.get("valueString") or
                field_data.get("valueNumber") or
                field_data.get("valueDate") or "")
        print(f"  {field_name:30s}  confidence: {str(conf):8s}  value: {val}")
else:
    print("  No contents found - check result structure above")

# Check for top-level classification confidence (gate B1)
print("\n" + "="*50)
print("TOP-LEVEL STRUCTURE (gate B1 check)")
print("="*50)
result_block = full.get("result", {})
# Fix: top-level keys are directly on full, not under full["result"]
for key_name in full.keys():
    print(f"  {key_name}")