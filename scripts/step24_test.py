import sys
import json
from azure.ai.contentunderstanding import ContentUnderstandingClient
from azure.ai.contentunderstanding.models import AnalysisInput
from azure.core.credentials import AzureKeyCredential
from azure.core.exceptions import AzureError

endpoint = "https://invoice-processing-dev-resource.services.ai.azure.com/"
key = "9n1Mwhx32kjiipj5F3A62Pell1qcBqRZr1VpTNCuBsbxILzaiGxXJQQJ99CFAC4f1cMXJ3w3AAAAACOGphgz"
file_url = "https://stinvoicedevwestus.blob.core.windows.net/cont-invoice-prototype/250524_0009.pdf?sp=r&st=2026-06-11T00:59:03Z&se=2026-06-11T09:14:03Z&spr=https&sv=2026-02-06&sr=b&sig=iyqM7cUGT2K%2FduTLH6zilnX0%2Bcs8JMqRZ0l7W5qCTqE%3D"

client = ContentUnderstandingClient(
    endpoint=endpoint,
    credential=AzureKeyCredential(key),
    api_version="2025-11-01"
)

print(f"Analyzing: {file_url}")
print("=" * 60)

try:
    poller = client.begin_analyze(
        analyzer_id="invoicerouter",
        inputs=[AnalysisInput(url=file_url)],
    )
    result = poller.result()
except AzureError as err:
    print(f"[Azure Error]: {err.message}")
    sys.exit(1)

full = result.as_dict()

# Save full result for inspection
with open("result.json", "w") as f:
    json.dump(full, f, indent=2)
print("Full result saved to result.json")
print()

# Find the child analyzer result — the content object that has fields
contents = full.get("contents", [])
child_content = None
for c in contents:
    if c.get("fields"):
        child_content = c
        break

if not child_content:
    print("No fields found in any content object.")
    print("Document may have been routed to 'other' or extraction returned nothing.")
    print()
    # Print what we did get
    for i, c in enumerate(contents):
        print(f"contents[{i}]: kind={c.get('kind')} analyzerId={c.get('analyzerId')} category={c.get('category','NOT PRESENT')}")
        segments = c.get("segments", [])
        for j, seg in enumerate(segments):
            print(f"  segment[{j}]: category={seg.get('category','NOT PRESENT')}")
    sys.exit(0)

# Classification result
category = child_content.get("category", "NOT FOUND")
analyzer_used = child_content.get("analyzerId", "NOT FOUND")
print(f"Category:      {category}")
print(f"Analyzer used: {analyzer_used}")
print()

# Field results
fields = child_content.get("fields", {})
print(f"{'Field':<22} {'Confidence':<12} {'Gate B4?':<10} Value")
print("-" * 80)

critical_fields = ["vendor_name", "invoice_number", "invoice_date", "due_date", "invoice_total"]
threshold = 0.75
review_triggered = False
review_reasons = []

for field_name, field_data in fields.items():
    ftype = field_data.get("type")

    if ftype == "array":
        items = field_data.get("valueArray", [])
        print(f"  {field_name:<20} {'N/A':<12} {'—':<10} {len(items)} item(s)")
        for i, item in enumerate(items):
            props = item.get("valueObject", {})
            desc = props.get("description", {}).get("valueString", "")
            qty  = props.get("quantity", {}).get("valueNumber", "")
            price = props.get("unit_price", {}).get("valueNumber", "")
            total = props.get("total_amount", {}).get("valueNumber", "")
            print(f"    [{i}] desc={desc}  qty={qty}  unit_price={price}  total={total}")

    elif ftype == "string" and field_name == "anomaly_flag":
        val = field_data.get("valueString", "")
        conf = field_data.get("confidence", "N/A")
        display = val if val else "(empty — no anomaly)"
        print(f"  {field_name:<20} {'advisory':<12} {'—':<10} {display}")

    else:
        conf = field_data.get("confidence")
        val = (field_data.get("valueString") or
               field_data.get("valueNumber") or
               field_data.get("valueDate") or "")

        if conf is None:
            gate = "—"
            conf_display = "N/A"
        elif isinstance(conf, float) and field_name in critical_fields:
            if conf < threshold:
                gate = "✗ REVIEW"
                review_triggered = True
                review_reasons.append(f"{field_name} confidence {conf:.3f} < {threshold}")
            else:
                gate = "✓ pass"
            conf_display = f"{conf:.3f}"
        else:
            gate = "—"
            conf_display = f"{conf:.3f}" if isinstance(conf, float) else str(conf)

        print(f"  {field_name:<20} {conf_display:<12} {gate:<10} {val}")

print()

# is_handwritten gate
is_handwritten_field = fields.get("is_handwritten", {})
is_handwritten_val = is_handwritten_field.get("valueString", "")
is_handwritten_conf = is_handwritten_field.get("confidence", 0)

print("=" * 60)
print("ROUTING DECISION")
print("=" * 60)

if category == "other":
    print("→ REJECT — category is 'other', no further processing")
elif is_handwritten_val == "yes":
    print(f"→ REVIEW — Gate B3: is_handwritten = yes (confidence {is_handwritten_conf:.3f})")
    print("  Mandatory human review — financial control policy")
elif review_triggered:
    print("→ REVIEW — Gate B4: one or more critical fields below confidence threshold")
    for reason in review_reasons:
        print(f"  {reason}")
else:
    print("→ HAPPY PATH — all gates passed")
    print("  Eligible for auto-write to Dynamics 365 CE")
    print("  (subject to B5 Dataverse duplicate check)")

print()
print(f"vendor_category: {fields.get('vendor_category', {}).get('valueString', 'N/A')}")
anomaly = fields.get("anomaly_flag", {}).get("valueString", "")
print(f"anomaly_flag:    {anomaly if anomaly else '(empty)'}")